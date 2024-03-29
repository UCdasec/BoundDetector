'''
    New model using sliding window size of bytes, and labeling 
    that window of bytes as a function start or not
'''
import json
import lief
import hashlib
import math
import shutil
import subprocess
import os
import json
import time
from lightning.pytorch.cli import LightningCLI

from lightning.pytorch.utilities.types import STEP_OUTPUT

from torchmetrics.classification import (
    BinaryF1Score, BinaryPrecision, BinaryRecall,
)
import sys
import random
import torch 
import numpy as np
import torch.nn as nn
import torch.optim as optim
from pathlib import Path
from torch.utils.data import Dataset, DataLoader
from alive_progress import alive_bar, alive_it
from torchinfo import summary
import typer
from rich.console import Console
from typing_extensions import Annotated

from models import recreatedModel

from pytorch_lightning.callbacks import ModelCheckpoint

import multiprocessing 
CPU_COUNT = multiprocessing.cpu_count()

from ripkit.ripbin import (
    ConfusionMatrix,
    lief_gnd_truth,
    get_functions,
    generate_minimal_labeled_features,
    generate_features,
    #get_functions,
    #new_file_super_careful_callback,
    new_file_callback,
    #must_be_file_callback,
    iterable_path_shallow_callback,
    iterable_path_deep_callback,
    calc_metrics,
)

import lightning.pytorch as pylight


# This is not the bi-directional RNN itself,
# But rather a wrapper to train it 
class lit(pylight.LightningModule):
    def __init__(self, classifier:nn.Module, 
                 loss_func: nn.modules.loss._Loss, 
                 learning_rate: int,
                 hidden_size: int,
                 input_size: int,
                 num_layers:int,
                 threshold: float =.9)->None:
        super().__init__()
        self.classifier = classifier
        self.loss_func = loss_func
        self.lr = learning_rate
        self.threshold = threshold
        #self.save_hyperparameters(ignore=['loss_func'])

        self.metrics = [
            BinaryF1Score(threshold=threshold),
            BinaryPrecision(threshold=threshold),
            BinaryRecall(threshold=threshold),
        ]
        self.hidden_size = hidden_size
        self.num_layers = num_layers
        self.rnn = nn.RNN(input_size, hidden_size, num_layers, 
                          batch_first=True, bidirectional=True,
                          nonlinearity='relu')
                          #nonlinearity='gru')
        #self.rnn = nn.GRU(input_size, hidden_size, num_layers, 
        #                  batch_first=True, bidirectional=True)
        self.fc = nn.Linear(hidden_size*2, 1)
        self.sigmoid = nn.Sigmoid()
        self.softmax = nn.Softmax()
        self.relu = nn.ReLU()

    #def forward(self, x)->torch.Tensor:
    #    '''
    #        Forward method
    #    '''
    #    print(x)
    #    param1 = self.num_layers * 2 
    #    param2 = x.size(0)
    #    param3 = self.hidden_size
    #    #h0 = torch.zeros(self.num_layers * 2, x.size(0), 
    #    #                 self.hidden_size).to(x.device)
    #    h0 = torch.zeros(param1, param2, 
    #                     param3).to(x.device)
    #    out, _ = self.rnn(x, h0)
    #    out = self.fc(out)
    #    out = out.squeeze(dim=2)
    #    out = torch.nan_to_num(self.sigmoid(out))
    #    return out



    def reset_metrics(self)->None:
        self.metrics = [
            BinaryF1Score(threshold=self.threshold),
            BinaryPrecision(threshold=self.threshold),
            BinaryRecall(threshold=self.threshold),
        ]

    
    def training_step(self, batch, batch_idx)->torch.Tensor:
        # Trianing loop
        inp_batch, target = batch

        out_tensor = self.classifier(inp_batch)

        # Calc loss
        loss = self.loss_func(out_tensor, target)
        self.log("Train_Loss", loss, prog_bar=True)
        return loss

    def validation_step(self,batch, batch_idx)->STEP_OUTPUT:
        # Trianing loop
        inp_batch, target = batch

        out_tensor = self.classifier(inp_batch)

        # Calc loss
        loss = self.loss_func(out_tensor, target)
        self.log("Valid_Loss", loss)

    def configure_optimizers(self)->optim.Optimizer:
        optimizer = optim.RMSprop(self.parameters(), lr=self.lr)
        return optimizer

    def predict_step(self, batch, batch_idx)->STEP_OUTPUT:

        inp_batch, target = batch

        out_tensor = self.classifier(inp_batch)
        return out_tensor

    def test_step(self, batch, batch_idx)->STEP_OUTPUT:
        # Trianing loop
        inp_batch, target = batch

        out_tensor = self.classifier(inp_batch)

        # Calc loss
        loss = self.loss_func(out_tensor, target)

        self.log("Test_Loss", loss)

        for func in self.metrics:
            # BUG: There is an error in this function, a claim that 
            #       target has values that are non [0 or 1]
            func.update(out_tensor.to('cpu'),
                          target.to('cpu'))


# Custom dataset class
class MyDataset(Dataset):
    def __init__(self, data, target):
        self.data = data
        self.target = target

    def __len__(self):
        return len(self.data)

    def __getitem__(self, index):
        inp = self.data[index]
        target = self.target[index]
        return inp, target


DEVICE = torch.device("cuda" if torch.cuda.is_available() else "cpu")

console = Console()
app = typer.Typer(pretty_exceptions_show_locals=False)

def test_model( model, dataloader, metrics = 
               [BinaryF1Score, BinaryPrecision, BinaryRecall]):
    '''
    test the model

    Returns
    -------

    list :
        List of the passed metrics
    '''

    model.eval()
    with torch.no_grad():
        bar = alive_it(dataloader, theme='scuba', 
                        title = "Evaluationg")

        for inp_batch, target in bar:
            output_tensor = model(inp_batch)

            for func in metrics:
                func.update(output_tensor.squeeze().to('cpu'),
                          target.squeeze().to('cpu'))

    return metrics 

def vsplit_dataset(inpAr, split: tuple[int,int,int]):
    '''
    Split passed matrix for train validate test 

    Parameteres 
    -----------

    inpAr: np.ndarray
        the dataset

    split: tuple[int,int,int]
        the split, must add to 100
    '''

    if sum(split) != 100:
        raise Exception("Split error, must add to 100")

    
    # Get the total rows so I know what 100 percent is 
    tot_rows = inpAr.shape[0]

    # The test index is 0% - splot[0]%
    test_index = tot_rows // 100 * split[0]

    # The validate index is split[0]% to split[1]%
    validate_index = test_index + tot_rows // 100 * split[1]

    # The test index is the rest
    return (inpAr[:test_index,:], 
            inpAr[test_index: validate_index,:], 
            inpAr[validate_index:, :]
            )


def gen_one_hot_dataset_ENDS(files: list[Path], num_chunks):

    # TODO: Bools instead?
    # BUG: Maybe - when I switch to np.bool_ the performance 
    #       drops DRASTICALLY
    inp_array = np.empty((num_chunks,1000,255), dtype=np.float64)
    label_arr = np.empty((num_chunks,1000, 1), dtype=np.float64)

    #inp_array = np.empty((num_chunks,1000,255), dtype=np.bool_)
    #label_arr = np.empty((num_chunks,1000, 1), dtype=np.bool_)


    data_info = []

    with alive_bar(num_chunks, title="Generating dataset") as bar:
    #for i in alive_it(range(num_chunks), title="Generating dataset"):
        for _ in range(num_chunks):

            # Randomly select a file from the glob
            selected_file = np.random.choice(files)

            # Load the selected .npz file
            with np.load(selected_file, mmap_mode='r', 
                         allow_pickle=True) as npz_file:
                # Get the first (and only) file array that is in the 
                # npz file
                data = npz_file[npz_file.files[0]]
    
                # Randomly select a 1000-row chunk from the 'data' array

                # Randomly select an index for data start
            
                if data.shape[0] < 1001:
                    continue

                index =np.random.randint(0, data.shape[0]-1000-1)
                chunk = data[index: index+1000, :]

                # the chunks is forward
                # start, mid, end, *byte
                inp_array[i,:,:] = chunk[:,3:]
                label_arr[i,:,:] = chunk[:,2].reshape((1000,1))
                data_info.append((selected_file, index))
                bar()

    return inp_array, label_arr


