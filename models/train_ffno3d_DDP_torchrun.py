import os, sys
module_path = os.path.abspath(os.path.join('../'))
if module_path not in sys.path:
    sys.path.append(module_path)

import numpy as np
import timeit
import torch
import torch.nn as nn
import torch.nn.functional as F
from torch import optim
import h5py
import argparse

from models.functions.helpers_deeplearning import get_device, get_batch_size, loss_criterion, RunningAverage, EarlyStopper
from models.functions.ffno_model import FFNO_3D
from models.functions.data_loader import GeologyTracesDataset

from torch.utils.data import DataLoader, Dataset
#DDP
import torch.multiprocessing as mp
from torch.utils.data.distributed import DistributedSampler
from torch.nn.parallel import DistributedDataParallel as DDP
from torch.distributed import init_process_group, destroy_process_group


parser = argparse.ArgumentParser(prefix_chars='@')
parser.add_argument('@S_in', type=int, default=32, help="Size of the spatial input grid")
parser.add_argument('@S_in_z', type=int, default=32, help="Size of the spatial input grid")
parser.add_argument('@S_out', type=int, default=32, help="Size of the spatial output grid")
parser.add_argument('@T_out', type=int, default=320, help="Number of time steps")
parser.add_argument('@Ntrain', type=int, default=27000, help="Number of training samples")
parser.add_argument('@Nval', type=int, default=3000, help="Number of validation samples")
parser.add_argument('@batch_size', type=int, default=16, help = 'batch size')
parser.add_argument('@epochs', type=int, default=350, help = 'Number of epochs')
parser.add_argument('@dv', type=int, default=16, help = "Number of channels after the liting")
parser.add_argument('@list_D1', type=int, nargs='+', default = [32, 32, 32, 32], help = "Dimensions along the 1st dimension after each block")
parser.add_argument('@list_D2', type=int, nargs='+', default = [32, 32, 32, 32], help = "Dimensions along the 2nd dimension after each block")
parser.add_argument('@list_D3', type=int, nargs='+', default = [64, 128, 256, 320], help = "Dimensions along the 3rd dimension after each block")
parser.add_argument('@list_M1', type=int, nargs='+', default = [16, 16, 16, 16], help = "Number of modes along the 1st dimension after each block")
parser.add_argument('@list_M2', type=int, nargs='+', default = [16, 16, 16, 16], help = "Number of modes along the 2nd dimension after each block")
parser.add_argument('@list_M3', type=int, nargs='+', default = [16, 32, 32, 32], help = "Number of modes along the 3rd dimension after each block")
parser.add_argument('@nlayers', type=int, default=4, help="Number of layers")
parser.add_argument('@padding', type=int, default=0, help = "Number of pixels for padding on each side of x and y")
parser.add_argument('@learning_rate', type=float, default=0.0006, help='learning rate')
parser.add_argument('@loss_weights', type=float, nargs='+', default = [1.0, 0.0], help = "Weight of L1 loss, L2 loss")
parser.add_argument('@dir_data_train', type=str, nargs='+', default=['inputs3D_S32_Z32_T320_fmax5_train'], help="Name of folders with training data")
parser.add_argument('@dir_data_val', type=str, nargs='+', default=['inputs3D_S32_Z32_T320_fmax5_val'], help="Name of folders with training data")
parser.add_argument('@additional_name', type=str, default="", help="string to add to the configuration name for saved outputs")
options = parser.parse_args().__dict__


path_data = './inputs/'
batch_size = options['batch_size']
Ntrain = options['Ntrain']
Nval = options['Nval']
learning_rate = options['learning_rate']
weight_decay = 0.00001
patience = 60 # number of epochs to wait for the validation loss to decrease before stopping the training
epochs = options['epochs']
loss_weights = options['loss_weights']


# UNO parameters
dv = options['dv']
S_in = options['S_in']
S_in_z = options['S_in_z']
S_out = options['S_out']
T_out = options['T_out']
padding = options['padding']

def ddp_setup():
    init_process_group(backend="nccl")
    torch.cuda.set_device(int(os.environ["LOCAL_RANK"]))

