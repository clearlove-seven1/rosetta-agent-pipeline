#!/usr/bin/env bash
set -e
cd ~/projects/rosetta1
PY=~/.conda/envs/rosetta-agent/bin/python
$PY -u scripts/build_mutant_pdbs.py --run-dir runs/20261113_114519_2hbb_A31A --label f31a
$PY -u scripts/build_mutant_pdbs.py --run-dir runs/20261113_113844_2hbb_A12M --label k12m
$PY -u scripts/build_mutant_pdbs.py --run-dir runs/20261113_114223_2hbb_A18V --label i18v
echo BUILD_ALL_DONE