def single_file_test_dataloader_bounds(input_bin: Path, min_func_len:int):

    # Features are: state? middlee? end? byte.................
    labeled_data = np.array(list(generate_features(input_bin, min_func_len)))

    # Need to chunk the bin into 1000,1 arrays 
    # This will result in a shape of (len(labeled_data)/1000 , 1000, 1 ) shape
    num_chunks = math.floor(labeled_data.shape[0] / 1000)
    extra_chunks = labeled_data.shape[0] - (num_chunks*1000)
    labeled_data = labeled_data[:-extra_chunks]

    inp_array = np.empty((num_chunks,1000,255), dtype=np.float64)
    start_label_arr = np.empty((num_chunks,1000, 1), dtype=np.float64)
    end_label_arr = np.empty((num_chunks,1000, 1), dtype=np.float64)

    for i in range(num_chunks):
        chunk = labeled_data[i:i+1000,:]

        # every thing after the first 3 columns
        inp_array[i,:,:] = chunk[:,3:]

        # The lbl array is the first column which is TRUE or FALSE, denoting 
        # whether or not the the bytes is a function start
        start_label_arr[i,:,:] = chunk[:,0].reshape((1000,1))
        end_label_arr[i,:,:] = chunk[:,2].reshape((1000,1))



    start_dataset = MyDataset(torch.Tensor(inp_array).to(DEVICE), 
                     torch.Tensor(start_label_arr).to(DEVICE))
    end_dataset = MyDataset(torch.Tensor(inp_array).to(DEVICE), 
                     torch.Tensor(end_label_arr).to(DEVICE))

    start_loader = DataLoader(start_dataset, batch_size=16, 
                                shuffle=False,
                                drop_last=True) #, num_workers=CPU_COUNT)
    end_loader = DataLoader(end_dataset, batch_size=16, 
                                shuffle=False,
                                drop_last=True) #, num_workers=CPU_COUNT)
    return start_loader, end_loader


def gen_one_hot_dataset(files: list[Path], num_chunks):

    #       drops DRASTICALLY
    inp_array = np.empty((num_chunks,1000,255), dtype=np.float64)
    label_arr = np.empty((num_chunks,1000, 1), dtype=np.float64)

    # 3d numpy array. Each chunk is 1000x255 and I have <num_chunks> chunks
    #inp_array = np.empty((num_chunks,1000,255), dtype=np.bool_)
    #label_arr = np.empty((num_chunks,1000, 1), dtype=np.bool_)

    data_info = []

    # Sanity check that all files exist TODO: This should be redundant and ultimately removed
    #files = [x for x in files if x.exists()]

    # This is how many chunks per file to get, notice if a file is not 
    # long enough for this number of chunks, other files will just be 
    # revisited
    about_chunks_per_file = num_chunks / len(files)


    # This loop will iterate num_chunks time over index
    # 0 : num_chunks-1 
    # It will select 1 chunk from one file then move on to the next
    data_index = 0
    #for i in alive_it(range(num_chunks), title="Generating dataset"):

    with alive_bar(num_chunks, title="Generating dataset") as bar:
        for _ in range(num_chunks):
    #for i in alive_it(range(num_chunks), title="Generating dataset"):
            good_file = False

            # Randomly select a file from the glob
            selected_file = np.random.choice(files)

            # Load files until a file atleast 1000 bytes long 
            npz_file_data = np.array([])
            while not good_file:
                selected_file = np.random.choice(files)
                # Load the selected .npz file
                with np.load(selected_file, mmap_mode='r', 
                             allow_pickle=True) as npz_file:

                    # Get the first (and only) file array that is in the npz file
                    npz_file_data = npz_file[npz_file.files[0]]
    
                    # If the npz_file_data array is not large enough to be a chunk, go to 
                    # the next loop iteration
                    if npz_file_data.shape[0] >= 1001:
                        good_file = True
                    else:
                        print(f"File {selected_file} too short")
                        break

            # TODO: Remove for loop to return to old, and unindent lines by 1
            for _ in range(int(about_chunks_per_file)):

                # Select an index
                # The greatest_index is matrix.shape - 1 - 1000
                rand_index =np.random.randint(0, npz_file_data.shape[0]-1000-1)

                # The chunk is a 2d matrix 
                # where the columns are [start,middle,end,*byte]
                chunk = npz_file_data[rand_index: rand_index+1000, :]

                # The input array, or the onehot array is the one-hot bytes, which is 
                # every thing after the first 3 columns
                inp_array[data_index,:,:] = chunk[:,3:]

                # The lbl array is the first column which is TRUE or FALSE, denoting 
                # whether or not the the bytes is a function start
                label_arr[data_index,:,:] = chunk[:,0].reshape((1000,1))
                #inp_array[i,:,:] = chunk[:,3:]
                #label_arr[i,:,:] = chunk[:,0].reshape((1000,1))

                data_index += 1
                bar()

                data_info.append((selected_file, data_index))
                if data_index >= 1000:
                    # We're done break 
                    return inp_array, label_arr
                


    return inp_array, label_arr

def chunk_generator(inp_array, lbl_array, num_chunks, labeled_data, ends:bool):
    '''

    Lazily yield the the chunks of data so I don't blow up anythings memory
    '''
    for i in range(num_chunks):
        # Get chunk data
        chunk_data = labeled_data[i:i+1000,3:]
        inp_array[i] = chunk_data
        if ends:
            #lbl_array[i] = labeled_data[i:i+1000,2].reshape((1000,1))
            yield chunk_data, labeled_data[i:i+1000,2].reshape((1000,1))
        else:
            #lbl_array[i]= labeled_data[i:i+1000,0].reshape((1000,1))
            yield chunk_data, labeled_data[i:i+1000,0].reshape((1000,1))



def single_bin_dataloader(
        input_bin:Path,
        min_func_len: int,
        batch_size: int,
        ends=False,
    ):
    '''
    Create a dataloader for test files input
    '''

    # Features are: state? middlee? end? byte.................
    labeled_data = np.array(list(generate_features(input_bin, min_func_len)))

    # Need to chunk the bin into 1000,1 arrays 
    # This will result in a shape of (len(labeled_data)/1000 , 1000, 1 ) shape
    num_chunks = math.floor(labeled_data.shape[0] / 1000)
    extra_chunks = labeled_data.shape[0] - (num_chunks*1000)
    labeled_data = labeled_data[:-extra_chunks]

    print(f"Using {num_chunks} of 1000 for {labeled_data.shape[0]} bytes")
    #print(f"Gile: {input_bin}: {labeled_data.shape[0]}")
    #print(f"We are then lossing{labeled_data.shape[0] - num_chunks*1000} bytes")

    inp_array = np.empty((num_chunks,1000,255))#, dtype=np.float32)
    lbl_array = np.empty((num_chunks,1000,1))#, dtype=np.float32)

    for i in range(num_chunks):
        # Get chunk data
        # Get 1000 one hot encoded bytes
        chunk_data = labeled_data[i:i+1000,3:]
        inp_array[i] = chunk_data

        if ends:
            lbl_array[i] = labeled_data[i:i+1000,2].reshape((1000,1))
            #lbl_array[i] = labeled_data[i:i+1000,2].squeeze()
        else:
            lbl_array[i]= labeled_data[i:i+1000,0].reshape((1000,1))
            #lbl_array[i]= labeled_data[i:i+1000,0].squeeze()

    ones_count = np.sum(lbl_array[:,:,0] == 1)
    print(ones_count)

    test_dataset = MyDataset(torch.Tensor(inp_array).to(DEVICE), 
                     torch.Tensor(lbl_array).to(DEVICE))
    #test_dataset = MyDataset(torch.Tensor(inp_array), 
    #                 torch.Tensor(lbl_array).squeeze())

    test_dataloader = DataLoader(test_dataset, batch_size=16, 
                                shuffle=False,
                                drop_last=True) #, num_workers=CPU_COUNT)
    return test_dataloader

