"""Workbench CLI：无 LLM 的确定性单点突变稳定性计算入口（v0.3）

用法示例（在仓库根目录、rosetta-agent env 下）：
  python -m workbench.cli \
    --protein examples/1ubq/1ubq.pdb --chain A --resnum 76 --to E \
    --name ubq_smoke --note "首次冒烟测试"

  # 断点恢复（输入/配置/产物逐项哈希校验，不匹配则拒绝复用并重算对应步骤）：
  python -m workbench.cli --resume runs/<run_id>

流程：输入校验 → 结构准备(剥配体/水) → cartesian relax → 编号一致性校验
     → cartesian_ddg → 解析+标签核对 → 报告。
状态机：created → validated → running → succeeded / failed / interrupted
退出码：0 成功 | 2 输入校验/恢复校验拒绝 | 3 依赖缺失 | 4 执行失败 | 130 中断
"""

import argparse
import glob
import os
import signal
import sys

from . import SCOPE
from . import executors, manifest as mf
from .ddgparse import label_matches, parse_ddg
from .numbering import build_numbering, mutfile_text, save_mapping_csv
from .resume import validate_resume
from .validation import ONE_TO_THREE, parse_pdb, validate_target


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="workbench",
        description="单体蛋白单点突变 Rosetta 稳定性计算（确定性，无 LLM）",
    )
    p.add_argument("--resume", metavar="RUN_DIR", default="",
                   help="恢复既有任务目录（互斥于其他输入参数）")
    p.add_argument("--protein", help="输入 PDB 路径（单体）")
    p.add_argument("--chain", help="目标链 ID（如 A）")
    p.add_argument("--resnum", type=int, help="PDB 晶体学残基编号")
    p.add_argument("--to", help="突变后氨基酸单字母（ACDEFGHIKLMNPQRSTVWY）")
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


def _install_signal_handlers():
    """SIGINT/SIGTERM → 协作式中断：只置位标志，由执行循环终止子进程组并落盘状态"""
    def _handler(signum, frame):
        executors.request_stop()
    signal.signal(signal.SIGTERM, _handler)
    signal.signal(signal.SIGINT, _handler)


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    _install_signal_handlers()
    try:
        if args.resume:
            return run_resume(os.path.abspath(args.resume))
        return run_new(args)
    except executors.Interrupted:
        return 130  # 常规中断；状态通常已由执行循环落盘
    except Exception as e:
        # 未预期崩溃兜底：manifest 不能停在 running 误导排查——如实置 failed 并留 traceback
        import traceback as _tb
        run_dir = mf._last.get("run_dir")
        m = mf._last.get("manifest")
        if run_dir and m:
            try:
                mf.record_step(run_dir, m, "crash", {
                    "status": "failed",
                    "error": repr(e)[:500],
                    "traceback": _tb.format_exc()[-2000:],
                })
                mf.set_status(run_dir, m, "failed")
            except Exception:
                pass
        traceback.print_exc()
        return 4


