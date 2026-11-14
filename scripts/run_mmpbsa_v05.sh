#!/usr/bin/env bash
# v0.5-04: 对 5 个 3ns MD 轨迹批量 MMPBSA(GB 全部 → PB 全部)
# 探索性工具输出;非结合/折叠自由能;跨体系差异不构成突变效应结论
set -u
cd ~/projects/rosetta1
PY=~/.conda/envs/rosetta-agent/bin/python
RUNS="20261113_230809_gmx_2hbb_f31a_3ns 20261113_231242_gmx_2hbb_k12m_3ns 20261113_231707_gmx_2hbb_i18v_3ns 20261113_232132_gmx_2hbb_wt_rep1_3ns 20261113_232604_gmx_2hbb_wt_rep2_3ns"
for R in $RUNS; do
  $PY -u -m workbench.mmpbsa --md-run runs/gromacs/$R --calc gb || echo "!! GB FAILED $R"
done
echo "=== BATCH_GB_DONE $(date +%H:%M:%S) ==="
for R in $RUNS; do
  $PY -u -m workbench.mmpbsa --md-run runs/gromacs/$R --calc pb || echo "!! PB FAILED $R"
done
echo "=== BATCH_PB_DONE $(date +%H:%M:%S) ==="