def create_dataloaders(
        input_files,
        cache_file = Path(".DEFAULT_CACHE"),
        num_chunks = 1000,
        ends=False,
    ):
    '''
    Generate dataloaders 
    '''

    #print(f"Cache file {cache_file} does not exist")
    #if cache_file.exists():
    #    data = np.empty((0,0))
    #    lbl = np.empty((0,0))
    #    with np.load(cache_file, mmap_mode='r') as f:
    #        data = f['data']
    #        lbl = f['lbl']
    #else:
        # Get the data set
    if ends:

        data, lbl= gen_one_hot_dataset_ENDS(input_files,num_chunks=num_chunks)
    else:
        data, lbl= gen_one_hot_dataset(input_files,num_chunks=num_chunks)

    #np.savez(cache_file, data=data, lbl=lbl)

    # Get split the dataset
    train_data, valid_data, test_data = vsplit_dataset(data,
                                                    (80,10,10)) 
    train_lbl, valid_lbl, test_lbl = vsplit_dataset(lbl,
                                                    (80,10,10)) 

    # Get the training dataset
    train_dataset = MyDataset(torch.Tensor(train_data).to(DEVICE), 
                     torch.Tensor(train_lbl).squeeze().to(DEVICE))
    valid_dataset = MyDataset(torch.Tensor(valid_data).to(DEVICE), 
                     torch.Tensor(valid_lbl).squeeze().to(DEVICE))
    test_dataset = MyDataset(torch.Tensor(test_data).to(DEVICE), 
                     torch.Tensor(test_lbl).squeeze().to(DEVICE))


    # Get the dataloader
    # BUG: Using num workers (maybe) introduced more CUDA init errors
    train_dataloader = DataLoader(train_dataset, batch_size=32, 
                                shuffle=False,
                                drop_last=True)#, num_workers=CPU_COUNT)
    valid_dataloader = DataLoader(valid_dataset, batch_size=32, 
                                shuffle=False,
                                drop_last=True) #,num_workers=CPU_COUNT)
    test_dataloader = DataLoader(test_dataset, batch_size=32, 
                                shuffle=False,
                                drop_last=True) #, num_workers=CPU_COUNT)

    return train_dataloader, valid_dataloader, test_dataloader


def lit_model_train(input_files, 
                    #cache_file = Path(".DEFAULT_CACHE"),
                    print_summary=False,
                    learning_rate = 0.0005,
                    input_size=255,
                    hidden_size=16,
                    layers=1,
                    epochs=100,
                    threshold=.9,
                    ends=False,
                    checkpoint_dir=""):
    
    '''
    Train RNN model
    '''

    cache_file = Path(".lit_cache_dataset.npz")

    # Init the model
    model = recreatedModel(input_size, hidden_size, layers)

    # Binary cross entrophy loss
    loss_func = nn.BCELoss()

    classifier = lit(model,
                     loss_func=loss_func,
                     learning_rate=learning_rate,
                     input_size=input_size,
                     hidden_size=hidden_size,
                     num_layers=layers)

    train_loader, valid_loader, test_loader = create_dataloaders(
            input_files, cache_file=cache_file,ends=False)

    # Summary of the model 
    if print_summary:
        summary(model, (32,1000,255))

    if checkpoint_dir != "":
        trainer = pylight.Trainer(max_epochs=100)
    else:
        trainer = pylight.Trainer(max_epochs=100,default_root_dir=checkpoint_dir)
    
    trainer.fit(classifier, train_loader, valid_loader)

    # TODO: Get metrics out of the test
    trainer.test(classifier, dataloaders=test_loader)

    return classifier.metrics, classifier


def rnn_predict_raw(model, unstripped_bin:Path, min_func_len:int, ends: bool):
    '''
    Use the model to predict for the passed binary 
    '''

    print("building dataloader")
    # 1. Create dataloader for the single binary 
    bin_dataloader = single_bin_dataloader(unstripped_bin, min_func_len, batch_size=32, ends=ends)
    print('dataloader built')

    # 2. Use the model to predict
    trainer = pylight.Trainer()#precision="16-mixed")

    res = trainer.predict(model,dataloaders=bin_dataloader)
    res = [x.detach().cpu() for x in res]
    return res


#def rnn_predict_bounds(model_start, model_end, unstripped_bin, threshold: float):
def rnn_predict_bounds(model, unstripped_bin, threshold: float, start):
    # the rnn can predict chunks of 1000 bytes 

    #start_conf = ConfusionMatrix(0,0,0,0)
    conf = ConfusionMatrix(0,0,0,0)
    end_conf = ConfusionMatrix(0,0,0,0)
    bound_conf = ConfusionMatrix(0,0,0,0)

    data_gen = generate_minimal_labeled_features(unstripped_bin)
    functions = get_functions(unstripped_bin)
    #func_start_addrs = {x.addr : (x.name, x.size) for x in functions}
    #func_start_addrs = [x.arr for x in functions]
    #func_end_addrs = [x.addr + x.size for x in functions]
    #TODO: Make ground truth be:
    #       func_start_addrs = []
    #       func_end_addrs = []
    # Then iterate twice, each time with a single model loaded.
    # Store the predict start addrs
    # Start the predicted end addrs 
    # Get the indiviauls truth matrices 
    # Then get the bound matrices

    inp_chunk = []
    lbl_start = []
    lbl_end = []

    # Instead of saving all the bytes labels, I am going to 
    # save a list of address.
    # One list of start addresses
    # One list of end addresses 
    # This is not ideal, but is more effecient than saving 
    # massive np arrays

    parsed_bin = lief.parse(str(unstripped_bin.resolve()))
    text_section = parsed_bin.get_section(".text")

    # Get the bytes in the .text section
    text_bytes = text_section.content

    # Get the base address of the loaded binary
    base_address = parsed_bin.imagebase


    start_addrs = []
    end_addrs = []
    lbl_chunk = []
    addrs = [] 

    #for row in data_gen:
    # each row is: Start?, Middle?, End?, one_hot_byte
    for i, row in enumerate(data_gen):
        base_address = base_address + text_section.virtual_address + i

        # Index | meaning
        # 0     | is_start
        # 1     | is_middle
        # 2     | is_end 
        # 3:    | one_hot_encoded value
        if start:
            lbl_chunk.append(row[0])
        else:
            lbl_chunk.append(row[2])

        #lbl_start.append(row[0])
        #lbl_end.append(row[1])
        inp_chunk.append(row[3:])
        
        # NOTICE: Feed the model with 1000 bytes
        if len(inp_chunk) == 1000:
            # make numpy matrix for this chunk
            #lbl_start = np.array(np.array(lbl_start))
            #lbl_end = np.array(np.array(lbl_end))
            lbl= np.array(np.array(lbl_chunk))
            inp = np.array([inp_chunk])

            # Get the prediction from the model 
            with torch.no_grad():
                #prediction_start = model(torch.Tensor(inp).to(DEVICE))
                prediction = model(torch.Tensor(inp).to(DEVICE))
                #prediction_end = model_end(torch.Tensor(inp).to(DEVICE))

            # Score the prediction
            prediction = prediction.squeeze().to('cpu').numpy()
            prediction[prediction >= threshold] = 1
            prediction[prediction < threshold] = 0
            target = lbl.squeeze()

            indices = np.where(prediction == 1)[0]
            addr = indices + base_address
            #start_addrs.append(start_addr)
            addrs.extend(addr)



            # Score the prediction_start
            #prediction_start = prediction_start.squeeze().to('cpu').numpy()
            #prediction_start[prediction_start >= threshold] = 1
            #prediction_start[prediction_start < threshold] = 0
            #target = lbl_start.squeeze()
            #start_indices = np.where(prediction_start == 1)[0]
            #start_addr = start_indices + base_address
            ##start_addrs.append(start_addr)
            #start_addrs.extend(start_addr)

            # Score the prediction_end
            #prediction_end = prediction_end.squeeze().to('cpu').numpy()
            #prediction_end[prediction_end >= threshold] = 1
            #prediction_end[prediction_end < threshold] = 0
            #target = lbl_end.squeeze()
            #end_addr = np.where(prediction_end == 1)[0]
            #end_addr = end_addr + base_address
            ##end_addrs.append(end_addr)
            #end_addrs.extend(start_addr)

            # Some each of the cases. The .item() changed the type from
            # np.int64 to int
            #if starts:
            conf.tp += np.sum((prediction == 1) & (target == 1)).item()
            conf.tn += np.sum((prediction == 0) & (target == 0)).item()
            conf.fp += np.sum((prediction == 1) & (target == 0)).item()
            conf.fn += np.sum((prediction == 0) & (target == 1)).item()

            #else:
            #end_conf.tp += np.sum((prediction_end == 1) & (target == 1)).item()
            #end_conf.tn += np.sum((prediction_end == 0) & (target == 0)).item()
            #end_conf.fp += np.sum((prediction_end == 1) & (target == 0)).item()
            #end_conf.fn += np.sum((prediction_end == 0) & (target == 1)).item()

            # Reset the chunk
            lbl_start = []
            lbl_end = []
            inp_chunk = []
            lbl_chunk = []


    #return start_conf, end_conf, start_addrs, end_addrs
    return conf, addrs