class Trainer:
    def __init__(
        self,
        model: torch.nn.Module,
        train_data: DataLoader,
        optimizer: torch.optim.Optimizer,
        val_data: DataLoader,
        snapshot_path: str,
    ) -> None:
        self.gpu_id = int(os.environ["LOCAL_RANK"])
        self.model = model.to(self.gpu_id)
        self.train_data = train_data
        self.optimizer = optimizer
        self.val_data = val_data
        self.epochs_run = 0
        self.snapshot_path = snapshot_path
        if os.path.exists(snapshot_path):
            print("Loading snapshot")
            self._load_snapshot(snapshot_path)

        self.model = DDP(model, device_ids=[self.gpu_id])

    def _load_snapshot(self, snapshot_path):

        loc = f"cuda:{self.gpu_id}"

        snapshot = torch.load(snapshot_path, map_location=loc)

        self.model.load_state_dict(snapshot["MODEL_STATE"])

        self.epochs_run = snapshot["EPOCHS_RUN"]

        print(f"Resuming training from snapshot at Epoch {self.epochs_run}")

    def _run_batch(self, source, targets):
        self.optimizer.zero_grad()
        output = self.model(source)
        loss = F.cross_entropy(output, targets)
        loss.backward()
        self.optimizer.step()

    def _run_epoch(self, epoch):
        b_sz = len(next(iter(self.train_data))[0])
        print(f"[GPU{self.gpu_id}] Epoch {epoch} | Batchsize: {b_sz} | Steps: {len(self.train_data)}")
        self.train_data.sampler.set_epoch(epoch)
        for source, targets in self.train_data:
            source = source.to(self.gpu_id)
            targets = targets.to(self.gpu_id)
            self._run_batch(source, targets)

    def _save_snapshot(self, epoch):
        snapshot = {
            "MODEL_STATE": self.model.module.state_dict(),
            "EPOCHS_RUN": epoch,
        }

        torch.save(snapshot, self.snapshot_path)
        print(f"Epoch {epoch} | Training snapshot saved at {self.snapshot_path}")

    def train(self, name_config: str):
        # Store losses history
        train_history = {'loss_relative':[], 'loss_absolute':[]}
        val_history = {'loss_relative':[], 'loss_absolute':[]}
        best_loss = np.inf

        ### OPTIMIZER
        lr_scheduler = torch.optim.lr_scheduler.ReduceLROnPlateau(self.optimizer, 'min', factor=0.5, patience=10, verbose=True) 
        early_stopper = EarlyStopper(patience=patience, min_delta=0.0001)

        ### TRAINING       
        for ep in range(self.epochs_run, epochs):
            self.train_data.sampler.set_epoch(ep)
            t1 = timeit.default_timer()
            self.model.train()
            train_losses_relative = RunningAverage()
            train_losses_absolute = RunningAverage()
        
            # training
            for _ in self.train_data:
                a = _[0].to(self.gpu_id)
                uE = _[1].to(self.gpu_id)
                uN = _[2].to(self.gpu_id)
                uZ = _[3].to(self.gpu_id)
                outE, outN, outZ = self.model(a)
                loss_rel = loss_criterion((outE,outN,outZ), (uE,uN,uZ), loss_weights, relative=True)
                loss_abs = loss_criterion((outE,outN,outZ), (uE,uN,uZ), loss_weights, relative=False)
            
                train_losses_relative.update(loss_rel.item(), get_batch_size(a))
                train_losses_absolute.update(loss_abs.item(), get_batch_size(a))
            
                # compute gradients and update parameters
                self.optimizer.zero_grad()
                loss_rel.backward()
                self.optimizer.step()
        
            train_history['loss_relative'].append(train_losses_relative.avg)
            train_history['loss_absolute'].append(train_losses_absolute.avg)

            # validation
            self.model.eval()
            with torch.no_grad():
                val_losses_relative = RunningAverage()
                val_losses_absolute = RunningAverage()

                # training
                for _ in self.val_data:
                    a = _[0].to(self.gpu_id)
                    uE = _[1].to(self.gpu_id)
                    uN = _[2].to(self.gpu_id)
                    uZ = _[3].to(self.gpu_id)
                    outE, outN, outZ = self.model(a)
                    loss_rel_val = loss_criterion((outE,outN,outZ), (uE,uN,uZ), loss_weights, relative=True)
                    loss_abs_val = loss_criterion((outE,outN,outZ), (uE,uN,uZ), loss_weights, relative=False)

                    val_losses_relative.update(loss_rel_val.item(), get_batch_size(a))
                    val_losses_absolute.update(loss_abs_val.item(), get_batch_size(a))

                val_history['loss_relative'].append(val_losses_relative.avg)
                val_history['loss_absolute'].append(val_losses_absolute.avg)
                
                lr_scheduler.step(val_losses_relative.avg)

                t2 = timeit.default_timer()
                print(f'Epoch {ep+1}/{epochs}: {t2-t1:.2f}s - Training loss = {train_losses_relative.avg:.5f} - Validation loss = {val_losses_relative.avg:.5f}'\
                    f' - Training accuracy = {train_losses_absolute.avg:.5f} - Validation accuracy = {val_losses_absolute.avg:.5f}')

                # save the model
                if val_losses_relative.avg < best_loss and self.gpu_id == 0:
                    best_loss = val_losses_relative.avg
                    ckp = self.model.module.state_dict() #DDP
                    torch.save(ckp, './logs/models/bestmodel-'+name_config+f'-epochs{epochs}.pt')
                
                if early_stopper.early_stop(val_losses_relative.avg):
                    break

                # save intermediate losses
                if ep%2==0 and self.gpu_id == 0:
                    with h5py.File(f'./logs/loss/loss-{name_config}-epoch{ep}on{epochs}.h5', 'w') as f:
                        f.create_dataset('train_loss_relative', data=train_history['loss_relative'])
                        f.create_dataset('train_loss_absolute', data=train_history['loss_absolute'])
                        f.create_dataset('val_loss_relative', data=val_history['loss_relative'])
                        f.create_dataset('val_loss_absolute', data=val_history['loss_absolute'])

                    # remove the previous losses saved
                    if ep>2:
                        os.remove(f'./logs/loss/loss-{name_config}-epoch{ep-2}on{epochs}.h5')

                    last_epoch_saved = ep # to remove the last intermediate save at the end

                    self._save_snapshot(ep)
    
        if self.gpu_id == 0:
            # save the final loss
            with h5py.File(f'./logs/loss/loss-{name_config}-epochs{ep+1}.h5', 'w') as f:
                f.create_dataset('train_loss_relative', data=train_history['loss_relative'])
                f.create_dataset('train_loss_absolute', data=train_history['loss_absolute'])
                f.create_dataset('val_loss_relative', data=val_history['loss_relative'])
                f.create_dataset('val_loss_absolute', data=val_history['loss_absolute'])

            os.remove(f'./logs/loss/loss-{name_config}-epoch{last_epoch_saved}on{epochs}.h5')
            return last_epoch_saved