# ==========================================
# 新任务
# ==========================================
def run_new(args) -> int:
    if not (args.protein and args.chain and args.resnum and args.to):
        print("[workbench] 拒绝执行：--protein/--chain/--resnum/--to 均必填（或改用 --resume）。",
              file=sys.stderr)
        return 2

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

    # ── 2. 建任务目录与 manifest（同目录已存在则追加序号，绝不覆盖）──
    pdb_stem = os.path.splitext(os.path.basename(args.protein))[0]
    run_id = mf.new_run_id(pdb_stem, chain, args.resnum, mut)
    run_dir = os.path.join(args.workdir, run_id)
    n = 2
    while os.path.exists(run_dir):
        run_dir = os.path.join(args.workdir, f"{run_id}_{n}")
        n += 1
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

    # ── 3.5 结构准备：剥离 HETATM/水（单体口径只算蛋白；原始输入不动，产物落 run_dir）──
    clean_path = os.path.join(run_dir, "inputs", "clean_protein.pdb")
    prep_msg = _prepare_clean_input(summary, clean_path)
    m["task"]["cleaned_protein"] = clean_path
    m["task"]["cleaned_protein_sha256"] = mf.sha256(clean_path)
    m["task"]["prep_note"] = prep_msg
    mf.save(run_dir, m)
    print(f"[workbench] 结构准备: {prep_msg}")

    # ── 4-7. 执行（relax → 编号校验 → ddg → 解析）──
    try:
        m, r = _step_relax(run_dir, m, clean_path, args.seed, args.nstruct, args.timeout_relax)
        m, relaxed_pdb = _step_check_numbering(run_dir, m, clean_path, residue)
        ddg_dir = _ensure(os.path.join(run_dir, "ddg"))
        _write_mutfile(ddg_dir, residue.seq_index, residue.aa1, mut)
        m, d = _step_ddg(run_dir, m, relaxed_pdb, ddg_dir, args.seed,
                         args.iterations, args.timeout_ddg)
    except executors.Interrupted as e:
        mf.set_status(run_dir, m, "interrupted")
        print(f"[workbench] ⏸ 已中断（状态已落盘，可 --resume {run_dir} 恢复）: {e}", file=sys.stderr)
        return 130
    except executors.ExecutorError as e:
        mf.set_status(run_dir, m, "failed")
        print(f"[workbench] ❌ 执行失败:\n{e}", file=sys.stderr)
        return 4

    return _finalize(run_dir, m, d, numbering_note, relaxed_pdb, residue, mut)


# ==========================================
# 断点恢复
# ==========================================
def run_resume(run_dir: str) -> int:
    mpath = os.path.join(run_dir, "manifest.json")
    if not os.path.exists(mpath):
        print(f"[workbench] 恢复拒绝：{mpath} 不存在。", file=sys.stderr)
        return 2
    import json
    with open(mpath, "r", encoding="utf-8") as f:
        m = json.load(f)

    if m.get("status") == "succeeded":
        print(f"[workbench] 该任务已完成（succeeded），无需恢复：{run_dir}")
        return 0

    v = validate_resume(m)
    for note in v["notes"]:
        print(f"[workbench] 恢复检查: {note}")
    if not v["ok"]:
        for issue in v["fatal"]:
            print(f"[workbench] ❌ 恢复拒绝: {issue}", file=sys.stderr)
        return 2

    task = m["task"]
    run_id = m["run_id"]
    print(f"[workbench] 恢复任务 {run_id}（原状态: {m.get('status')}）")

    # 重新生成清洗产物（如缺失/不一致——从原始输入确定性重建）
    clean_path = task.get("cleaned_protein") or os.path.join(run_dir, "inputs", "clean_protein.pdb")
    if not (os.path.exists(clean_path) and _sha(clean_path) == task.get("cleaned_protein_sha256")):
        try:
            summary = parse_pdb(task["protein_path"])
        except FileNotFoundError as e:
            print(f"[workbench] {e}", file=sys.stderr)
            return 2
        prep_msg = _prepare_clean_input(summary, clean_path)
        m["task"]["cleaned_protein_sha256"] = mf.sha256(clean_path)
        print(f"[workbench] 结构准备（重建）: {prep_msg}")

    m.setdefault("resume_history", []).append({"time": mf.now_iso(), "from_status": m.get("status")})
    m = mf.set_status(run_dir, m, "running")

    cfg = m.get("config", {})
    try:
        # relax：复用已校验产物，否则重算（重算前清掉旧的不完整产物）
        if v["reusable"]["relax"]:
            relaxed_pdb = m["steps"]["relax"]["relaxed_pdb"]
            print(f"[workbench] ♻ 复用已校验 relax 产物: {os.path.basename(relaxed_pdb)}")
        else:
            _wipe(os.path.join(run_dir, "relax"), "*.pdb")
            m, _ = _step_relax(run_dir, m, clean_path, cfg.get("seed", 1111111),
                               cfg.get("nstruct", 1), cfg.get("timeout_relax_s", 1800))
            relaxed_pdb = m["steps"]["relax"]["relaxed_pdb"]

        # 编号一致性：无论复用与否都必须重验（产物是唯一事实）
        residue = _ResidueRef(task["seq_index"], task["wt_aa"], task["chain"], task["resnum_pdb"])
        m, relaxed_pdb = _step_check_numbering(run_dir, m, clean_path, residue)

        # ddg
        ddg_dir = os.path.join(run_dir, "ddg")
        if v["reusable"]["ddg"]:
            print(f"[workbench] ♻ 复用已校验 ddg 产物: "
                  f"{os.path.basename(m['steps']['cartesian_ddg']['ddg_file'])}")
        else:
            _wipe(ddg_dir, "*.ddg")
            mut_path = _write_mutfile(ddg_dir, residue.seq_index, residue.aa1, task["mut_aa"])
            m, _ = _step_ddg(run_dir, m, relaxed_pdb, ddg_dir, cfg.get("seed", 1111111),
                             cfg.get("ddg_iterations", 3), cfg.get("timeout_ddg_s", 3600))
    except executors.Interrupted as e:
        mf.set_status(run_dir, m, "interrupted")
        print(f"[workbench] ⏸ 再次中断（状态已落盘，可继续 --resume）: {e}", file=sys.stderr)
        return 130
    except executors.ExecutorError as e:
        mf.set_status(run_dir, m, "failed")
        print(f"[workbench] ❌ 执行失败:\n{e}", file=sys.stderr)
        return 4

    numbering_note = (
        f"PDB {task['chain']}:{task['resnum_pdb']} → 文件内顺序编号 #{task['seq_index']}"
        f"（恢复运行；映射表见 numbering_map.csv）"
    )
    return _finalize(run_dir, m, m["steps"].get("cartesian_ddg"), numbering_note,
                     relaxed_pdb, residue, task["mut_aa"])


