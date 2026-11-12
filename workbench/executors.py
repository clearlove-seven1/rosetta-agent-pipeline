"""Rosetta 执行器：relax 与 cartesian_ddg（v0.2）

工程约定（对应规划 §2.3 P1）：
  - 一律 subprocess.run(参数列表)，无 shell=True、无字符串拼接
  - 每次调用独立 cwd、独立日志文件、强制超时
  - 成功判定 = 退出码 0 且【预期产物存在】，二者缺一即失败
"""

import glob
import os
import re
import subprocess
import time

ROSETTA_BIN = os.getenv("ROSETTA_BIN_DIR", "/opt/software/rosetta/main/source/bin")
RELAX_EXE = os.path.join(ROSETTA_BIN, "relax.linuxgccrelease")
DDG_EXE = os.path.join(ROSETTA_BIN, "cartesian_ddg.linuxgccrelease")

# 本机已验证的 relax 协议（与 2026-09-10 两次成功任务一致，去掉配体相关项）
RELAX_FLAGS = [
    "-relax:cartesian",
    "-score:weights", "ref2015_cart",
    "-relax:min_type", "lbfgs_armijo_nonmonotone",
    "-relax:constrain_relax_to_start_coords",
    "-relax:coord_constrain_sidechains",
    "-use_input_sc",
    "-ignore_unrecognized_res",
    "-ignore_zero_occupancy",
    "-missing_density_to_jump",
]


class ExecutorError(RuntimeError):
    """外部程序执行失败（含退出码非 0 或产物缺失）"""


def check_dependencies() -> list:
    """返回缺失依赖清单；空列表 = 可执行"""
    missing = []
    for exe in (RELAX_EXE, DDG_EXE):
        if not os.path.exists(exe):
            missing.append(exe)
    return missing


