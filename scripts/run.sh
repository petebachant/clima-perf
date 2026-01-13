#!/bin/bash

# Load modules
module purge
module load climacommon/2025_05_15

# Set environment variable for GPU usage
export CLIMACOMMS_DEVICE=CUDA

# Set environmental variable for julia to not use global packages for
# reproducibility
export JULIA_LOAD_PATH=@:@stdlib

calkit xenv -n main -- python scripts/run.py --date "$1"
