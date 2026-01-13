#!/usr/bin/env bash

# TODO: module purge and load
# TODO: set env vars? Maybe move to calkit.yaml?

calkit xenv -n main -- python scripts/run.py --date "$1"
