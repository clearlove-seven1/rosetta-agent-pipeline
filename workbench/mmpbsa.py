"""Workbench MM/PBSA 执行器（v0.5-04）：对既有 MD 轨迹做端点法能量分析（探索性工具输出）

用法（仓库根目录）：
  python -m workbench.mmpbsa --md-run runs/gromacs/<run_id> --calc gb
  python -m workbench.mmpbsa --md-run runs/gromacs/<run_id> --calc pb --start-frame 101 --interval 4

口径与边界（必须随结果一起读）：
  * 本模块调用 gmx_MMPBSA（hdac8 env，1.7.0 / AmberTools 26.0）对【单个体系】做
    MM-GB/PB 端点能量分析（stability 模式，-s 旗标）；
  * 结果是"该体系的 MM+隐溶剂总能量"，不是结合自由能，也不是折叠自由能；
  * 未包含熵贡献（nmode 未做）；未做收敛性评估；协议未对实验数据验证；
  * 跨体系（WT vs 突变体）对比属于探索性观察——起点结构/拓扑不同、3ns 未收敛，
    任何差异都不得解读为突变效应，不得用于筛选/评分/论文结论。
  * gmx_MMPBSA 官方对 stability 模式的原话："It does not by itself calculate a
    binding free energy or an experimental folding free energy."

工程约定与 gromacs.py 一致：参数列表、进程组、协作中断、超时、哈希、manifest 同构。
关键集成参数（v0.5-04 探针实证）：
  * 稳定模式：gmx_MMPBSA -s -cg <g1> <g2>（两组【合并】定义溶质；组号为 0 基；
    单链蛋白拆两个残基段，并集覆盖全部残基，两段必须不同否则报错）；
  * PBRadii 属 &general（放 &gb 会报 Unknown variable）；igb=8 与 PBRadii=4 配对；
  * 轨迹需先 gmx trjconv -pbc mol 修分子完整性。
"""

import argparse
import json
import os
import re
import signal
import sys
import time

from . import executors
from . import manifest as mf
from .executors import ExecutorError, Interrupted, _tail
from .gromacs import _run_env, resolve_gmx

MMPBSA_ENV_DEFAULT = os.path.expanduser("~/.conda/envs/hdac8")

MMPBSA_SCOPE = (
    "任务类型: 单体系端点法能量分析（MM-GB/PB, gmx_MMPBSA stability 模式）\n"
    "输出量: 体系 MM+隐溶剂总能量及分量 (kJ/mol), 逐帧与均值±SD\n"
    "边界: 不含熵贡献; 协议未经验证; 结果不是结合/折叠自由能; 跨体系差异不得\n"
    "      解读为突变效应; 不得用于筛选/评分/论文结论。探索性工具输出。"
)


# ---------------------------------------------------------------------------
# 环境解析：gromacs_env.sh（gmx, cuFFT 层）+ hdac8 amber.sh（gmx_MMPBSA/sander）
# 顺序实测重要：先 GROMACS 再 amber.sh（后者不清 LD_LIBRARY_PATH）
# ---------------------------------------------------------------------------
def resolve_mmenv(repo_root: str, mmpbsa_env_root: str) -> dict:
    amber_sh = os.path.join(mmpbsa_env_root, "amber.sh")
    gmx_env_sh = os.path.join(repo_root, "configs", "gromacs_env.sh")
    inner = (
        f'[ -f "{gmx_env_sh}" ] && source "{gmx_env_sh}" >/dev/null 2>&1; '
        f'[ -f "{amber_sh}" ] && source "{amber_sh}" >/dev/null 2>&1; '
        "command -v gmx_MMPBSA; command -v gmx; command -v sander; env -0"
    )
    r = subprocess_run(inner)
    out = r.decode(errors="replace")
    lines = out.split("\n")
    if len(lines) < 3 or not lines[0].strip():
        raise ExecutorError(
            f"无法定位 gmx_MMPBSA（source {amber_sh} 失败？）输出: {out[:300]}")
    mm, gmx, sander = lines[0].strip(), lines[1].strip(), lines[2].strip()
    env = {}
    for kv in "\n".join(lines[3:]).split("\0"):
        if "=" in kv:
            k, _, v = kv.partition("=")
            env[k] = v
    ver = get_version(mm, env)
    return {"mm": mm, "gmx": gmx, "sander": sander, "env": env,
            "mmpbsa_env_root": mmpbsa_env_root, "version": ver}