def rnn_predict(model, unstripped_bin, threshold: float):
    # the rnn can predict chunks of 1000 bytes 

    tp = 0
    tn = 0
    fp = 0
    fn = 0

    data_gen = generate_minimal_labeled_features(unstripped_bin)
    inp_chunk = []
    lbl_chunk = []

    # Recored start time
    start = time.time()

    #for row in data_gen:
    # each row is: Start?, Middle?, End?, one_hot_byte
    for row in data_gen:
        # Index | meaning
        # 0     | is_start
        # 1     | is_middle
        # 2     | is_end 
        # 3:    | one_hot_encoded value
        lbl_chunk.append(row[0])
        inp_chunk.append(row[3:])
        
        # NOTICE: Feed the model with 1000 bytes

        if len(lbl_chunk) == 1000:
            # make numpy matrix for this chunk
            lbl = np.array(np.array(lbl_chunk))
            inp = np.array([inp_chunk])

            # Get the prediction from the model 
            with torch.no_grad():
                prediction = model(torch.Tensor(inp).to(DEVICE))

            # Reset the chunk
            lbl_chunk = []
            inp_chunk = []

            # Score the prediction
            prediction = prediction.squeeze().to('cpu').numpy()
            prediction[prediction >= threshold] = 1
            prediction[prediction < threshold] = 0
            target = lbl.squeeze()

            # Some each of the cases. The .item() changed the type from
            # np.int64 to int
            tp += np.sum((prediction == 1) & (target == 1)).item()
            tn += np.sum((prediction == 0) & (target == 0)).item()
            fp += np.sum((prediction == 1) & (target == 0)).item()
            fn += np.sum((prediction == 0) & (target == 1)).item()

    runtime = time.time() - start

    return tp, tn, fp, fn, runtime 

@app.command()
def gen_unified_train(
        inp_dir: Annotated[str, typer.Argument(
                                    help='Directory of bings to train on')],
    ):
    '''
    Get files from each opt level to train on 
    5 opt levels, 20 files each
    Conviently, this is training on the whole small dataset
    '''

    train_files = []
    for maybe_file in Path(inp_dir).rglob('*'):
        if maybe_file.is_file():
            train_files.append(maybe_file)


    # Generate the npz files for them
    if len(train_files) != 100:
        print("should have 100")
        return

    # Train on these files
    metrics_starts, classifier_starts = lit_model_train(train_files)
    print([x.compute() for x in metrics_starts])
    metrics_ends, classifier_ends = lit_model_train(train_files, ends=True)
    print([x.compute() for x in metrics_ends])
    return



@app.command()
def gen_npzs(bin_path: Annotated[str, typer.Argument()],
             out_dir: Annotated[str, typer.Argument()],
             sample: Annotated[int, typer.Option()] = 0,
            ):
    '''
    Generate npz files
    '''

    bins_path = Path(bin_path)
    out_path = Path(out_dir)

    if not out_path.exists():
        out_path.mkdir()

    if bins_path.is_dir():
        #TODO: This is best the first 100 are used for training
        bins = list(bins_path.glob('*'))[:100]
        if sample != 0 :
            # Sample sample bins
            bins = random.sample(bins,sample)

    elif bins_path.is_file() and sample == 0:
        bins = [bins_path]
    else:
        print(f"Does not exist {bins_path}")
        return


    for binary in alive_it(bins):

        # Generate analysis
        print("Generating Tensors...")
        data = np.array(list(generate_minimal_labeled_features(binary)))

        data_file = out_path / binary.name
        index = 1
        while data_file.exists():
            data_file = out_path / Path(f"{binary.name}_{index}")
            index+=1

        print("Saving Tensor and binary")
        try:
            # Save the analysis to file 
            np.savez_compressed(data_file , data=data)
        except Exception as e:
            raise Exception("Error")

    print(f"Saved {len(bins)} new npzs")
    return



@app.command()
def train_on(
        inp_dir: Annotated[str, typer.Argument(
                                    help='Directory of bins to train on')],
        base_checkpoint_dir: Annotated[str, typer.Option()]="",
    ):

    base_check=False
    if base_checkpoint_dir != "":
        checkpoints_start = Path(base_checkpoint_dir).joinpath("starts")
        checkpoints_end= Path(base_checkpoint_dir).joinpath("ends")
        checkpoints_start.mkdir(parents=True,exist_ok=True)
        checkpoints_end.mkdir(parents=True,exist_ok=True)
        base_check=True


    # Get the files from the input path
    #train_files = [f"{x." for x  in Path(inp_dir).rglob('*') ]
    train_files = [ x for x in Path(inp_dir).rglob('*') if x.is_file()]

    # Train files must be the npz version of the files...


    # Train on these files
    if base_check:
        metrics, classifier = lit_model_train(train_files,checkpoint_dir=checkpoints_start)
    else:
        metrics, classifier = lit_model_train(train_files)
    print(f"Starts: ")
    print([x.compute() for x in metrics])

    if base_check:
        metrics_ends, classifier_ends = lit_model_train(train_files,checkpoint_dir=checkpoints_end,ends=True)
    else:
        metrics_ends, classifier_ends = lit_model_train(train_files,ends=True)

    print(f"Ends: ")
    print([x.compute() for x in metrics_ends])
    return


#@app.command()
#def train_on_first(
#        opt_lvl: Annotated[str, typer.Argument(
#                        help='Directory of bins to test on')],
#
#        num_bins: Annotated[int, typer.Argument(
#                        help='Num bins to test on')],
#        ends: Annotated[bool, typer.Option(
#                        help='Num bins to test on')]=False,
#    ):
#
#    opts = ['O0','O1','O2','O3','Z','S']
#
#    # TODO: verify this is how Oz and Os are lbls
#    if opt_lvl not in opts:
#        print(f"opt lbl must be in {opts}")
#        return
#
#    ##TODO: This is best used when I have large similar datasets for O0-Oz
#    ##       until I have all of those compiled I will manually split
#    ##with open("TEST_BIN_NAME_SET.json", 'r') as f:
#    ##    bin_names = json.load(f)['names']
#
#    ## 
#    rust_files = []
#
#    for parent in Path("/home/ryan/.ripbin/ripped_bins/").iterdir():
#        info_file = parent / 'info.json'
#        info = {}
#        try:
#            with open(info_file, 'r') as f:
#                info = json.load(f)
#        except FileNotFoundError:
#            print(f"File not found: {info_file}")
#            continue
#        except json.JSONDecodeError as e:
#            print(f"JSON decoding error: {e}")
#            continue
#        except Exception as e:
#            print(f"An error occurred: {e}")
#            continue
#
#
#        if info['optimization'].upper() in opt_lvl.upper():
#
#            npz_file =  parent / 'one_hot_plus_func_labels.npz'
#
#            if npz_file.exists():
#                rust_files.append(npz_file)
#
#    # Get the first x files
#    if len(rust_files) < num_bins:
#        print(f"Num bins too low {rust_files}")
#        return
#
#    # Get the first x files
#    first_x_files = rust_files[0:num_bins]
#
#    with open(f"TRAINED_FILESET_{opt_lvl}.txt", 'w') as f:
#        f.write("\n".join(x.name for x in first_x_files))
#        
#
#    # Train on these files
#    metrics, classifier = lit_model_train(first_x_files, ends=ends)
#    print([x.compute() for x in metrics])
#
#    return


