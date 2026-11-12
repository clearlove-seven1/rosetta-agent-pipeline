"""Workbench CLI：无 LLM 的确定性单点突变稳定性计算入口（v0.2）

用法示例（在仓库根目录、rosetta-agent env 下）：
  python -m workbench.cli \
    --protein examples/1ubq/1ubq.pdb --chain A --resnum 76 --to E \
    --name ubq_smoke --note "首次冒烟测试"

流程：输入校验 → cartesian relax → 编号一致性校验 → cartesian_ddg → 解析 → 报告。
每次运行独立目录 runs/<run_id>/，含 manifest.json、日志、映射表与 report.txt。
退出码: 0 成功 | 2 输入校验拒绝 | 3 依赖缺失 | 4 执行失败
"""

import argparse
import os
import sys
import time

from . import SCOPE
from . import executors, manifest as mf
from .ddgparse import label_matches, parse_ddg
from .numbering import build_numbering, mutfile_text, save_mapping_csv
from .validation import ONE_TO_THREE, parse_pdb, validate_target


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="workbench",
        description="单体蛋白单点突变 Rosetta 稳定性计算（确定性，无 LLM）",
    )
    p.add_argument("--protein", required=True, help="输入 PDB 路径（单体）")
    p.add_argument("--chain", required=True, help="目标链 ID（如 A）")
    p.add_argument("--resnum", required=True, type=int, help="PDB 晶体学残基编号")
    p.add_argument("--to", required=True, help="突变后氨基酸单字母（ACDEFGHIKLMNPQRSTVWY）")
    p.add_argument("--wt", default="", help="野生型单字母（留空则从文件读取并回显核对）")
    p.add_argument("--name", default="", help="任务名称（可选）")
    p.add_argument("--note", default="", help="用户备注（可选）")
    p.add_argument("--seed", type=int, default=1111111, help="随机种子（relax/ddg 共用）")
    p.add_argument("--nstruct", type=int, default=1, help="relax 输出构象数")
    p.add_argument("--iterations", type=int, default=3, help="ddg 迭代轮数")
    p.add_argument("--workdir", default="runs", help="运行产物根目录")
    p.add_argument("--timeout-relax", type=int, default=1800)
    p.add_argument("--timeout-ddg", type=int, default=3600)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)

    mut = args.to.strip().upper()
    if len(mut) != 1 or mut not in ONE_TO_THREE:
        print(f"[workbench] 拒绝执行：--to '{args.to}' 不是合法的单字母氨基酸。", file=sys.stderr)
        return 2
    chain = args.chain.strip().upper()

    # ── 0. 依赖前置检查（缺 Rosetta 直接失败，不输出任何"成功"假象）──
    missing = executors.check_dependencies()
    if missing:
        print(f"[workbench] 依赖缺失，无法执行：{missing}", file=sys.stderr)
        return 3

    # ── 1. 输入校验 ──
    try:
        summary = parse_pdb(args.protein)
    except FileNotFoundError as e:
        print(f"[workbench] {e}", file=sys.stderr)
        return 2
    ok, msg, residue = validate_target(summary, chain, args.resnum, args.wt)
    print(f"[workbench] 校验: {msg}")
    if not ok:
        return 2

    # ── 2. 建任务目录与 manifest ──
    pdb_stem = os.path.splitext(os.path.basename(args.protein))[0]
    run_id = mf.new_run_id(pdb_stem, chain, args.resnum, mut)
    run_dir = os.path.join(args.workdir, run_id)
    os.makedirs(run_dir, exist_ok=True)
    task = {
        "run_id": run_id, "name": args.name, "note": args.note,
        "protein_path": summary.path, "chain": chain, "resnum": args.resnum,
        "icode": residue.icode, "wt_aa": residue.aa1, "mut_aa": mut,
        "seq_index": residue.seq_index, "seed": args.seed,
        "nstruct": args.nstruct, "iterations": args.iterations,
        "timeout_relax": args.timeout_relax, "timeout_ddg": args.timeout_ddg,
        "warnings": summary.warnings,
    }
    m = mf.create(run_dir, task)
    print(f"[workbench] 任务目录: {run_dir}")

    # ── 3. 编号映射（输入结构上计算并落盘审计）──
    residues, by_key, by_seq = build_numbering(summary.path)
    map_csv = save_mapping_csv(residues, os.path.join(run_dir, "numbering_map.csv"))
    target = by_seq[residue.seq_index]
    numbering_note = (
        f"PDB {chain}:{args.resnum} → 文件内顺序编号 #{target.seq_index}"
        f"（cartesian_ddg mutfile 使用该编号；映射表见 {os.path.basename(map_csv)}）"
    )
    m = mf.set_status(run_dir, m, "validated")

    # ── 4. relax ──
    relax_dir = os.path.join(run_dir, "relax")
    print(f"[workbench] relax 中… (nstruct={args.nstruct}, seed={args.seed})")
    m = mf.set_status(run_dir, m, "running")
    m = mf.record_step(run_dir, m, "relax", {"start_time": mf.now_iso()})
    try:
        r = executors.run_relax(summary.path, relax_dir, args.seed,
                                nstruct=args.nstruct, timeout=args.timeout_relax)
    except executors.ExecutorError as e:
        mf.record_step(run_dir, m, "relax", {"status": "failed", "error": str(e)[:2000]})
        mf.set_status(run_dir, m, "failed")
        print(f"[workbench] ❌ relax 失败:\n{e}", file=sys.stderr)
        return 4
    m = mf.record_step(run_dir, m, "relax", {
        "status": "succeeded", "duration_s": r["duration_s"],
        "returncode": r["returncode"], "command": r["command"],
        "relaxed_pdb": r["relaxed_pdb"], "log": r["log"],
    })
    m["software"]["rosetta"] = r["version"]
    mf.save(run_dir, m)
    print(f"[workbench] relax 完成 ({r['duration_s']}s): {os.path.basename(r['relaxed_pdb'])}")

    # ── 5. 弛豫前后顺序编号一致性校验（防 mutfile 错位）──
    same, msg = executors.check_seq_numbering_unchanged(summary.path, r["relaxed_pdb"])
    if not same:
        mf.record_step(run_dir, m, "numbering_check", {"status": "failed", "detail": msg})
        mf.set_status(run_dir, m, "failed")
        print(f"[workbench] ❌ {msg}", file=sys.stderr)
        return 4
    m = mf.record_step(run_dir, m, "numbering_check", {"status": "succeeded", "detail": msg})

    # 顺序编号在弛豫输出上必须保持不变，否则 mutfile 需基于新结构重算
    _, _, by_seq_relaxed = build_numbering(r["relaxed_pdb"])
    if residue.seq_index not in by_seq_relaxed or by_seq_relaxed[residue.seq_index].aa1 != residue.aa1:
        mf.record_step(run_dir, m, "numbering_check", {"status": "failed", "detail": "目标位点在弛豫产物中不一致"})
        mf.set_status(run_dir, m, "failed")
        print("[workbench] ❌ 目标位点在弛豫产物中不一致，拒绝继续", file=sys.stderr)
        return 4

    # ── 6. cartesian_ddg ──
    ddg_dir = os.path.join(run_dir, "ddg")
    mut_path = os.path.join(ddg_dir, "mutfile.txt")
    os.makedirs(ddg_dir, exist_ok=True)
    with open(mut_path, "w", encoding="utf-8") as f:
        f.write(mutfile_text(residue.seq_index, residue.aa1, mut))
    print(f"[workbench] cartesian_ddg 中… (iterations={args.iterations})")
    m = mf.record_step(run_dir, m, "cartesian_ddg", {"start_time": mf.now_iso()})
    try:
        d = executors.run_cartesian_ddg(r["relaxed_pdb"], mut_path, ddg_dir,
                                        args.seed, iterations=args.iterations,
                                        timeout=args.timeout_ddg)
    except executors.ExecutorError as e:
        mf.record_step(run_dir, m, "cartesian_ddg", {"status": "failed", "error": str(e)[:2000]})
        mf.set_status(run_dir, m, "failed")
        print(f"[workbench] ❌ cartesian_ddg 失败:\n{e}", file=sys.stderr)
        return 4
    m = mf.record_step(run_dir, m, "cartesian_ddg", {
        "status": "succeeded", "duration_s": d["duration_s"],
        "returncode": d["returncode"], "command": d["command"],
        "ddg_file": d["ddg_file"], "log": d["log"],
    })

    # ── 7. 解析 + 标签核对 ──
    try:
        ddg_info = parse_ddg(d["ddg_file"])
    except ValueError as e:
        mf.set_status(run_dir, m, "failed")
        print(f"[workbench] ❌ .ddg 解析失败: {e}", file=sys.stderr)
        return 4
    lcheck = label_matches(ddg_info, residue.seq_index, mut)
    if not lcheck[0]:
        m["result"] = {"ddg_reu": ddg_info["ddg"], "label_check": "failed", "trust": "不可信——编号错位嫌疑"}
        mf.set_status(run_dir, m, "failed")
        print(f"[workbench] ❌ {lcheck[1]}", file=sys.stderr)
        return 4

    # ── 8. 报告 + 收尾 ──
    report = mf.now_iso()
    m = mf.set_result(run_dir, m, {
        "ddg_reu": round(ddg_info["ddg"], 3),
        "wt_avg_reu": round(ddg_info["wt_avg"], 3),
        "mut_avg_reu": round(ddg_info["mut_avg"], 3),
        "n_rounds": ddg_info["n_rounds"],
        "direction": "destabilizing" if ddg_info["ddg"] > 0 else "stabilizing_or_neutral",
        "label_check": "passed",
        "ddg_file": d["ddg_file"],
        "units": "REU",
        "calc_type": "cartesian_ddg 常规稳定性模式",
        "finished_time": report,
    })
    m = mf.set_status(run_dir, m, "succeeded")

    from .report import render
    text = render(m, ddg_info, lcheck, numbering_note)
    with open(os.path.join(run_dir, "report.txt"), "w", encoding="utf-8") as f:
        f.write(text + "\n")
    print(text)
    print(f"\n[workbench] ✅ 完成: {run_dir}/report.txt | manifest.json | logs")
    return 0


if __name__ == "__main__":
    print(f"[workbench] {SCOPE.splitlines()[0]}")
    sys.exit(main())