def subprocess_run(inner: str) -> bytes:
    r = __import__("subprocess").run(
        ["bash", "-c", inner], capture_output=True, timeout=120,
        env={**os.environ, "PATH": os.environ.get("PATH", "/usr/bin:/bin:/usr/local/bin")},
    )
    return r.stdout


def get_version(mm: str, env: dict) -> dict:
    import subprocess
    r = subprocess.run([mm, "-v"], capture_output=True, env=env, timeout=120)
    m = re.search(r"gmx_MMPBSA\s+([\d.]+)", r.stdout.decode(errors="replace") + r.stderr.decode(errors="replace"))
    return {"gmx_MMPBSA": m.group(1) if m else "unknown"}


# ---------------------------------------------------------------------------
# 输出解析（对 1.7.0 实测格式）
# ---------------------------------------------------------------------------
def parse_dat(text: str) -> dict:
    """解析 FINAL_RESULTS .dat：按溶剂模型分节，抓 Complex/DELTA 表的分量均值。

    稳定模式输出节名如 "GENERALIZED BORN:" / "POISSON BOLTZMANN:"；
    表行形如 "TOTAL  -982.14  64.70  20.93 ..."（首列名称，第二列 Average）。
    """
    result: dict = {}
    section = None
    for raw in text.splitlines():
        line = raw.rstrip()
        m = re.match(r"^(GENERALIZED BORN|POISSON BOLTZMANN|GBNSR6|RISM)\s*:", line)
        if m:
            section = m.group(1)
            result.setdefault(section, {"components": {}, "total": None, "sd": None})
            continue
        if section is None:
            continue
        tokens = line.split()
        if len(tokens) >= 3 and tokens[0] in (
            "BOND", "ANGLE", "DIHED", "VDWAALS", "EEL", "1-4 VDW", "1-4 EEL",
            "EGB", "EPB", "ENPOLAR", "ESURF", "GGAS", "GSOLV", "TOTAL",
        ):
            try:
                avg = float(tokens[1])
                sd = float(tokens[2]) if len(tokens) > 2 else None
            except ValueError:
                continue
            result[section]["components"][tokens[0]] = avg
            if tokens[0] == "TOTAL":
                result[section]["total"] = avg
                result[section]["sd"] = sd
    if not result:
        raise ValueError("dat 中未找到能量分节（格式不符？）")
    return result


