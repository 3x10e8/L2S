import argparse
import torch
import torch.nn as nn
import torch.nn.functional as F
from hs_api.converter import CRI_Converter, Quantize_Network, BN_Folder
from spikingjelly.activation_based import neuron, surrogate, encoding, layer, functional
from spikingjelly.datasets.n_mnist import NMNIST
from torch.utils.data import DataLoader
from hs_api.api import CRI_network
import time
from torch.utils.tensorboard import SummaryWriter
from copy import deepcopy
from tqdm import tqdm
from spikingjelly import visualizing
from matplotlib import pyplot as plt
import matplotlib
import numpy as np
import hs_bridge
import sys
import subprocess
import time
import pickle
import random
from examples.synth_stress import synthnet

parser = argparse.ArgumentParser()
parser.add_argument('-s', default=1, type=int, help='stride size')
parser.add_argument('-k', default=3, type=int, help='kernel size')
parser.add_argument('-p', default=0, type=int, help='padding size')
parser.add_argument('-c', default=4, type=int, help='channel size')
parser.add_argument('-alpha',  default=4, type=int, help='Range of value for quantization')
parser.add_argument('-b', default=1, type=int, help='batch size')
parser.add_argument('-T', default=4, type=int)
parser.add_argument('-resume_path', default='/Volumes/export/isn/keli/code/HS/CRI_Mapping/output/nmnist/checkpoint_max_T_16_C_20_lr_0.001_opt_adam.pth', type=str, help='checkpoint file')
parser.add_argument('-data-dir', default='/Volumes/export/isn/keli/code/data/NMNIST', type=str, help='path to dataset')
parser.add_argument('-targets', default=10, type=int, help='Number of labels')
parser.add_argument('-figure-dir', default='/Users/keli/Code/CRI/hs_api/figure',type=str, help='path to output figure' )

def norm(x: torch.Tensor):
    s = x.shape
    x = x.flatten()
    std, mean = torch.std_mean(x)
    x -= mean
    x /= std
    return x.reshape(s)

def plot_2d_heatmap(array: np.ndarray, title: str, xlabel: str, ylabel: str, int_x_ticks=True, int_y_ticks=True,
                    plot_colorbar=True, colorbar_y_label='magnitude', x_max=None, figsize=(12, 8), dpi=200):
    if array.ndim != 2:
        raise ValueError(f"Expected 2D array, got {array.ndim}D array instead")

    fig, heatmap = plt.subplots(figsize=figsize, dpi=dpi)
    if x_max is not None:
        im = heatmap.imshow(array.T, aspect='auto', extent=[-0.5, x_max, array.shape[1] - 0.5, -0.5])
    else:
        im = heatmap.imshow(array.T, aspect='auto')

    heatmap.set_title(title)
    heatmap.set_xlabel(xlabel)
    heatmap.set_ylabel(ylabel)

    heatmap.xaxis.set_major_locator(matplotlib.ticker.MaxNLocator(integer=int_x_ticks))
    heatmap.yaxis.set_major_locator(matplotlib.ticker.MaxNLocator(integer=int_y_ticks))
    heatmap.xaxis.set_minor_locator(matplotlib.ticker.NullLocator())
    heatmap.yaxis.set_minor_locator(matplotlib.ticker.NullLocator())

    if plot_colorbar:
        cbar = heatmap.figure.colorbar(im)
        cbar.ax.set_ylabel(colorbar_y_label, rotation=90, va='top')
        cbar.ax.yaxis.set_minor_locator(matplotlib.ticker.NullLocator())
    return fig

    
