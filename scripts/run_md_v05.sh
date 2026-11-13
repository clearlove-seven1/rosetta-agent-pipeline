#!/usr/bin/env bash
# v0.5-03: 2HBB WT×2副本 + F31A/K12M/I18V ×3ns 串行 MD 对照
# 观察性对照;不代表收敛;不构成科学结论(PROTEIN_WORKBENCH_PLAN §6 v0.5)
set -u
cd ~/projects/rosetta1
PY=~/.conda/envs/rosetta-agent/bin/python
run() {
  echo "=== MD: $2 (seed=$3) $(date +%H:%M:%S) ==="
  $PY -u -m workbench.gromacs --protein "$1" --chain A --label "$2" --ns 3.0 --seed "$3" \
      --note "v0.5-03 observational comparison; no scientific conclusion" \
      || echo "!! FAILED: $2 rc=$?"
}
S=runs/md_structures
run $S/f31a/mut_relaxed.pdb 2hbb_f31a_3ns 20261113
run $S/k12m/mut_relaxed.pdb 2hbb_k12m_3ns 20261113
run $S/i18v/mut_relaxed.pdb 2hbb_i18v_3ns 20261113
run $S/f31a/wt_relaxed.pdb  2hbb_wt_rep1_3ns 20261113
run $S/f31a/wt_relaxed.pdb  2hbb_wt_rep2_3ns 20261114
echo "=== ALL MD DONE $(date +%H:%M:%S) ==="
