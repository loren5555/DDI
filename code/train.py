import numpy as np
import pandas as pd
import pickle as pkl
import os.path

from argparse import ArgumentParser

import dgl
import dgl.nn
import dgl.data

import torch
import torch.nn as nn
import torch.optim as optim
import pytorch_lightning as pl
from pytorch_lightning import Trainer
from pytorch_lightning.utilities.types import STEP_OUTPUT, TRAIN_DATALOADERS, EVAL_DATALOADERS
from pytorch_lightning.callbacks import ModelCheckpoint
from pytorch_lightning.callbacks.early_stopping import EarlyStopping

from sklearn.model_selection import train_test_split
from sklearn.metrics import f1_score, accuracy_score, precision_score, recall_score

from model import *
from utils import *
from data import *

from typing import Optional

DEFAULT_HIDDEN_SIZE = 512
DEFAULT_BATCH_SIZE = None
DEFAULT_LEARNING_RATE = 1e-3
DEFAULT_MIN_SAMPLE_SIZE = 10

class Model(pl.LightningModule):

    def __init__(self, 
                 pkl_path: str = '../dataset.pkl', 
                 hidden_size=DEFAULT_HIDDEN_SIZE, 
                 learning_rate: float = DEFAULT_LEARNING_RATE,
                 min_sample_size: int = DEFAULT_MIN_SAMPLE_SIZE,
                 batch_size: Optional[int] = DEFAULT_BATCH_SIZE
                 ):
        super().__init__()

        # save hyperparameters
        self.pkl_path = pkl_path
        self.learning_rate = learning_rate
        self.batch_size = batch_size
        self.save_hyperparameters()

        self.dataset = DDIDataSet(pkl_path, min_sample_size)

        # build model
        self.sage = RGCN(
            in_feats_d = self.dataset.num_drug_features,
            in_feats_p = self.dataset.num_protein_features,
            hidden_size = hidden_size,
            out_feats = self.dataset.num_ddi_types + 1,
            rel_names = [f'DDI_{y:02}' for y in self.dataset.ddi_types] + ['DPI', 'PDI', 'PPI']
        )
        self.predictor = MLPPredictor(
            in_features = hidden_size,
            out_classes = self.dataset.num_ddi_types + 1
        )

        self.loss_func = nn.CrossEntropyLoss()

    def setup(self, stage: str = None) -> None:
        self.train_eid, self.test_eid = train_test_split(
            np.arange(self.dataset.num_ddi),
            test_size=0.2, stratify=self.dataset.ddi_graph.edata[dgl.ETYPE].cpu()
        )

    def train_dataloader(self) -> TRAIN_DATALOADERS:
        return DDIDataLoader(self.dataset, self.train_eid, batch_size=self.batch_size)

    def training_step(self, batch: tuple[dgl.DGLGraph, dgl.DGLGraph, dgl.DGLGraph], batch_idx: int) -> STEP_OUTPUT:
        graph, pos_pair_graph, neg_pair_graph = batch

        pos_score, neg_score = self(graph.to(self.device), pos_pair_graph, neg_pair_graph)
        prediction = torch.cat([pos_score, neg_score])
        real_label = torch.cat([pos_pair_graph.edata[dgl.ETYPE] + 1, 
                                torch.zeros(neg_score.shape[0], device=self.device, dtype=torch.int)])
        
        loss = self.loss_func(prediction, real_label)
        self.log('train_loss', loss)
        return loss
    
    def val_dataloader(self) -> EVAL_DATALOADERS:
        return DDIDataLoader(self.dataset, self.test_eid, batch_size=self.batch_size)

    def validation_step(self, batch: tuple[dgl.DGLGraph, dgl.DGLGraph, dgl.DGLGraph], batch_idx: int) -> STEP_OUTPUT:
        graph, pos_pair_graph, neg_pair_graph = batch
        
        pos_score, neg_score = self(graph.to(self.device), pos_pair_graph, neg_pair_graph)
        prediction = torch.cat([pos_score, neg_score])
        real_label = torch.cat([pos_pair_graph.edata[dgl.ETYPE] + 1, 
                                torch.zeros(neg_score.shape[0], device=self.device, dtype=torch.int)])
        loss = self.loss_func(prediction, real_label)

        pred_label = torch.argmax(prediction, dim=1).cpu()
        real_label = real_label.cpu()
        f1 = f1_score(real_label, pred_label, average='weighted')
        accuracy = accuracy_score(real_label, pred_label)
        precision = precision_score(real_label, pred_label, average='weighted')
        recall = recall_score(real_label, pred_label, average='weighted')
        self.log_dict({'val_f1': f1, 'val_accuracy': accuracy, 'val_precision': precision, 'val_recall': recall, 'val_loss': loss}, batch_size=1)
        return loss
    
    def configure_optimizers(self):
        return optim.Adam(self.parameters(), lr=self.learning_rate)
    
    def forward(self, hg: dgl.DGLGraph, *ddi_graphs: dgl.DGLGraph) -> tuple[torch.Tensor, ...]:
        '''
        Parameters
        ----------
        hg : DGLGraph
            The heterogeneous graph that contains 'Drug', 'Protein' nodes and 'DDI', 'DPI', 'PDI', 'PPI' edges.
            The graph should be bidirected.
        ddi_graphs : DGLGraph
            The graphs that contains 'Drug' nodes and 'DDI' edges for prediction.
        Returns
        -------
        tuple[torch.Tensor, ...]
            The prediction of the DDI types as probability vectors.
        '''
        h = self.sage(hg, hg.ndata['feature'])
        return tuple(
            self.predictor(g, h['Drug'][g.ndata[dgl.NID]]) 
            if dgl.NID in g.ndata.keys() else self.predictor(g, h['Drug'])
            for g in ddi_graphs
        )

