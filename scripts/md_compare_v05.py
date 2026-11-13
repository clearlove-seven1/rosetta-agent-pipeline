#!/usr/bin/env python
"""v0.5-03 跨体系观察对比:读各 MD run 的 manifest/analysis,输出对比表 + RMSF 叠加图。

定位:观察性统计汇总。RMSD 平台 ≠ 收敛;跨体系差异 ≠ 稳定性结论;
本脚本产物不得用于筛选/评分/亲和力判断(PROTEIN_WORKBENCH_PLAN §6 v0.5)。

用法: python scripts/md_compare_v05.py
输出: docs/md_2hbb_mutants_v05_table.md + docs/md_2hbb_mutants_rmsf.png
"""

import glob
import json
import os
import sys

import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SYSTEMS = [
    ("WT (rep1, seed 20261113)", "2hbb_wt_rep1_3ns"),
    ("WT (rep2, seed 20261114)", "2hbb_wt_rep2_3ns"),
    ("F31A  exp +2.84 / REU +5.87", "2hbb_f31a_3ns"),
    ("K12M  exp -1.80 / REU +1.88", "2hbb_k12m_3ns"),
    ("I18V  exp +0.07 / REU +0.17", "2hbb_i18v_3ns"),
]


def read_xvg(path):
    rows = []
    if not os.path.isfile(path):
        return None
    for line in open(path, errors="replace"):
        line = line.strip()
        if not line or line[0] in "#@":
            continue
        rows.append([float(x) for x in line.split()])
    return np.array(rows) if rows else None


def main() -> int:
    rows = [
        "| 体系 | 状态 | ns/day | RMSD 均值 (nm) | RMSD 后半程 (nm) | T 均值 (K) | RMSF 峰值 (nm) |",
        "|---|---|---|---|---|---|---|",
    ]
    fig, ax = plt.subplots(figsize=(7.2, 4.4))
    for name, pat in SYSTEMS:
        dirs = sorted(glob.glob(os.path.join(REPO, "runs/gromacs", f"*_{pat}")))
        if not dirs:
            rows.append(f"| {name} | 缺失 | — | — | — | — | — |")
            continue
        rd = dirs[-1]
        m = json.load(open(os.path.join(rd, "manifest.json"), encoding="utf-8"))
        a = m.get("analysis_summary") or {}
        arr = read_xvg(os.path.join(rd, "analysis", "rmsf_atom.xvg"))
        if arr is not None and len(arr):
            ax.plot(np.arange(1, len(arr) + 1), arr[:, 1], lw=1.0, label=name)
            peak = f"{arr[:, 1].max():.3f}"
        else:
            peak = "—"
        rmsd = a.get("rmsd_backbone_nm") or {}
        perf = (m.get("gpu_evidence_mdrun_log") or {}).get("performance_ns_day")
        rows.append(
            f"| {name} | {m['status']} | {perf} | "
            f"{rmsd.get('mean', float('nan')):.3f} | {rmsd.get('mean_last_half', float('nan')):.3f} | "
            f"{(a.get('temperature_K') or {}).get('mean', float('nan')):.2f} | {peak} |"
        )
    ax.set_xlabel("residue number (CA order)")
    ax.set_ylabel("C-alpha RMSF (nm)")
    ax.set_title("2HBB WT/mutants — 3 ns each — RMSF (observational only)")
    ax.legend(fontsize=7.5)
    fig.text(0.5, 0.01, "Observational only: NOT convergence, NOT a scientific conclusion.",
             ha="center", fontsize=8, color="0.35")
    fig.tight_layout(rect=(0, 0.03, 1, 1))
    fig_path = os.path.join(REPO, "docs", "md_2hbb_mutants_rmsf.png")
    fig.savefig(fig_path, dpi=130)

    table_path = os.path.join(REPO, "docs", "md_2hbb_mutants_v05_table.md")
    with open(table_path, "w", encoding="utf-8") as f:
        f.write(
            "# v0.5-03 跨体系观察对比表（自动生成，勿手改）\n\n"
            "> 观察性统计：RMSD 平台 ≠ 收敛；跨体系 RMSD/RMSF 差异 ≠ 稳定性结论；\n"
            "> 不得用于筛选/评分/亲和力判断。实验 ΔΔG(kcal/mol) 与 Rosetta ΔΔG(REU) 仅为选点背景，两列口径不同不可互比。\n\n"
            + "\n".join(rows) + "\n"
        )
    print("\n".join(rows))
    print(f"\n[compare] 表: {table_path}\n[compare] 图: {fig_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