#@app.command()
#def train_without(
#        opt_lvl: Annotated[str, typer.Argument(
#                        help='Directory of bins to test on')],
#        testset: Annotated[str, typer.Argument(
#                        help='Directory of bins to test on')],
#    ):
#    opts = ['O0','O1','O2','O3','Z','S']
#
#    # TODO: verify this is how Oz and Os are lbls
#    if opt_lvl not in opts:
#        print(f"opt lbl must be in {opts}")
#        return
#
#    test_path = Path(testset).resolve()
#    if not test_path.exists():
#        print(f"PAth {test_path} does not exist")
#
#    # Get the test files 
#    rust_test_files = [ x.name for x in test_path.glob('*')]
#
#    for parent in Path("/home/ryan/.ripbin/ripped_bins/").iterdir():
#        info_file = parent / 'info.json'
#        info = {}
#        try:
#            with open(info_file, 'r') as f:
#                info = json.load(f)
#        except FileNotFoundError:
#            print(f"File not found: {info_file}")
#            continue
#        except json.JSONDecodeError as e:
#            print(f"JSON decoding error: {e}")
#            continue
#        except Exception as e:
#            print(f"An error occurred: {e}")
#            continue
#
#
#        if info['optimization'].upper() in opt_lvl:
#            npz_file = parent / "onehot_plus_func_labels.npz"
#
#            if info['binary_name'] not in rust_test_files:
#                rust_train_files.append(npz_file)
#
#    # Get the classifier and the metrics from training 
#    metrics, classifier = lit_model_train(rust_train_files)
#    print([x.compute() for x in metrics])
#
#    # Create the pytorch tainer, will call the .test method on this 
#    # object to test the model 
#    trainer = pylight.Trainer(max_epochs=100)
#
#    classifier.reset_metrics()
#
#    # Get the run time of the module
#    start = time.time()
#    res = trainer.test(classifier,dataloaders=test_dataloader)
#    runtime = time.time() - start
#
#    # TODO: This is old code but I want to make sure its completely 
#    #        useless before I delete it 
#    #print(f"Test on {len(rust_test_files)}")
#    #metrics = [x.compute() for x in classifier.metrics]
#    #print(metrics)
#    #print(f"Run time for 1000 chunks on optimization {opt_lvl}: {runtime}")
#    #print(f"The len of train files was {len(rust_train_files)}")
#    #print(f"The len of test files was {len(rust_test_files)}")
#    return

#@app.command()
#def read_results(
#        file: Annotated[str, typer.Argument(
#                        help='json file of results')],
#    ):
#
#    # Create a path object for the file
#    file_path = Path(file)
#
#    # If the file doesn't exist return error
#    if not file_path.exists():
#        print(f"File {file} does not exist")
#        return
#
#    # Load the json file
#    with open(file, 'r') as f:
#        data = json.load(f)
#        # Data is made of 
#        #{
#        #    '<bin_name>' : {'tp' : ,
#        #                     'fp' : ,
#        #                     'tn' : ,
#        #                     'fn' : ,
#        #                    }
#        #}
#
#    # Sum all the confusion matrix values 
#    tp = sum(x['tp'] for x in data.values())
#    fp = sum(x['fp'] for x in data.values())
#    tn = sum(x['tn'] for x in data.values())
#    fn = sum(x['fn'] for x in data.values())
#    runtime = sum(x['runtime'] for x in data.values())
#    tot_file_size = sum(x['filesize'] for x in data.values())
#
#    # Recall 
#    recall = tp/ (tp + fn)
#
#    # Precision 
#    precision = tp / (tp + fp)
#
#    # F1
#    f1 = 2 * precision * recall / (precision+recall)
#
#    # File avg runtime
#    file_avg = runtime / len(data.values())
#
#    # Byte per second 
#    # TODO: This bps is per .text byte ...
#    #       talking with boyand this should change
#    bps = (tp+tn+fn+fp) / runtime
#
#    print(f"For {len(data.keys())} bins...")
#    print(f"TP : {tp}")
#    print(f"TN : {tn}")
#    print(f"FP : {fp}")
#    print(f"FN : {fn}")
#    print(f"F1 : {f1}")
#    print(f"Precision : {precision}")
#    print(f"Recall : {recall}")
#    print(f"Avg file time: {file_avg}")
#    print(f"BPS .text: {bps}")
#    print(f"BPS whole file: {tot_file_size/runtime}")
#
#    print(f"total file size: {tot_file_size}")
#
#
#    return


def strip_file(bin_path:Path)->Path:

    # Copy the bin and strip it 
    strip_bin = bin_path.parent / Path(bin_path.name + "_STRIPPED")
    strip_bin = strip_bin.resolve()
    shutil.copy(bin_path, Path(strip_bin))
    print(f"The new bin is at {strip_bin}")

    try:
        _ = subprocess.check_output(['strip',f'{strip_bin.resolve()}'])
    except subprocess.CalledProcessError as e:
        print("Error running command:", e)
        # TODO: Handle better
        raise Exception("Could not strip bin")

    return strip_bin 


def save_raw_prediction(raw_res:np.ndarray, out_file):

    #with open(out_file, 'w') as f:
    np.savez(out_file, raw_res)
    return


def calculate_md5(file_path, buffer_size=8192):
    '''
    Get the hash of a file. This is helpful for storing binaries of the same 
    names that were compiled with different flags / for different OSs 
    '''

    md5_hash = hashlib.md5()

    # Open, read, and take hash of file iterating over the buffers until
    # there's no more
    with open(file_path, 'rb') as file:
        buffer = file.read(buffer_size)
        while buffer:
            md5_hash.update(buffer)
            buffer = file.read(buffer_size)

    # Return the digest 
    return md5_hash.hexdigest()


def export_dataset_npzs(
        bin_names_dir: Annotated[str, typer.Argument(
                        help='Directory of bins to test on')],
        opt: Annotated[str, typer.Argument(
                        help='Directory of bins to test on')],
    ):
    """
    a hopefully temporary funcrion to grab binaries from ripbin db given names of optimization levels
    """


    bins_path = Path(bin_names_dir)
    if bins_path.is_dir():
        bins = list(bins_path,rglob('*'))
    elif bins_path.is_file():
        bins = [bins_path]
    else:
        print(f"Bins parh does not exist")
        return


    for bin in alive_it(bins):
        #hash the bin files
        bin_hash = calcculate_md5(bin)

        #look up the bin in ripkit


        #for parent in Path("/home/ryan/.ripbin/ripped_bins/").iterdir():
        #    info_file = parent / 'info.json'
        #    info = {}
        #    try:
        #        with open(info_file, 'r') as f:
        #            info = json.load(f)
        #    except FileNotFoundError:
        #        print(f"File not found: {info_file}")
        #        continue
        #    except json.JSONDecodeError as e:
        #        print(f"JSON decoding error: {e}")
        #        continue
        #    except Exception as e:
        #        print(f"An error occurred: {e}")
        #        continue


        if info['optimization'].upper() in OPTIMIZATION:
            npz_file = parent / "onehot_plus_func_labels.npz"





    return

@app.command()
def raw_test_bounds(
        testset_dir: Annotated[str, typer.Argument(
                        help='Directory of bins to test on')],
        start_weights: Annotated[str, typer.Argument(
                            help='Weight for start model')],
        end_weights: Annotated[str, typer.Argument(
                            help='Weights for end model')],
        out_dir: Annotated[str, typer.Argument(
                        help='Directory of bins to test on')],
        only_starts: Annotated[bool, typer.Option(
                        help='Directory of bins to test on')]=False,
        only_ends: Annotated[bool, typer.Option(
                        help='Directory of bins to test on')]=False,
        min_func_len: Annotated[int, typer.Option(
                        help='Directory of bins to test on')]=1,
    ):


    out_path = Path(out_dir)

    if not out_path.exists():
        out_path.mkdir()
    elif out_path.is_file():
        print("Out path is a file")
        return

    test_path = Path(testset_dir)
    if test_path.is_file():
        files = [test_path]
    else:
        files = list(Path(testset_dir).glob('*'))

    # Load the pytorch lightning model from the checkpoints
    #start_birnn_model = lit.load_from_checkpoint(start_weights)
    #end_birnn_model = lit.load_from_checkpoint(end_weights)

    if not only_ends:
        # Predict the starts and ends for the binaries
        for bin in files:

            out_file =  out_path.joinpath(bin.name + "_starts_result.npz")
            if out_file.exists():
                continue

            # Load the pytorch lightning model from the checkpoints
            start_birnn_model = lit.load_from_checkpoint(start_weights)
            #model = start_birnn_model.classifier
            #model.eval()

            start_raw_res = rnn_predict_raw( start_birnn_model  , bin, 1, ends=False)
            stacked_start_res = np.hstack([arr.numpy().reshape(1, -1) for arr in start_raw_res])

            print("start res shape  ", stacked_start_res.shape)
            save_raw_prediction(stacked_start_res, out_file)
            del stacked_start_res
            del start_raw_res
            torch.cuda.empty_cache()

            print(f"Done {bin}")

    count = 0
    if not only_starts:
        for bin in files:
            out_file = out_path.joinpath(bin.name + "_ends_result.npz")
            if out_file.exists():
                continue 

            # Load the model and predict
            end_birnn_model = lit.load_from_checkpoint(end_weights)
            #model = end_birnn_model.classifier
            #model.eval()

            end_raw_res = rnn_predict_raw(end_birnn_model, bin, 1, ends=True)


            # Stack the results together
            stacked_end_res = np.hstack([arr.numpy().reshape(1, -1) 
                                    for arr in end_raw_res])
            print("End res shape  ", stacked_end_res.shape)
            save_raw_prediction(stacked_end_res, out_file)
            del stacked_end_res
            del end_raw_res
            torch.cuda.empty_cache()
    return





