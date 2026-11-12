"""Rosetta 执行器：relax 与 cartesian_ddg（v0.3）

工程约定（对应规划 §2.3 P1 / §6 v0.3）：
  - 一律 subprocess.Popen(参数列表, start_new_session=True)：无 shell=True、
    无字符串拼接；子进程独立进程组，中断/超时可整组清理
  - 每次调用独立 cwd、独立日志文件、强制超时、可被信号协作中断
  - 成功判定 = 退出码 0 且【预期产物存在】，二者缺一即失败
  - 资源统计：子进程 CPU 时间（getrusage 差值）与子进程峰值 RSS（累计最大值）
"""

import glob
import hashlib
import os
import resource
import signal
import subprocess
import time

ROSETTA_BIN = os.getenv("ROSETTA_BIN_DIR", "/opt/software/rosetta/main/source/bin")
RELAX_EXE = os.path.join(ROSETTA_BIN, "relax.linuxgccrelease")
DDG_EXE = os.path.join(ROSETTA_BIN, "cartesian_ddg.linuxgccrelease")

# Rosetta 的选项解析器按空白切分参数，不支持含空格路径（本机 2026-11-12 实测复现）。
# 解法：给 Rosetta 的输入/输出路径一律经【无空格 staging 目录】以符号链接转接，
# 产物落回真实位置；staging 只含链接，删除无害。
STAGE_ROOT = "/tmp/wb_stage"


def _stage_dir(out_dir_abs: str, tag: str) -> str:
    key = hashlib.sha256(out_dir_abs.encode()).hexdigest()[:12]
    d = os.path.join(STAGE_ROOT, key, tag)
    os.makedirs(d, exist_ok=True)
    return d


def _symlink_force(target: str, link: str):
    if os.path.islink(link) or os.path.exists(link):
        try:
            os.remove(link)
        except OSError:
            pass
    os.symlink(target, link)


def _rosetta_safe_path(args: list, flag: str, real_path: str, out_dir_abs: str,
                       link_name: str) -> list:
    """若 real_path 含空格，则建立符号链接并替换 args 中 flag 后面的路径。

    返回（可能被替换的）参数列表副本；替换时在调用方记录 staging 事实。
    """
    if " " not in real_path:
        return args
    stage = _stage_dir(out_dir_abs, "links")
    link = os.path.join(stage, link_name)
    _symlink_force(real_path, link)
    new_args = list(args)
    idx = new_args.index(flag)
    new_args[idx + 1] = link
    return new_args

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

# 信号协作中断：cli 的 SIGINT/SIGTERM 处理器只置位，运行循环负责杀进程组
_STOP = {"flag": False}


class ExecutorError(RuntimeError):
    """外部程序执行失败（含退出码非 0 或产物缺失）"""


class Interrupted(ExecutorError):
    """收到中断信号（SIGINT/SIGTERM）被协作终止——区别于普通失败"""


def request_stop():
    """信号处理器调用：请求终止当前子进程（由运行循环执行）"""
    _STOP["flag"] = True


def check_dependencies() -> list:
    """返回缺失依赖清单；空列表 = 可执行"""
    missing = []
    for exe in (RELAX_EXE, DDG_EXE):
        if not os.path.exists(exe):
            missing.append(exe)
    return missing


def _rusage():
    ru = resource.getrusage(resource.RUSAGE_CHILDREN)
    return ru.ru_utime + ru.ru_stime, ru.ru_maxrss


def _kill_group(proc):
    """终止整个子进程组：先 SIGTERM，5 秒后仍存活则 SIGKILL"""
    try:
        pgid = os.getpgid(proc.pid)
    except ProcessLookupError:
        return
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return
    for _ in range(10):
        if proc.poll() is not None:
            return
        time.sleep(0.5)
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _run(args, cwd, log_path, timeout):
    """统一执行：参数列表、日志落盘、超时与信号中断、资源统计。

    返回 (returncode, duration_s, resource_dict)。
    """
    _STOP["flag"] = False
    t0 = time.time()
    cpu0, _rss0 = _rusage()
    with open(log_path, "w", encoding="utf-8") as log:
        proc = subprocess.Popen(
            args, cwd=cwd, stdout=log, stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        while True:
            rc = proc.poll()
            if rc is not None:
                break
            if _STOP["flag"]:
                _kill_group(proc)
                log.write("\n[workbench] 收到中断信号，子进程组已终止\n")
                raise Interrupted("收到中断信号（SIGINT/SIGTERM），任务已终止")
            if time.time() - t0 > timeout:
                _kill_group(proc)
                log.write(f"\n[workbench] 超时（>{timeout}s），进程组已终止\n")
                raise ExecutorError(f"执行超时（>{timeout}s）: {args[0]}")
            time.sleep(0.5)

    duration = round(time.time() - t0, 1)
    cpu1, rss1 = _rusage()
    res = {
        "cpu_time_s": round(cpu1 - cpu0, 1),
        # ru_maxrss 是所有子进程的累计峰值，非单步独占——如实标注
        "peak_rss_children_mb": round(rss1 / 1024, 1),
    }
    return rc, duration, res


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
    # Rosetta 不支持空格路径：经 staging 符号链接转接（产物仍落真实 out_dir）
    staged = args
    if " " in out_dir_abs or " " in os.path.abspath(input_pdb):
        staged = _rosetta_safe_path(args, "-s", os.path.abspath(input_pdb),
                                    out_dir_abs, "in.pdb")
        staged = _rosetta_safe_path(staged, "-out:path:all", out_dir_abs,
                                    out_dir_abs, "out")
    rc, dur, res = _run(staged, cwd=out_dir_abs, log_path=log_path, timeout=timeout)

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
        "resource": res,
        "version": rosetta_version(log_path),
        "command": args,
        "command_executed": staged,
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
    out_dir_abs = os.path.abspath(out_dir)
    log_path = os.path.join(out_dir_abs, "ddg.log")
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
    # Rosetta 不支持空格路径：输入侧经 staging 符号链接转接（.ddg 写入 cwd，不经选项解析）
    staged = args
    if " " in os.path.abspath(relaxed_pdb) or " " in os.path.abspath(mutfile_path):
        staged = _rosetta_safe_path(args, "-s", os.path.abspath(relaxed_pdb),
                                    out_dir_abs, "in.pdb")
        staged = _rosetta_safe_path(staged, "-ddg:mut_file", os.path.abspath(mutfile_path),
                                    out_dir_abs, "mut.txt")
    rc, dur, res = _run(staged, cwd=out_dir_abs, log_path=log_path, timeout=timeout)

    ddg_files = [f for f in glob.glob(os.path.join(out_dir_abs, "*.ddg"))
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
        "resource": res,
        "version": rosetta_version(log_path),
        "command": args,
        "command_executed": staged,
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