def load_train_objs():
    train_set = GeologyTracesDataset(path_data, options['dir_data_train'], S_in=S_in, S_in_z=S_in_z, S_out=S_out, T_out=T_out,
                                      transform_a='normal', N=Ntrain)  # load dataset
    val_set = GeologyTracesDataset(path_data, options['dir_data_val'], S_in=S_in, S_in_z=S_in_z, S_out=S_out, T_out=T_out,
                                    transform_a='normal', N=Nval)
    
    list_D1 = np.array(options['list_D1']).astype(int)
    list_D2 = np.array(options['list_D2']).astype(int)
    list_D3 = np.array(options['list_D3']).astype(int)
    list_M1 = np.array(options['list_M1']).astype(int)
    list_M2 = np.array(options['list_M2']).astype(int)
    list_M3 = np.array(options['list_M3']).astype(int)
    nlayers = options['nlayers']

    assert nlayers == list_D1.shape[0]
    
    model = FFNO_3D(list_D1, list_D2, list_D3,
                    list_M1, list_M2, list_M3, dv, 
                    input_dim=4, # to define the uplift network (last dimension after grid concatenation)
                    output_dim=1, # to define the projection network (last dimension after projection)
                    n_layers=nlayers,
                    padding = 0
    )  # load model

    name_config = f"FFNO3D-dv{dv}-{options['nlayers']}layers-S{S_in}-T{T_out}-padding{padding}-learningrate{str(learning_rate).replace('.','p')}-" \
        f"L1loss{str(loss_weights[0]).replace('.','p')}-L2loss{str(loss_weights[1]).replace('.','p')}-"
    name_config += f"Ntrain{Ntrain}-batchsize{batch_size}"
    name_config += options['additional_name']
    
    optimizer = optim.Adam(model.parameters(), lr=learning_rate, betas=(0.9, 0.999))
    return train_set, model, optimizer, val_set, name_config


def prepare_dataloader(dataset: Dataset, batch_size: int):
    return DataLoader(
        dataset,
        batch_size=batch_size,
        pin_memory=True,
        shuffle=False,
        sampler=DistributedSampler(dataset)
    )

def main(snapshot_path: str = "snapshot.pt"):
    ddp_setup()
    dataset, model, optimizer, valset, nameConfig = load_train_objs()
    train_data = prepare_dataloader(dataset, batch_size)
    val_data =prepare_dataloader(valset, batch_size)
    trainer = Trainer(model, train_data, optimizer, val_data, snapshot_path)
    trainer.train(nameConfig)
    destroy_process_group()

if __name__ == '__main__':
    main()