#TODO: mark
@app.command()
def raw_test(
        testset_dir: Annotated[str, typer.Argument(
                        help='Directory of bins to test on')],
        weights: Annotated[str, typer.Argument(
                            help='File of bins')],
        out_dir: Annotated[str, typer.Argument(
                        help='Directory of bins to test on')],
        min_func_len: Annotated[int, typer.Option(
                        help='Directory of bins to test on')]=1,
    ):


    out_path = Path(out_dir)

    if not out_path.exists():
        out_path.mkdir()
    elif out_path.is_file():
        print("Out path is a file")
        return

    test_path = Path(testset_dir)
    if test_path.is_file():
        files = [test_path]
    else:
        files = list(Path(testset_dir).glob('*'))

    # Load the pytorch lightning model from the checkpoints
    lit_pylit = lit.load_from_checkpoint(weights)#,loss_func=loss_func,
    #                                 learning_rate=learning_rate,
    #                                 classifier=model)
    #model = lit_pylit.classifier
    model = lit_pylit

    for bin in alive_it(files):
        raw_res = rnn_predict_raw(model, bin, 1)
        #print(len(raw_res))
        #for i, item in enumerate(raw_res):
        #    print(f"{i}: {type(item)}")
        #    print(f"{item.shape}")
        #cur_res = raw_res[0].detach().numpy()
        #print(cur_res.shape)

        stacked_res = np.hstack([arr.numpy().reshape(1, -1) for arr in raw_res])
        #print(stacked_res.shape)

        save_raw_prediction(stacked_res, out_path.joinpath(bin.name + "_result.npz"))
    return


def convert_npz_to_func_list(bin_path:Path, npz:np.ndarray)->np.ndarray:
    '''
    '''

    bin = lief.parse(str(bin_path.resolve()))

    text_section = bin.get_section(".text")

    # Get the base address of the loaded binary
    base_address = bin.imagebase

    start_addrs = []

    # This enumerate the .text byte and sees which ones are functions
    for i, val in enumerate(npz.flatten()):
        if val == 0:
            continue
        address = base_address + text_section.virtual_address + i
        start_addrs.append(address)

    return np.array(start_addrs)


def all_lief_gnd_truth(bin_path: Path):
    '''
    Retrun labels all the functions in the .text section of the 
    binary
    '''
    bin = lief.parse(str(bin_path.resolve()))

    text_section = bin.get_section(".text")
    text_bytes = text_section.content

    # Get the bytes in the .text section
    text_bytes = text_section.content

    # Get the base address of the loaded binary
    base_address = bin.imagebase

    functions = get_functions(bin_path)

    func_start_addrs = {x.addr : (x.name, x.size) for x in functions}

    # This enumerate the .text byte and sees which ones are functions
    for i, _ in enumerate(text_bytes):
        address = base_address + text_section.virtual_address + i

        if address in func_start_addrs.keys():
            yield 1
        else:
            yield 0 



@app.command()
def read_bounds_raw(
        binaries: Annotated[str, typer.Argument(
                                    help='bins',
                                    callback=iterable_path_shallow_callback
                            )],
        results: Annotated[str, typer.Argument(
                        help='bin results',
                        callback=iterable_path_deep_callback)],
        threshold: Annotated[float, typer.Argument(
                        help='Threshold for predictions')],
        verbose: Annotated[bool, typer.Option(
                        help='bin results')]=False,
        starts: Annotated[bool, typer.Option(
                        help='only do starts')]=False,
        ignore_missing: Annotated[bool, typer.Option(
                        help='Ignore bins that dont have a result')]=False,
        )->None:
    '''
    Read the raw results and calculate the confusion matrix for the BiRNN 

    This file will be a matrix of 1xnum_byte

    The first byte will be the first byte in the .text. However the whole 
    matrix may not be able to cover all the .text bytes. 
    '''

    matching_bins_ends = {}
    matching_bins_starts = {}

    for bin in binaries:
        found_start = False
        found_end = False
        for res in [x for x in results if x.is_file() and "result" in x.name]:
            if not starts:
                if bin.name in res.name and "end" in res.name:
                    matching_bins_ends[bin] = res
                    found_end = True
            if bin.name in res.name and "start" in res.name:
                matching_bins_starts[bin] = res
                found_start= True

        if ignore_missing and (not found_start or not found_end):
            # Make sure if we are reading bounds, the bin is not in either dict
            if bin in matching_bins_ends:
                del matching_bins_ends[bin]

            # IF we are only doing starts, then the bin can stay in starts
            if not starts:
                if bin in matching_bins_starts:
                    del matching_bins_starts[bin]
        else:
            if starts:
                if not found_start:
                    print(f"Could not find a matching file for {bin}")
                    raise typer.Abort()
            else:
                if not found_start or not found_end:
                    print(f"Could not find a matching file for {bin}")
                    raise typer.Abort()


    total_start_conf = ConfusionMatrix(0,0,0,0)
    total_end_conf = ConfusionMatrix(0,0,0,0)

    print(len(matching_bins_starts.keys()))

    # Read the result for the bins
    for bin_path in alive_it(matching_bins_starts.keys()):

        # Init the confusion matrix for this bin
        start_conf = ConfusionMatrix(0,0,0,0)
        end_conf = ConfusionMatrix(0,0,0,0)

        # 1  - Ground truth for bin file, this changes depending on 
        #       if we are reading the result of an end experiment or 
        #       not.
        gnd_truth = lief_gnd_truth(bin_path.resolve())
        #gnd_matrix_starts = gnd_truth.func_addrs.flatten()
        gnd_matrix_starts = gnd_truth.func_addrs
        lengths_adjusted = gnd_truth.func_lens
        gnd_matrix_ends = gnd_truth.func_addrs + lengths_adjusted
        gnd_matrix = np.concatenate((gnd_truth.func_addrs.T.reshape(-1,1), 
                                    gnd_matrix_ends.T.reshape(-1,1)), axis=1)
        #gnd_matrix_ends = gnd_truth.func_addrs.flatten() + gnd_truth.func_lens.flatten()


        # 2 - Read the npz data 
        birnn_starts = read_birnn_npz(matching_bins_starts[bin_path])

        if not starts:
            birnn_ends = read_birnn_npz(matching_bins_ends[bin_path])

        print(f"HEREEEEEEEEEEEEEEEEEEEE")
        print(birnn_starts.shape)
        print(birnn_starts.flatten().shape)
        print(f"HEREEEEEEEEEEEEEEEEEEEE")
        with open("BIRNN_RAW", 'w') as f:
            for i, val in enumerate(birnn_starts.flatten()):
                f.write(f"{val}\n")
        with open("LIEF_RAW", 'w') as f:
            for i, val in enumerate(gnd_matrix_starts):
                f.write(f"{val}\n")


        # 2.1 - Use the threshold to identiry positives and negatives
        birnn_starts[birnn_starts >= threshold] = 1
        birnn_starts[birnn_starts < threshold] = 0
        birnn_starts = birnn_starts.astype(int)
        print(len(birnn_starts[birnn_starts==1]))
        birnn_ends[birnn_ends >= threshold] = 1
        birnn_ends[birnn_ends < threshold] = 0
        birnn_ends = birnn_starts.astype(int)

        start_indices = np.where(birnn_starts == 1)[1]
        if not starts:
            ends_indices = np.where(birnn_ends == 1)[1]

        # 3 - Get the address of the first byte in the .text section and add this
        #     to all the function bound indices
        #parsed_bin = lief.parse(str(bin_path.resolve()))
        #text_section_virt_addr = parsed_bin.get_section(".text").virtual_address
        #text_section_start = parsed_bin.imagebase + text_section_virt_addr
        #birnn_starts = start_indices + text_section_start
        #birnn_ends = ends_indices + text_section_start

        birnn_starts = convert_npz_to_func_list(bin_path, birnn_starts)
        birnn_ends = convert_npz_to_func_list(bin_path, birnn_ends)

        #birnn_starts[birnn_starts >= threshold] = 1
        #birnn_starts[birnn_starts < threshold] = 0
        #birnn_starts = birnn_starts.astype(int)
        #print(len(birnn_starts[birnn_starts==1]))

        #if not starts:
        #    birnn_ends[birnn_ends >= threshold] = 1
        #    birnn_ends[birnn_ends < threshold] = 0
        #    birnn_ends = birnn_starts.astype(int)


        #gnd_matrix_starts = np.array(list(all_lief_gnd_truth(bin_path.resolve()))).flatten()
        #birnn_starts = birnn_starts.flatten()

        # 2.2 - Start prediction current is list of all byte labels, convert to a list of addrs
        #     to all the function bound indices

        print(gnd_matrix_starts.shape)
        print(birnn_starts.shape)

        #with open("LIEF", 'w') as f:
        #    for val in gnd_matrix_starts:
        #        f.write(f"{val}\n")
        #with open("BIRNN", 'w') as f:
        #    for val in birnn_starts:
        #        f.write(f"{val}\n")


        # 2.2 Currently, the starts and ends have all the bytes in the .text section
        #       1. start at text section base address 
        #       2. starts = [index_of_x(x)+base_addr for x in matrix if x==1]
        #birnn_starts = convert_npz_to_func_list(bin_path,birnn_starts.flatten())

        #if not starts:
        #    birnn_ends = convert_npz_to_func_list(bin_path,birnn_ends.flatten())

        with open("LIEF", 'w') as f:
            for i, val in enumerate(gnd_matrix_starts):
                if i < len(gnd_matrix_starts)-1:
                    f.write(f"{val}: {gnd_matrix_starts[i+1]-val}\n")
                else:
                    f.write(f"{val}\n")
        with open("BIRNN", 'w') as f:
            for i, val in enumerate(birnn_starts):
                if i < len(birnn_starts)-1:
                    f.write(f"{val}: {birnn_starts[i+1]-val}\n")
                else:
                    f.write(f"{val}\n")

        print("write to files")

        # 3 - Compare the two lists
        # Get all the start addrs that are in both, in ida only, in gnd_trush only
        #start_conf.tp=len(np.intersect1d(gnd_matrix_starts, birnn_starts))
        #start_conf.fp=len(np.setdiff1d( birnn_starts, gnd_matrix_starts ))
        #start_conf.fn=len(np.setdiff1d(gnd_matrix_starts, birnn_starts))

        start_conf.tp=len(np.intersect1d(gnd_matrix[:,0], birnn_starts))
        start_conf.fp=len(np.setdiff1d( birnn_starts, gnd_matrix[:,0] ))
        start_conf.fn=len(np.setdiff1d(gnd_matrix[:,0], birnn_starts))

        # 5 - Compare the ends
        end_conf.tp=len(np.intersect1d(gnd_matrix[:,1], birnn_ends))
        end_conf.fp=len(np.setdiff1d( birnn_ends, gnd_matrix[:,1] ))
        end_conf.fn=len(np.setdiff1d(gnd_matrix[:,1], birnn_ends))


        #print("Set diff")

        #if not starts:
        #    end_conf.tp=len(np.intersect1d(gnd_matrix_ends, birnn_ends))
        #    end_conf.fp=len(np.setdiff1d( birnn_ends, gnd_matrix_ends ))
        #    end_conf.fn=len(np.setdiff1d(gnd_matrix_ends, birnn_ends))


        # Save total results
        total_start_conf.tp += start_conf.tp
        total_start_conf.fp += start_conf.fp
        total_start_conf.fn += start_conf.fn

        # Save total results
        total_end_conf.tp += end_conf.tp
        total_end_conf.fp += end_conf.fp
        total_end_conf.fn += end_conf.fn

        if verbose:
            print(f"Starts: {bin_path}")
            print(f"Conf: { start_conf }")
            print(f"Metirx{ calc_metrics(start_conf)}")

            if not starts:
                print("Ends")
                print(f"Conf: { end_conf }")
                print(f"Metirx{ calc_metrics(end_conf)}")
     



    print(f"Starts")
    print(f"Conf Matrix: {total_start_conf}")     
    print(f"Metrics: {calc_metrics(total_start_conf)}")

    print(f"Ends")
    print(f"Conf Matrix: {total_end_conf}")     
    print(f"Metrics: {calc_metrics(total_end_conf)}")
    return