def _run(args, cwd, log_path, timeout):
    """统一执行：参数列表、日志落盘、超时保护。返回 (returncode, duration_s)。"""
    t0 = time.time()
    with open(log_path, "w", encoding="utf-8") as log:
        try:
            proc = subprocess.run(
                args, cwd=cwd, stdout=log, stderr=subprocess.STDOUT, timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            log.write(f"\n[workbench] 超时（>{timeout}s），进程已终止\n")
            raise ExecutorError(f"执行超时（>{timeout}s）: {args[0]}")
    return proc.returncode, round(time.time() - t0, 1)


def _tail(path, n=25):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return "".join(f.readlines()[-n:])
    except OSError:
        return "(日志不可读)"


def rosetta_version(log_path) -> str:
    """从日志提取 Rosetta 版本行；提取不到返回 unknown（不编造）"""
    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if "Rosetta version" in line:
                    return line.split("core.init:", 1)[-1].strip()
    except OSError:
        pass
    return "unknown"


def run_relax(input_pdb: str, out_dir: str, seed: int, nstruct: int = 1,
              timeout: int = 1800) -> dict:
    """Cartesian relax（单体）。成功返回产物信息，失败抛 ExecutorError。

    注意：Rosetta 进程 cwd 会切到 out_dir，所有传给 -out:path 的路径必须绝对。
    """
    os.makedirs(out_dir, exist_ok=True)
    out_dir_abs = os.path.abspath(out_dir)
    log_path = os.path.join(out_dir_abs, "relax.log")
    out_prefix = "relaxed_"
    args = [
        RELAX_EXE,
        "-s", os.path.abspath(input_pdb),
        *RELAX_FLAGS,
        "-nstruct", str(nstruct),
        "-run:constant_seed",
        "-run:jran", str(seed),
        "-out:path:all", out_dir_abs,
        "-out:prefix", out_prefix,
        "-out:file:scorefile", "score.sc",
    ]
    rc, dur = _run(args, cwd=out_dir_abs, log_path=log_path, timeout=timeout)

    pdbs = sorted(glob.glob(os.path.join(out_dir_abs, f"{out_prefix}*.pdb")))
    score = os.path.join(out_dir_abs, "score.sc")
    if rc != 0 or not pdbs or not os.path.exists(score):
        raise ExecutorError(
            f"relax 失败（退出码 {rc}, {dur}s, 产物 pdb={len(pdbs)}, score.sc={os.path.exists(score)}）\n"
            f"--- 日志尾部 ---\n{_tail(log_path)}"
        )
    # 多 nstruct 时取分数最低的；单 nstruct 只有一个
    if nstruct > 1:
        best = _best_by_score(score, pdbs)
    else:
        best = pdbs[0]
    return {
        "relaxed_pdb": best,
        "score_sc": score,
        "log": log_path,
        "returncode": rc,
        "duration_s": dur,
        "version": rosetta_version(log_path),
        "command": args,
    }


def _best_by_score(score_sc: str, pdbs: list) -> str:
    """解析 score.sc，返回 total_score 最低的 pdb 绝对路径"""
    scores = {}
    with open(score_sc, "r", encoding="utf-8", errors="replace") as f:
        header_idx = None
        for line in f:
            if line.startswith("SCORE:") and "total_score" in line:
                header_idx = line.split().index("total_score")
                continue
            if header_idx is not None and line.startswith("SCORE:"):
                parts = line.split()
                try:
                    scores[parts[-1]] = float(parts[header_idx])
                except (ValueError, IndexError):
                    continue
    best_desc = min(scores, key=scores.get) if scores else ""
    for p in pdbs:
        if os.path.basename(p) in best_desc or best_desc in p:
            return p
    return pdbs[0]


def run_cartesian_ddg(relaxed_pdb: str, mutfile_path: str, out_dir: str,
                      seed: int, iterations: int = 3,
                      timeout: int = 3600) -> dict:
    """cartesian_ddg 单突变（常规稳定性模式）。成功返回产物信息。"""
    os.makedirs(out_dir, exist_ok=True)
    log_path = os.path.join(out_dir, "ddg.log")
    args = [
        DDG_EXE,
        "-s", os.path.abspath(relaxed_pdb),
        "-ddg:mut_file", os.path.abspath(mutfile_path),
        "-ddg:iterations", str(iterations),
        "-ddg:cartesian",
        "-ddg:dump_pdbs", "false",
        "-ddg:bbnbrs", "1",
        "-score:weights", "ref2015_cart",
        "-relax:min_type", "lbfgs_armijo_nonmonotone",
        "-relax:cartesian",
        "-run:constant_seed",
        "-run:jran", str(seed),
        "-out:prefix", "ddg_",
    ]
    rc, dur = _run(args, cwd=out_dir, log_path=log_path, timeout=timeout)

    ddg_files = [f for f in glob.glob(os.path.join(out_dir, "*.ddg"))
                 if os.path.getsize(f) > 0]
    if rc != 0 or not ddg_files:
        raise ExecutorError(
            f"cartesian_ddg 失败（退出码 {rc}, {dur}s, 有效 .ddg={len(ddg_files)}）\n"
            f"--- 日志尾部 ---\n{_tail(log_path)}"
        )
    return {
        "ddg_file": ddg_files[0],
        "log": log_path,
        "returncode": rc,
        "duration_s": dur,
        "version": rosetta_version(log_path),
        "command": args,
    }


def check_seq_numbering_unchanged(input_pdb: str, relaxed_pdb: str) -> "tuple[bool, str]":
    """弛豫前后残基顺序/数量一致性校验：顺序编号错位会让 mutfile 打错位点（静默灾难）。"""
    from .validation import parse_pdb
    a = parse_pdb(input_pdb)
    b = parse_pdb(relaxed_pdb)
    if len(a.residues) != len(b.residues):
        return False, (f"弛豫后残基数变化: {len(a.residues)} → {len(b.residues)}，"
                       "顺序编号不可信，拒绝继续")
    for ra, rb in zip(a.residues, b.residues):
        if (ra.chain, ra.resnum, ra.aa1) != (rb.chain, rb.resnum, rb.aa1):
            return False, (f"弛豫后残基顺序变化: #{ra.seq_index} "
                           f"{ra.chain}:{ra.resnum}{ra.aa1} → {rb.chain}:{rb.resnum}{rb.aa1}，"
                           "顺序编号不可信，拒绝继续")
    return True, f"弛豫前后 {len(a.residues)} 个残基顺序一致"