# ==========================================
# 步骤实现（新任务与恢复共用）
# ==========================================
class _ResidueRef:
    """恢复模式下从 manifest 重建的目标位点引用（等价 validation.ResidueInfo 子集）"""
    def __init__(self, seq_index, aa1, chain, resnum):
        self.seq_index, self.aa1, self.chain, self.resnum = seq_index, aa1, chain, resnum
        self.icode = " "


def _sha(path):
    return mf.sha256(path)


def _prepare_clean_input(summary, clean_path: str) -> str:
    os.makedirs(os.path.dirname(clean_path), exist_ok=True)
    kept = 0
    with open(summary.path, "r", encoding="utf-8", errors="replace") as fin, \
         open(clean_path, "w", encoding="utf-8") as fout:
        for line in fin:
            if line.startswith("ATOM  "):
                fout.write(line)
                kept += 1
        fout.write("END\n")
    hetatm = dict(summary.hetatm_resnames) if summary.hetatm_resnames else "无"
    note = (f"剥离 HETATM/水（保留 {kept} 个蛋白原子行）；原始输入文件未修改。"
            f"剥离对象：{hetatm}" + (f"，另去除 {summary.n_water} 个水" if summary.n_water else ""))
    return note


def _step_relax(run_dir, m, relax_input, seed, nstruct, timeout):
    relax_dir = os.path.join(run_dir, "relax")
    print(f"[workbench] relax 中… (nstruct={nstruct}, seed={seed})")
    m = mf.record_step(run_dir, m, "relax", {"start_time": mf.now_iso()})
    r = executors.run_relax(relax_input, relax_dir, seed,
                            nstruct=nstruct, timeout=timeout)
    m = mf.record_step(run_dir, m, "relax", {
        "status": "succeeded", "duration_s": r["duration_s"],
        "returncode": r["returncode"], "command": r["command"],
        "relaxed_pdb": r["relaxed_pdb"],
        "relaxed_pdb_sha256": mf.sha256(r["relaxed_pdb"]),
        "log": r["log"], "resource": r["resource"],
    })
    m["software"]["rosetta"] = r["version"]
    mf.save(run_dir, m)
    print(f"[workbench] relax 完成 ({r['duration_s']}s, CPU {r['resource']['cpu_time_s']}s): "
          f"{os.path.basename(r['relaxed_pdb'])}")
    return m, r