def parse_csv_frame_totals(path: str) -> dict:
    """解析 -eo csv 的逐帧 TOTAL 列 → 均值/SD/帧数（纯 python，零依赖）。

    1.7.0 实测格式：首行是节标题（如 "GENERALIZED BORN:"），随后才是
    "Frame #,BOND,...,TOTAL" 表头与数据行；帧号列为纯整数，可据此跳过其他节。
    """
    import csv as _csv
    with open(path, errors="replace") as f:
        rows = list(_csv.reader(f))
    header_idx = None
    for i, r in enumerate(rows):
        if "TOTAL" in r and any("Frame" in c for c in r):
            header_idx = i
            break
    if header_idx is None:
        raise ValueError("csv 中未找到 Frame#/TOTAL 表头")
    header = rows[header_idx]
    ti = header.index("TOTAL")
    vals = []
    for r in rows[header_idx + 1:]:
        if not r or not r[0].strip():
            continue
        if not r[0].strip().isdigit():
            break  # 进入下一节/汇总区，停止
        try:
            vals.append(float(r[ti]))
        except ValueError:
            pass
    n = len(vals)
    if n == 0:
        raise ValueError("csv 无有效数据行")
    mean = sum(vals) / n
    sd = (sum((v - mean) ** 2 for v in vals) / (n - 1)) ** 0.5 if n > 1 else 0.0
    return {"n_frames": n, "total_mean": mean, "total_sd": sd}


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def build_input(calc: str, startframe: int, endframe: int, interval: int,
                salt: float, igb: int, pbradii: int) -> str:
    lines = [
        "; Protein Design Workbench v0.5 MM/PBSA 探索性分析（stability 模式）",
        "&general",
        f"startframe={startframe}, endframe={endframe}, interval={interval},",
        f"verbose=2, PBRadii={pbradii},",
        "/",
    ]
    if calc == "gb":
        lines += ["&gb", f"igb={igb}, saltcon={salt},", "/"]
    elif calc == "pb":
        lines += ["&pb", f"istrng={salt},", "/"]
    else:
        raise ExecutorError(f"未知计算类型 {calc}")
    return "\n".join(lines) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="workbench-mmpbsa",
        description="MM-GB/PB 端点能量分析（探索性工具输出；非结合/折叠自由能）",
    )
    ap.add_argument("--md-run", required=True, help="已 succeeded 的 MD run 目录")
    ap.add_argument("--calc", choices=["gb", "pb"], default="gb")
    ap.add_argument("--start-frame", type=int, default=101,
                    help="起始帧（默认跳过前 1ns 平衡段，观察性）")
    ap.add_argument("--end-frame", type=int, default=0, help="0=最后一帧")
    ap.add_argument("--interval", type=int, default=4)
    ap.add_argument("--salt", type=float, default=0.15)
    ap.add_argument("--igb", type=int, default=8)
    ap.add_argument("--pbradii", type=int, default=4)
    ap.add_argument("--mmpbsa-env", default=MMPBSA_ENV_DEFAULT)
    ap.add_argument("--out-root", default="runs/mmpbsa")
    ap.add_argument("--timeout", type=int, default=7200)
    args = ap.parse_args(argv)

    def _sig(signum, frame):
        executors.request_stop()
    signal.signal(signal.SIGTERM, _sig)
    signal.signal(signal.SIGINT, _sig)

    try:
        return run_new(args)
    except Interrupted:
        return 130
    except ExecutorError as e:
        print(f"[workbench-mmpbsa] ❌ 失败:\n{e}", file=sys.stderr)
        return 4
    except Exception as e:
        import traceback
        traceback.print_exc()
        return 4