def main():
    args = parser.parse_args()
    print(args)
    
    config = {}
    config['neuron_type'] = "I&F"
    config['global_neuron_params'] = {}
    config['global_neuron_params']['v_thr'] = 6
    
    # synth = synthnet(3,3,-2,6,2)
    axonsDict = {
        'a0': [('0',2),('1',1)],
        'a1': [('0',4),('1',3)]
    }
    
    neuronsDict = {
        '0': [(1,1)],
        '1': [(0,-1)]
    }
    
    hardwareNetwork = CRI_network(axons=axonsDict,
                                  connections=neuronsDict,
                                  config=config, 
                                  target='CRI', 
                                  outputs = neuronsDict.keys(),
                                  simDump=True,
                                  coreID=1,
                                  perturbMag=0,
                                  leak=2**6)
    softwareNetwork = CRI_network(axons=axonsDict,
                                  connections=neuronsDict,
                                  config=config, 
                                  outputs = neuronsDict.keys(), 
                                  target='simpleSim',
                                  perturbMag=0,
                                  leak=2**6)

    sw_s_list, hw_s_list = [], []
    sw_v_list, hw_v_list = [], []
    
    input_lists = ['a0','a1','a0','a1']
    
    for t in range(args.T):
        
        input = input_lists[t]
        
        # swOutput: [(key, potential) for all the neurons in softwareNetwork] 
        swOutput, swSpike  = softwareNetwork.step(input, membranePotential=True)
        swSpikeIdx = [int(spike) for spike in swSpike]
        sw_v_list.append(torch.tensor([v for k,v in swOutput]).unsqueeze(0))

        hwOutput, spikeResult  = hardwareNetwork.step(input, membranePotential=True)
        hwSpike, latency, hbmAcc = spikeResult
        hwSpikeIdx = [int(spike) for spike in hwSpike]   
        hw_v_list.append(torch.tensor([v for k,v in hwOutput]).unsqueeze(0)) 
        
        sw_spikes = torch.zeros(len(neuronsDict)).flatten()
        sw_spikes[swSpikeIdx] = 1
        sw_s_list.append(sw_spikes.unsqueeze(0))
        
        hw_spikes = torch.zeros(len(neuronsDict)).flatten()
        hw_spikes[hwSpikeIdx] = 1
        hw_s_list.append(hw_spikes.unsqueeze(0))

    
    # plot the spikes
    sw_s_list = torch.cat(sw_s_list)
    hw_s_list = torch.cat(hw_s_list)
    
    figsize = (12, 8)
    dpi = 100

    #compare the software and hardware spike output
    num_matches = (sw_s_list==hw_s_list).sum()
    total = sw_s_list.numel()
    accuracy = num_matches/total * 100 if num_matches != 0 else 0
    print(f"Spikes {accuracy}% matches")

    #compare the pytorch and software firing rate
    sw_r_list = torch.mean(sw_s_list.T, axis=1, keepdims=True)
    hw_r_list = torch.mean(hw_s_list.T, axis=1, keepdims=True)
    num_matches = (sw_r_list==hw_r_list).sum()
    total = sw_r_list.numel()
    accuracy = num_matches/total * 100 if num_matches != 0 else 0
    print(f"Firing rate {accuracy}% matches")
    
    visualizing.plot_1d_spikes(spikes=sw_s_list.numpy(), title='Software Spikes', xlabel='simulating step',
                ylabel='neuron index', figsize=figsize, dpi=dpi)
    plt.savefig(f"figure/SW_S.png")
    visualizing.plot_1d_spikes(spikes=hw_s_list.numpy(), title='Hardware Spikes', xlabel='simulating step',
                ylabel='neuron index', figsize=figsize, dpi=dpi)
    plt.savefig(f"figure/HW_S.png")
    
    # plot the membrane potential 
    sw_v_list = torch.cat(sw_v_list)
    hw_v_list = torch.cat(hw_v_list)
    
    plot_2d_heatmap(array=sw_v_list.numpy(), title='Software membrane potentials', xlabel='simulating step',
                                ylabel='neuron index', int_x_ticks=True, x_max=args.T, figsize=figsize, dpi=dpi)
    plt.savefig(f"figure/SW_V.png")
    
    plot_2d_heatmap(array=hw_v_list.numpy(), title='Hardware membrane potentials', xlabel='simulating step',
                                ylabel='neuron index', int_x_ticks=True, x_max=args.T, figsize=figsize, dpi=dpi)
    plt.savefig(f"figure/HW_V.png")
    
    # reset the membrane potential to zero
    softwareNetwork.simpleSim.initialize_sim_vars(len(neuronsDict))
    hs_bridge.FPGA_Execution.fpga_controller.clear(
                len(neuronsDict), False, 0
            )
    breakpoint()
    hardwareNetwork.sim_flush("test_syn_sim.txt")

if __name__ == '__main__':
    main()