def config_parser(parser: ArgumentParser = ArgumentParser()) -> ArgumentParser:

    subparsers = parser.add_subparsers(title='Subcommands', dest='subcommand', required=True)

    new_parser = subparsers.add_parser('new', help='Train a new model')
    resume_parser = subparsers.add_parser('resume', help='Resume training a model')

    model_parser = new_parser.add_argument_group('Model', 'Arguments for Model')
    model_parser.add_argument('--pkl-path', type=str, default='../dataset.pkl', help='Path to the dataset pickle file')
    model_parser.add_argument('--hidden-size', type=int, default=DEFAULT_HIDDEN_SIZE, help='Hidden size of the model')
    model_parser.add_argument('--learning-rate', type=float, default=DEFAULT_LEARNING_RATE, help='Learning rate of the optimizer')
    model_parser.add_argument('--batch-size', type=int, default=DEFAULT_BATCH_SIZE, help='Batch size of the dataloader')
    model_parser.add_argument('--min-sample-size', type=int, default=DEFAULT_MIN_SAMPLE_SIZE, help='Minimum sample size of each DDI type, used to filter out the DDI types with few samples')


    resume_parser = resume_parser.add_argument_group('Resume')
    resume_parser.add_argument('checkpoint_path', type=str, default=None)
    
    for p in (model_parser, resume_parser):
        trainer_parser = p.add_argument_group('Trainer', 'Arguments for Trainer')
        trainer_parser.add_argument('--early-stop-patience', type=int, default=3, help='Patience for early stopping')

    return parser

if __name__ == '__main__':

    import warnings
    warnings.filterwarnings('ignore')
    from rich import traceback, print
    traceback.install()

    parser = config_parser()
    args = parser.parse_args()

    monitor_config = dict(
        monitor='val_loss',
        mode='min',
    )
    
    checkpoint_callback = ModelCheckpoint(
        dirpath='./model_checkpoint',
        save_top_k=3,
        **monitor_config
    )

    early_stop_callback = EarlyStopping(
        min_delta=0.00,
        patience=args.early_stop_patience,
        **monitor_config
    )
    
    early_stop_callback = EarlyStopping(
        monitor='val_loss',
        min_delta=0.00,
        patience=args.early_stop_patience,
        verbose=False,
        mode='min'
    )

    trainer = Trainer(
        max_epochs=-1,
        strategy='ddp_find_unused_parameters_true',
        callbacks=[checkpoint_callback, early_stop_callback],
        log_every_n_steps=1,
        profiler='advanced',
    )

    checkpoint_callback.dirpath = os.path.join(
        trainer.logger.log_dir,
        'checkpoints'
    )

    match args.subcommand:
        case 'new':
            model = Model(
                pkl_path=args.pkl_path, 
                hidden_size=args.hidden_size, 
                learning_rate=args.learning_rate,
                min_sample_size=args.min_sample_size,
                batch_size=args.batch_size
            )
            trainer.fit(model)
        case 'resume':
            print("resume from", args.checkpoint_path)
            model = Model.load_from_checkpoint(args.checkpoint_path)
            trainer.fit(model, checkpoint_path=args.checkpoint_path)