def read_birnn_npz(inp: Path)->np.ndarray:
    '''
    Read the ida npz
    '''
    npz_file = np.load(inp)
    return npz_file[list(npz_file.keys())[0]]


@app.command()
def test_on(
        testset_dir: Annotated[str, typer.Argument(
                        help='Directory of bins to test on')],
        start_weights: Annotated[str, typer.Argument(
                    help='File of bins')],
        end_weights: Annotated[str, typer.Argument(
                    help='File of bins')],
        threshold: Annotated[float, typer.Argument(
                    help='Threshold for model prediction')],
        verbose: Annotated[bool, typer.Option()]=False,
        logdir: Annotated[str, typer.Option()] = "",
        ):

    if logdir == "":
        logging = False
    else:
        logging = True
        log_out = Path(logdir)

    # make sure testdir exists 
    testset_path = Path(testset_dir)
    if not testset_path.exists():
        print(f"Testset {testset_path} doesn't exist")
        return

    if testset_path.is_file():
        testfiles = [testset_path]
    else:
        # Load the files from the test set 
        testfiles = list(testset_path.glob('*'))

    tot_start_conf = ConfusionMatrix(0,0,0,0)
    tot_end_conf = ConfusionMatrix(0,0,0,0)
    tot_bound_conf = ConfusionMatrix(0,0,0,0)

    for bin in alive_it(testfiles):

        gnd_truth = lief_gnd_truth(bin.resolve())
        # NOTICE: IMPORTANT... xda seems to the first byte outside of the function 
        #               as the end of the function 
        lengths_adjusted = gnd_truth.func_lens
        ends = gnd_truth.func_addrs + lengths_adjusted
        gnd_matrix = np.concatenate((gnd_truth.func_addrs.T.reshape(-1,1), 
                                    ends.T.reshape(-1,1)), axis=1)
 
        # Load the pytorch lightning model from the checkpoints
        lit_pylit = lit.load_from_checkpoint(start_weights)
        model_start = lit_pylit.classifier
        model_start.eval()
        start_conf, start_addrs= rnn_predict_bounds(model_start, bin, threshold, start=True)

        end_birnn_model = lit.load_from_checkpoint(end_weights)
        model_end = end_birnn_model.classifier
        model_end.eval()
        end_conf , end_addrs = rnn_predict_bounds(model_end, bin, threshold, start=False)
        #start_conf, end_conf, start_addrs, end_addrs = rnn_predict_bounds(model_start, model_end, bin, threshold)

        # 1. Add a column of all 1s for the start addrs, and a column of all 2s for the end addrs
        all_ones = np.ones((1,len(start_addrs)))
        #tmp_starts = np.vstack((xda_starts, np.ones((1,len(xda_starts))))).T
        tmp_starts = np.vstack((start_addrs, all_ones)).T

        all_twos = np.full((1,len(end_addrs)),2)
        #tmp_ends = np.vstack((xda_ends, np.full((1,len(xda_ends)),2))).T
        tmp_ends = np.vstack((end_addrs, all_twos)).T

        # 2. Vertically stack the start and end columns and sort by adddress
        comb =  np.vstack((tmp_starts, tmp_ends))
        sorted_indices = np.argsort(comb[:, 0])
        bounds = comb[sorted_indices]

        # 3. Filter any occurancce where theres more than 1 start, or end in a row
        # Specifically, the following line..
        #   a. bounds[1:,1] gets the second column excluding the first row
        #   b. bounds[:,1] gets the second column excluding the last row
        #   c. Compare these two 
        #    
        #  1                                     1
        #  2   ->   2  !=   1  ->  True    ->    2
        #  1        1       2      True          1
        #  1        1       1      False
        if bounds.shape[0] == 0:
            birnn_bounds = np.array([[]])
        else:
            indices_to_keep = np.append(True, bounds[1:, 1] != bounds[:-1, 1])
            filt_sorted_bounds = bounds[indices_to_keep]

            # If the first label is a function end, remove it,
            # If the last label is a function start, remove it 
            if filt_sorted_bounds[0,1] == 2:
                filt_sorted_bounds = filt_sorted_bounds[1:,:]
            if filt_sorted_bounds[-1,1] == 1:
                filt_sorted_bounds = filt_sorted_bounds[:-1,:]

            # Lastly, combine the start and ends array to make matrix:   | start | end |
            starts = filt_sorted_bounds[filt_sorted_bounds[:,1] == 1]
            ends = filt_sorted_bounds[filt_sorted_bounds[:,1] == 2]
            birnn_bounds = np.hstack(( starts[:,0].reshape(-1,1), ends[:,0].reshape(-1,1)))

        bound_tp = np.count_nonzero(np.all(np.isin(birnn_bounds, gnd_matrix),axis=1))
        bound_fp = birnn_bounds.shape[0] - bound_tp
        bound_fn = gnd_matrix.shape[0] - bound_tp

        tot_bound_conf.tp+=bound_tp
        tot_bound_conf.fp = birnn_bounds.shape[0] - bound_tp
        tot_bound_conf.fn = gnd_matrix.shape[0] - bound_tp

        tot_start_conf.tp += start_conf.tp
        tot_start_conf.tn += start_conf.tn
        tot_start_conf.fp += start_conf.fp
        tot_start_conf.fn += start_conf.fn

        tot_end_conf.tp += end_conf.tp
        tot_end_conf.tn += end_conf.tn
        tot_end_conf.fp += end_conf.fp
        tot_end_conf.fn += end_conf.fn

        if logging:
            tot_out = log_out / f"{bin.name}_res"
            with open(tot_out, 'w') as f:
                json.dump({'bin': bin.name, 
                            'start_tp' : start_conf.tp,
                            'start_fp' : start_conf.fp,
                            'start_fn' : start_conf.fn,
                            'end_tp' : end_conf.tp ,
                            'end_fp' : end_conf.fp ,
                            'end_fn' : end_conf.fn,
                            'bound_tp' : bound_tp ,
                            'bound_fp' : bound_fp ,
                            'bound_fn' : bound_fn,
                            }, f )

        if verbose:
            print(f"Start Conf: {start_conf}")
            print(f"Metrics: {calc_metrics(start_conf)}")
            print(f"End Conf: {end_conf}")
            print(f"Metrics: {calc_metrics(end_conf)}")

    print(f"RESULTS.....................")
    print(f"Total Start Conf: {tot_start_conf}")
    print(f"StartMetrics: {calc_metrics(tot_start_conf)}")
    print(f"Total End Conf: {tot_end_conf}")
    print(f"End Metrics: {calc_metrics(tot_end_conf)}")
    print(f"Total End Conf: {tot_bound_conf}")
    print(f"End Metrics: {calc_metrics(tot_bound_conf)}")

    return

