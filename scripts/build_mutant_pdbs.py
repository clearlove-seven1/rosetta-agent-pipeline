#!/usr/bin/env python
"""为 v0.5-03 MD 对照生成 Rosetta 松弛突变/野生结构（cartesian_ddg dump_pdbs=true）。

用法（仓库根目录、rosetta-agent env）：
  python scripts/build_mutant_pdbs.py --run-dir runs/20261113_114519_2hbb_A31A --label f31a

做什么：
  1. 复用既有 v0.4 run 目录的 relax 产物与 mutfile（不重新 relax）；
  2. 以相同 seed/iterations 重跑 cartesian_ddg，但 dump_pdbs=true 导出 WT/MUT 松弛结构；
  3. 解析新 .ddg 并与原 run 的 manifest ddg_reu 交叉核对（同种子应完全一致）；
  4. 产物落 runs/md_structures/<label>/：mut_relaxed.pdb、wt_relaxed.pdb、
     ddg_crosscheck.json（含核对结果与文件哈希）。

注意：本脚本只生成 MD 输入结构与一致性核对记录，不做任何科学解读。
"""

import argparse
import glob
import hashlib
import json
import os
import shutil
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from workbench import executors
from workbench.ddgparse import parse_ddg
from workbench import manifest as mf


def sha(path: str) -> str:
    return mf.sha256(path)


def main() -> int:
    ap = argparse.ArgumentParser(description="生成 MD 用松弛突变结构（cartesian_ddg dump_pdbs）")
    ap.add_argument("--run-dir", required=True, help="既有 v0.4 run 目录（须已 succeeded）")
    ap.add_argument("--label", required=True, help="输出标签，如 f31a")
    ap.add_argument("--seed", type=int, default=1111111, help="与原 run 一致的种子")
    ap.add_argument("--iterations", type=int, default=3)
    ap.add_argument("--out-root", default="runs/md_structures")
    ap.add_argument("--timeout", type=int, default=3600)
    args = ap.parse_args()

    run_dir = os.path.abspath(args.run_dir)
    m = json.load(open(os.path.join(run_dir, "manifest.json"), encoding="utf-8"))
    if m.get("status") != "succeeded":
        print(f"[mutpdb] 拒绝：run 状态为 {m.get('status')}，仅 succeeded 可复用。", file=sys.stderr)
        return 2
    relaxed = os.path.join(run_dir, "relax", "relaxed_clean_protein_0001.pdb")
    mutfile = os.path.join(run_dir, "ddg", "mutfile.txt")
    for p in (relaxed, mutfile):
        if not os.path.isfile(p):
            print(f"[mutpdb] 拒绝：缺少 {p}", file=sys.stderr)
            return 2
    orig_ddg = (m.get("result") or {}).get("ddg_reu")

    out_dir = os.path.abspath(os.path.join(args.out_root, args.label))
    if os.path.exists(out_dir):
        print(f"[mutpdb] 拒绝：输出目录已存在 {out_dir}", file=sys.stderr)
        return 2
    os.makedirs(out_dir)

    print(f"[mutpdb] {args.label}: 重跑 cartesian_ddg（dump_pdbs=true，seed={args.seed}）…")
    d = executors.run_cartesian_ddg(relaxed, mutfile, out_dir, args.seed,
                                    iterations=args.iterations, timeout=args.timeout,
                                    dump_pdbs=True)

    # 交叉核对：同 seed/输入 → ΔΔG 应与原 run 完全一致
    fresh = parse_ddg(d["ddg_file"])
    fresh_reu = round(fresh["ddg"], 3)
    match = (orig_ddg is not None and fresh_reu == orig_ddg)

    mut_pdbs = sorted(glob.glob(os.path.join(out_dir, "*MUT*.pdb")))
    wt_pdbs = sorted(glob.glob(os.path.join(out_dir, "*WT*.pdb")))
    if not mut_pdbs or not wt_pdbs:
        print(f"[mutpdb] 失败：dump 结构缺失 MUT={len(mut_pdbs)} WT={len(wt_pdbs)}", file=sys.stderr)
        return 4
    shutil.copy2(mut_pdbs[0], os.path.join(out_dir, "mut_relaxed.pdb"))
    shutil.copy2(wt_pdbs[0], os.path.join(out_dir, "wt_relaxed.pdb"))

    record = {
        "source_run": m["run_id"],
        "source_manifest_ddg_reu": orig_ddg,
        "fresh_ddg_reu": fresh_reu,
        "crosscheck_match": bool(match),
        "label": args.label,
        "mut_relaxed": "mut_relaxed.pdb",
        "mut_relaxed_sha256": sha(os.path.join(out_dir, "mut_relaxed.pdb")),
        "wt_relaxed": "wt_relaxed.pdb",
        "wt_relaxed_sha256": sha(os.path.join(out_dir, "wt_relaxed.pdb")),
        "ddg_file": os.path.relpath(d["ddg_file"], out_dir),
        "note": "MD 输入结构生成记录；结构为计算模型，不代表任何实验构象",
    }
    with open(os.path.join(out_dir, "ddg_crosscheck.json"), "w", encoding="utf-8") as f:
        json.dump(record, f, ensure_ascii=False, indent=2)
    print(f"[mutpdb] {args.label}: 完成。ΔΔG 交叉核对 {'一致' if match else '不一致!'}"
          f"（原 {orig_ddg} vs 新 {fresh_reu}）")
    return 0 if match else 4


if __name__ == "__main__":
    sys.exit(main())