def _step_check_numbering(run_dir, m, clean_path, residue):
    same, msg = executors.check_seq_numbering_unchanged(
        clean_path, m["steps"]["relax"]["relaxed_pdb"])
    if not same:
        mf.record_step(run_dir, m, "numbering_check", {"status": "failed", "detail": msg})
        mf.set_status(run_dir, m, "failed")
        raise executors.ExecutorError(msg)
    relaxed_pdb = m["steps"]["relax"]["relaxed_pdb"]
    _, _, by_seq_r = build_numbering(relaxed_pdb)
    if residue.seq_index not in by_seq_r or by_seq_r[residue.seq_index].aa1 != residue.aa1:
        msg = "目标位点在弛豫产物中不一致，拒绝继续"
        mf.record_step(run_dir, m, "numbering_check", {"status": "failed", "detail": msg})
        mf.set_status(run_dir, m, "failed")
        raise executors.ExecutorError(msg)
    m = mf.record_step(run_dir, m, "numbering_check", {"status": "succeeded", "detail": msg})
    return m, relaxed_pdb


def _write_mutfile(ddg_dir, seq_index, wt_aa1, mut_aa1) -> str:
    os.makedirs(ddg_dir, exist_ok=True)
    mut_path = os.path.join(ddg_dir, "mutfile.txt")
    with open(mut_path, "w", encoding="utf-8") as f:
        f.write(mutfile_text(seq_index, wt_aa1, mut_aa1))
    return mut_path


def _ensure(path):
    os.makedirs(path, exist_ok=True)
    return path


def _wipe(directory, pattern):
    """清除某步骤的不完整旧产物（重算前调用；动作会在日志中留痕）"""
    for f in glob.glob(os.path.join(directory, pattern)):
        try:
            os.remove(f)
        except OSError:
            pass


def _step_ddg(run_dir, m, relaxed_pdb, ddg_dir, seed, iterations, timeout):
    mut_path = os.path.join(ddg_dir, "mutfile.txt")
    if not os.path.exists(mut_path):
        raise executors.ExecutorError("mutfile.txt 缺失（内部错误）")
    print(f"[workbench] cartesian_ddg 中… (iterations={iterations})")
    m = mf.record_step(run_dir, m, "cartesian_ddg", {"start_time": mf.now_iso()})
    d = executors.run_cartesian_ddg(relaxed_pdb, mut_path, ddg_dir, seed,
                                    iterations=iterations, timeout=timeout)
    m = mf.record_step(run_dir, m, "cartesian_ddg", {
        "status": "succeeded", "duration_s": d["duration_s"],
        "returncode": d["returncode"], "command": d["command"],
        "ddg_file": d["ddg_file"], "ddg_file_sha256": mf.sha256(d["ddg_file"]),
        "log": d["log"], "resource": d["resource"],
    })
    print(f"[workbench] ddg 完成 ({d['duration_s']}s, CPU {d['resource']['cpu_time_s']}s)")
    return m, d


def _finalize(run_dir, m, d, numbering_note, relaxed_pdb, residue, mut) -> int:
    # 解析 + 标签核对
    try:
        ddg_info = parse_ddg(d["ddg_file"])
    except ValueError as e:
        mf.set_status(run_dir, m, "failed")
        print(f"[workbench] ❌ .ddg 解析失败: {e}", file=sys.stderr)
        return 4
    lcheck = label_matches(ddg_info, residue.seq_index, mut)
    if not lcheck[0]:
        m["result"] = {"ddg_reu": ddg_info["ddg"], "label_check": "failed",
                       "trust": "不可信——编号错位嫌疑"}
        mf.set_status(run_dir, m, "failed")
        print(f"[workbench] ❌ {lcheck[1]}", file=sys.stderr)
        return 4

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
        "finished_time": mf.now_iso(),
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