@app.command()
def test_on_read(
        inp_dir: Annotated[str, typer.Argument()]
    ):

    tot_out = Path(inp_dir)
    if not tot_out.exists():
        return

    tot_start_conf = ConfusionMatrix(0,0,0,0)
    tot_end_conf = ConfusionMatrix(0,0,0,0)
    tot_bound_conf = ConfusionMatrix(0,0,0,0)

    for file in alive_it(list(tot_out.glob('*'))):
        try:
            with open(file, 'r') as f:
                data = json.load(f)
        except json.JSONDecodeError as e:
            print(f"Decode error on {file}")
            continue

        tot_start_conf.tp += data['start_tp']
        tot_start_conf.fp += data['start_fp']
        tot_start_conf.fn += data['start_fn']

        tot_end_conf.tp += data['end_tp']
        tot_end_conf.fp += data['end_fp']
        tot_end_conf.fn += data['end_fn']

        tot_bound_conf.tp += data['bound_tp']
        tot_bound_conf.fp += data['bound_fp']
        tot_bound_conf.fn += data['bound_fn']


    print(f"Total Start Conf: {tot_start_conf}")
    print(f"StartMetrics: {calc_metrics(tot_start_conf)}")

    print(f"Total End Conf: {tot_end_conf}")
    print(f"End Metrics: {calc_metrics(tot_end_conf)}")

    print(f"Total Bound Conf: {tot_bound_conf}")
    print(f"Bound Metrics: {calc_metrics(tot_bound_conf)}")


                   # {'bin': bin.name, 
                   #'start_tp' : start_conf.tp,
                   #'start_fp' : start_conf.fp,
                   #'start_fn' : start_conf.fn,
                   #'end_tp' : end_conf.tp ,
                   #'end_fp' : end_conf.fp ,
                   #'end_fn' : end_conf.fn,
                   #'bound_tp' : bound_tp ,
                   #'bound_fp' : bound_fp ,
                   #'bound_fn' : bound_fn,
                   #         }, f )



@app.command()
def model_summary():

    learning_rate =  0.0005
    input_size=255
    hidden_size=16
    layers=1

    model = recreatedModel(input_size, hidden_size, layers)

    # Binary cross entrophy loss
    loss_func = nn.BCELoss()

    classifier = lit(model,
                     loss_func=loss_func,
                     learning_rate=learning_rate,
                     input_size=input_size,
                     hidden_size=hidden_size,
                     num_layers=layers)

    summary(model, (32,1000,255))

    return


if __name__ == "__main__":
    app()

    exit(1)

    OPTIMIZATION = 'O1'

    #TODO: This is best used when I have large similar datasets for O0-Oz
    #       until I have all of those compiled I will manually split
    #with open("TEST_BIN_NAME_SET.json", 'r') as f:
    #    bin_names = json.load(f)['names']

    # 
    rust_train_files = []
    rust_test_files = []

    rust_o0_files = []

    xda_testset= []
    with open('OUT.json', 'r') as f:
        xda_testset= json.load(f)['names']

    for parent in Path("/home/ryan/.ripbin/ripped_bins/").iterdir():
        info_file = parent / 'info.json'
        info = {}
        try:
            with open(info_file, 'r') as f:
                info = json.load(f)
        except FileNotFoundError:
            print(f"File not found: {info_file}")
            continue
        except json.JSONDecodeError as e:
            print(f"JSON decoding error: {e}")
            continue
        except Exception as e:
            print(f"An error occurred: {e}")
            continue


        if info['optimization'].upper() in OPTIMIZATION:
            npz_file = parent / "onehot_plus_func_labels.npz"

            if info['binary_name'] in xda_testset:
                rust_test_files.append(npz_file)
            else:
                rust_train_files.append(npz_file)

            #rust_o0_files.append(npz_file)
            #if info['binary_name'] not in bin_names:
            #    rust_train_files.append(npz_file)
            #else:
            #    rust_test_files.append(npz_file)




    # TODO: TEMP: 
    #rust_train_files.extend(rust_test_files)
    #rust_test_files.extend(rust_o0_files[250:])
    #rust_train_files.extend(rust_o0_files[:250])



    metrics, classifier = lit_model_train(rust_train_files)
    print([x.compute() for x in metrics])


    # Create the dataloader for the test files now 
    test_data, test_lbl= gen_one_hot_dataset(rust_test_files ,
                                            num_chunks=1000)

    # TODO: Hardcoded a chache here 
    # BUG: This doesn't have to do with the target tensor error 
    #       beacuse it happens with and without it 
    #_, _, test_data = vsplit_dataset(test_data, (0,0,100)) 
    #_, _, test_lbl = vsplit_dataset(test_lbl,(0,0,100)) 

    #print(test_lbl)

    test_dataset = MyDataset(torch.Tensor(test_data).to(DEVICE), 
                     torch.Tensor(test_lbl).squeeze().to(DEVICE))

    # BUG: Num workers introduced more CUDA Initialization errors
    test_dataloader = DataLoader(test_dataset, batch_size=32, 
                                shuffle=False,
                                drop_last=True)#, num_workers=CPU_COUNT)

    trainer = pylight.Trainer(max_epochs=100)

    classifier.reset_metrics()

    # Get the run time of the module
    start = time.time()
    res = trainer.test(classifier,dataloaders=test_dataloader)
    runtime = time.time() - start

    print(f"Test on {len(rust_test_files)}")
    metrics = [x.compute() for x in classifier.metrics]
    print(metrics)
    print(f"Run time for 1000 chunks on optimization {OPTIMIZATION}: {runtime}")
    print(f"The len of train files was {len(rust_train_files)}")
    print(f"The len of test files was {len(rust_test_files)}")

    test_files = Path("RUST_TEST_FILES.txt")
    with open(test_files, 'w') as f:
        for file in rust_test_files:
            f.write(f"{file.parent}\n")

    #run_info = {
    #    'metrics' : metrics,
    #    'optimization' : OPTIMIZATION,
    #    'train_file_pool' : rust_train_files,
    #    'test_file_pool' : rust_test_files,
    #}

    ## Save the summary
    #with open(f"RNN_SUMMARY_{OPTIMIZATION}.json", 'w') as f:
    #    json.dump(run_info, f)