def run_new(args) -> int:
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    md_dir = os.path.abspath(args.md_run)
    mpath = os.path.join(md_dir, "manifest.json")
    if not os.path.isfile(mpath):
        print(f"[workbench-mmpbsa] 拒绝：{mpath} 不存在", file=sys.stderr)
        return 2
    m = json.load(open(mpath, encoding="utf-8"))
    if m.get("status") != "succeeded":
        print(f"[workbench-mmpbsa] 拒绝：MD run 状态 {m.get('status')} ≠ succeeded", file=sys.stderr)
        return 2

    md_dir_rel = md_dir
    tpr = os.path.join(md_dir_rel, "md.tpr")
    xtc = os.path.join(md_dir_rel, "md.xtc")
    topol = os.path.join(md_dir_rel, "topol.top")
    for p in (tpr, xtc, topol):
        if not os.path.isfile(p):
            print(f"[workbench-mmpbsa] 拒绝：缺少 {p}", file=sys.stderr)
            return 2

    # 帧范围：xtc 实际帧数从 rmsd_bb.xvg 推断（分析步已记录）
    n_frames = ((m.get("analysis_summary") or {}).get("n_frames_xtc"))
    if not n_frames:
        print("[workbench-mmpbsa] 拒绝：manifest 缺少 n_frames_xtc", file=sys.stderr)
        return 2
    end_frame = args.end_frame or n_frames
    if not (1 <= args.start_frame <= end_frame <= n_frames):
        print(f"[workbench-mmpbsa] 拒绝：帧范围 1..{n_frames} 内需 1<=start<=end", file=sys.stderr)
        return 2

    out_dir = os.path.abspath(os.path.join(
        args.out_root, f"{time.strftime('%Y%m%d_%H%M%S')}_{args.calc}_{m['run_id']}"))
    if os.path.exists(out_dir):
        print(f"[workbench-mmpbsa] 拒绝：目录已存在 {out_dir}", file=sys.stderr)
        return 2
    os.makedirs(os.path.join(out_dir, "logs"), exist_ok=True)

    g = resolve_gmx(repo_root)
    mm = resolve_mmenv(repo_root, os.path.abspath(os.path.expanduser(args.mmpbsa_env)))
    print(f"[workbench-mmpbsa] gmx_MMPBSA={mm['version'].get('gmx_MMPBSA')} "
          f"(env={mm['mmpbsa_env_root']}); gmx={g['gmx']}")

    env = mm["env"]
    info = {
        "md_run": m["run_id"], "md_run_dir": md_dir_rel,
        "md_inputs_sha256": {os.path.basename(p): mf.sha256(p) for p in (tpr, xtc, topol)},
        "calc": args.calc, "start_frame": args.start_frame,
        "end_frame": end_frame, "interval": args.interval,
        "salt_M": args.salt, "igb": args.igb if args.calc == "gb" else None,
        "pbradii": args.pbradii,
        "n_frames_sampled": len(range(args.start_frame, end_frame + 1, args.interval)),
        "software": {"gmx_MMPBSA": mm["version"], "gmx": g["version"].get("GROMACS version"),
                     "env_root": mm["mmpbsa_env_root"]},
        "scope_and_limits": MMPBSA_SCOPE,
    }

    run_id = os.path.basename(out_dir)
    man = {
        "schema": "workbench-manifest/0.2",
        "run_id": run_id, "status": "created",
        "status_history": [{"status": "created", "time": mf.now_iso()}],
        "task": {"calc_type": "MM-GB/PB 端点能量分析（stability 模式, 探索性）",
                 "md_run": m["run_id"], **info},
        "software": {"gmx_MMPBSA_env": mm["mmpbsa_env_root"],
                     "gmx_MMPBSA_version": mm["version"].get("gmx_MMPBSA"),
                     "git_commit": mf.git_commit(repo_root)},
        "steps": {}, "result": None, "scope_and_limits": MMPBSA_SCOPE,
    }
    mf.save(out_dir, man)
    man = mf.set_status(out_dir, man, "running")

    def step(name, argv, stdin_bytes=None, timeout=900, cwd=out_dir):
        log_path = os.path.join(out_dir, "logs", f"{name}.log")
        rc, dur, res = _run_env(argv, cwd=cwd, log_path=log_path, timeout=timeout,
                                env=env, stdin_bytes=stdin_bytes)
        if rc != 0:
            raise ExecutorError(f"{name} 失败（退出码 {rc}）\n{_tail(log_path)}")
        man2 = mf.record_step(out_dir, man, name, {
            "status": "succeeded", "command": argv, "duration_s": dur,
            "resource": res, "log": os.path.relpath(log_path, out_dir)})
        return man2, dur

    try:
        # 1) PBC 修正轨迹（分子完整性）
        pbc_xtc = os.path.join(out_dir, "md_pbc.xtc")
        man, _ = step("trjconv_pbc",
                      [g["gmx"], "trjconv", "-s", tpr, "-f", xtc,
                       "-o", pbc_xtc, "-pbc", "mol"], stdin_bytes=b"System\n")
        # 2) index：单链蛋白按残基中点拆两段（两段必须不同；0 基组号）
        ic = m["task"]["input_check"]
        lo, hi = ic["resseq_min"], ic["resseq_max"]
        mid = (lo + hi) // 2
        index_ndx = os.path.join(out_dir, "index.ndx")
        man, _ = step("make_ndx",
                      [g["gmx"], "make_ndx", "-f", tpr, "-o", index_ndx],
                      stdin_bytes=f"r {lo}-{mid}\nr {mid + 1}-{hi}\nq\n".encode())
        n_sections = sum(1 for ln in open(index_ndx) if re.match(r"^ *\[", ln))
        g1, g2 = n_sections - 2, n_sections - 1  # 0 基（新增两组在末尾）
        man["task"]["complex_groups_0based"] = [g1, g2]
        man["task"]["residue_split"] = [f"{lo}-{mid}", f"{mid + 1}-{hi}"]
        mf.save(out_dir, man)
        # 3) 输入文件
        inp = os.path.join(out_dir, "mmpbsa.in")
        with open(inp, "w", encoding="utf-8") as f:
            f.write(build_input(args.calc, args.start_frame, end_frame,
                                args.interval, args.salt, args.igb, args.pbradii))
        man["task"]["mmpbsa_in"] = open(inp, encoding="utf-8").read()
        mf.save(out_dir, man)
        # 4) gmx_MMPBSA
        dat = os.path.join(out_dir, f"FINAL_RESULTS_{args.calc.upper()}.dat")
        csvf = os.path.join(out_dir, f"FINAL_ENERGY_{args.calc.upper()}.csv")
        timeout = args.timeout if args.calc == "gb" else max(args.timeout, 14400)
        man, _ = step("gmx_MMPBSA",
                      [mm["mm"], "-O", "-i", os.path.basename(inp),
                       "-cs", tpr, "-ct", pbc_xtc, "-ci", os.path.basename(index_ndx),
                       "-s", "-cg", str(g1), str(g2), "-cp", topol,
                       "-o", os.path.basename(dat), "-eo", os.path.basename(csvf),
                       "-nogui", "--progress-style", "plain"],
                      timeout=timeout)
    except Interrupted as e:
        mf.set_status(out_dir, man, "interrupted")
        print(f"[workbench-mmpbsa] ⏸ 已中断: {e}", file=sys.stderr)
        return 130
    except ExecutorError as e:
        mf.set_status(out_dir, man, "failed")
        print(f"[workbench-mmpbsa] ❌ 执行失败:\n{e}", file=sys.stderr)
        return 4

    # 5) 解析
    try:
        parsed = parse_dat(open(dat, errors="replace").read())
        frames = parse_csv_frame_totals(csvf)
    except (ValueError, OSError) as e:
        mf.set_status(out_dir, man, "failed")
        print(f"[workbench-mmpbsa] ❌ 输出解析失败: {e}", file=sys.stderr)
        return 4
    man["result"] = {
        "sections": parsed,
        "frame_totals": frames,
        "calc": args.calc,
        "convergence": "not_assessed — 端点法分析不评估收敛",
        "scientific_conclusion": (
            "none — 探索性工具输出；不含熵贡献、协议未经验证；"
            "不是结合/折叠自由能；跨体系差异不得解读为突变效应"),
        "finished_time": mf.now_iso(),
    }
    man["result_outputs"] = {
        os.path.basename(dat): {"sha256": mf.sha256(dat), "bytes": os.path.getsize(dat)},
        os.path.basename(csvf): {"sha256": mf.sha256(csvf), "bytes": os.path.getsize(csvf)},
    }
    mf.set_status(out_dir, man, "succeeded")
    sec = list(parsed.keys())[0]
    print(f"\n[workbench-mmpbsa] ✅ 完成: {out_dir}\n"
          f"[workbench-mmpbsa] {sec} TOTAL = {parsed[sec]['total']:.2f} ± "
          f"{parsed[sec]['sd']:.2f} kJ/mol ({frames['n_frames']} 帧)\n"
          f"[workbench-mmpbsa] 边界: 探索性工具输出; 非结合/折叠自由能; 未验证协议")
    return 0


if __name__ == "__main__":
    sys.exit(main())
