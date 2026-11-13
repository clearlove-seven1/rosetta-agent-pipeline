"""Workbench GROMACS MD 执行器（v0.5-02）：EM → NVT → NPT → 生产 MD 的确定性流程

用法（仓库根目录、rosetta-agent env）：
  python -m workbench.gromacs \
    --protein runs/<v04_run>/inputs/clean_protein.pdb --chain A --label 2hbb_A \
    --ns 1.0

  # 断点恢复（逐阶段产物哈希校验；配置/输入不匹配则拒绝恢复）：
  python -m workbench.gromacs --resume runs/gromacs/<run_id>

口径与边界（MD_SCOPE，与 manifest 一致）：
  - 本模块执行确定性 MD 流程并完整记录参数/种子/命令/退出码/GPU 证据；
  - 流程成功 ≠ 动力学收敛 ≠ 任何科学结论；科学解释由使用者负责；
  - ns > 5 需显式 --confirm-long-run（时长本身不赋予结论效力）；
  - GPU 生效判定 = mdrun 日志 + 运行期 nvidia-smi 采样双证据，不以"机器有卡"为准。

工程约定与 Rosetta 侧（workbench/executors.py）一致：
  - subprocess 参数列表、独立 cwd、独立日志、进程组中断、强制超时、资源统计；
  - 成功判定 = 退出码 0 且预期产物存在且哈希入档；
  - 状态机 created → validated → running → succeeded / failed / interrupted；
  - 退出码 0 成功 | 2 校验/护栏/恢复拒绝 | 3 依赖缺失 | 4 执行失败 | 130 中断。

v0.5-01 冒烟版的教训已固化在代码里：
  - ions 准备步用 cut-off 静电（净电荷体系 + PME 会被 grompp 拒绝；该 tpr 仅作
    genion 输入）；
  - NVT/NPT 的 POSRES 必须显式 -r 参考坐标（GROMACS ≥2018）；
  - 全阶段固定种子（ld-seed / gen-seed），不再留随机种子的追溯负担；
  - GPU 显存判定用「峰值 − 该卡基线 > 200 MiB」，不用固定绝对阈值。
"""

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time

from . import executors
from . import manifest as mf
from .executors import ExecutorError, Interrupted, _kill_group, _rusage, _STOP, _tail
from .validation import THREE_TO_ONE, parse_pdb

MD_SCOPE = (
    "任务类型: 单链蛋白分子动力学流程（EM → NVT → NPT → 生产 MD）\n"
    "Protocol: pdb2gmx → editconf → solvate → genion → steep EM → V-rescale/C-rescale 平衡 → 生产 MD\n"
    "输出量: 轨迹与能量观察统计（RMSD/RMSF/T/P/势能），单位 nm/ps/K/bar/kJ/mol\n"
    "边界: 流程成功不代表动力学收敛，不构成任何科学结论，不得用于筛选/评分/亲和力判断；\n"
    "      计算筛选 ≠ 实验验证。科学问题设计与结果解释由使用者负责。"
)

DEFAULT_SEED = 20261113
LONG_RUN_NS = 5.0  # 超过该时长需 --confirm-long-run（不改变任何结论边界）

# GPU 取证：mdrun 日志关键字（宽匹配，证据原文入 manifest）
GPU_EVIDENCE_KEYWORDS = (
    "GPU", "CUDA", "cuFFT", "Performance", "Using ", "task assignment",
    "Selected", "auto-selected", "nodata",
)


# ==========================================
# 环境解析
# ==========================================
def resolve_gmx(repo_root: str) -> dict:
    """source configs/gromacs_env.sh 后回收完整环境与 gmx 路径（含版本信息）。

    命令为固定脚本字符串（不含用户输入），仍以参数列表调用 bash。
    返回 {"gmx": 路径, "env": 环境字典, "version": 关键字段, "mode": "env_script"}。
    """
    env_sh = os.path.join(repo_root, "configs", "gromacs_env.sh")
    inner = (
        f'if [ -f "{env_sh}" ]; then source "{env_sh}" >/dev/null 2>&1; fi; '
        "command -v gmx; env -0"
    )
    r = subprocess.run(
        ["bash", "-c", inner], capture_output=True, timeout=120,
        env={**os.environ, "PATH": os.environ.get("PATH", "/usr/bin:/bin:/usr/local/bin")},
    )
    out = r.stdout.decode(errors="replace")
    if not out or "\n" not in out:
        raise ExecutorError(
            f"无法定位 gmx（source {env_sh} 失败？）。可改用 --gmx <绝对路径> 显式指定。\n"
            f"stderr: {r.stderr.decode(errors='replace')[:300]}"
        )
    gmx_path, env_blob = out.split("\n", 1)
    env: dict = {}
    for kv in env_blob.split("\0"):
        if "=" in kv:
            k, _, v = kv.partition("=")
            env[k] = v
    info = {
        "gmx": gmx_path.strip(),
        "env": env,
        "version": _gmx_version(gmx_path.strip(), env),
        "mode": "env_script",
        "env_script": os.path.relpath(env_sh, repo_root),
    }
    if not info["version"]:
        raise ExecutorError(f"gmx --version 无输出（{gmx_path}），依赖不可用")
    return info


def resolve_explicit_gmx(gmx_path: str) -> dict:
    """显式指定 gmx 二进制（不经 env 脚本；用于测试或特殊环境）。"""
    if not os.path.isfile(gmx_path):
        raise FileNotFoundError(f"--gmx 指定的二进制不存在: {gmx_path}")
    env = {**os.environ}
    return {"gmx": os.path.abspath(gmx_path), "env": env,
            "version": _gmx_version(os.path.abspath(gmx_path), env),
            "mode": "explicit_path", "env_script": ""}


def _gmx_version(gmx: str, env: dict) -> dict:
    keys = ("GROMACS version", "Precision", "Memory model", "GPU support",
            "GPU FFT library", "CUDA runtime", "CUDA driver", "SIMD instructions")
    got: dict = {}
    try:
        r = subprocess.run([gmx, "--version"], capture_output=True, env=env, timeout=120)
        for line in r.stdout.decode(errors="replace").splitlines():
            for k in keys:
                if line.strip().startswith(k + ":"):
                    got[k] = line.split(":", 1)[1].strip()
    except (OSError, subprocess.TimeoutExpired):
        return {}
    return got


# ==========================================
# 输入校验（在耗费任何 gmx 时间之前）
# ==========================================
def validate_md_input(summary) -> "tuple[bool, str, dict]":
    """MD 口径校验：单链、无 HETATM、无多 MODEL、全标准残基。"""
    if summary.has_multi_model:
        return False, "拒绝执行：PDB 含多个 MODEL。请先提取单一模型再提交。", {}
    if summary.hetatm_resnames or summary.n_water:
        return False, (
            f"拒绝执行：检测到 HETATM/水（{dict(summary.hetatm_resnames) or ''}"
            f"{'，水 ×' + str(summary.n_water) if summary.n_water else ''}）。"
            "MD 口径要求干燥的单链蛋白输入（Rosetta 清洗产物 inputs/clean_protein.pdb 即符合）。"
        ), {}
    if len(summary.chains) != 1:
        found = ", ".join(f"'{c}'({len(rs)} 残基)" for c, rs in summary.chains.items()) or "无"
        return False, f"拒绝执行：MD 流程 v0.5 仅支持单链。文件中的链: {found}", {}
    # 非标准残基：parse_pdb 只统计标准氨基酸，需要显式扫描 ATOM 记录找漏网残基
    nonstandard = set()
    with open(summary.path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            if line.startswith("ATOM  "):
                aa3 = line[17:20].strip()
                if aa3 and aa3 not in THREE_TO_ONE:
                    nonstandard.add(aa3)
    if nonstandard:
        return False, (
            f"拒绝执行：检测到非标准残基 {sorted(nonstandard)}（修饰残基/翻译后修饰）。"
            "pdb2gmx 会失败或静默丢失，请先处理。"
        ), {}
    chain, residues = next(iter(summary.chains.items()))
    nums = [r.resnum for r in residues]
    info = {
        "chain": chain,
        "n_residues": len(residues),
        "resseq_min": min(nums),
        "resseq_max": max(nums),
        "resseq_contiguous": max(nums) - min(nums) + 1 == len(nums),
        "n_atom_records_atoms": None,
    }
    note = (f"校验通过: 链 '{chain}'，{len(residues)} 残基 "
            f"（{min(nums)}–{max(nums)}，连续={info['resseq_contiguous']}），无 HETATM/MODEL")
    if not info["resseq_contiguous"]:
        note += "（注意：编号不连续，RMSF 残基号映射将按 CA 顺序标注）"
    return True, note, info


# ==========================================
# mdp 生成（全部种子固定）
# ==========================================
def build_mdp(kind: str, p: dict) -> str:
    lines = [f"; {kind} — Protein Design Workbench v0.5 MD（种子与参数全部显式）"]
    vals = {
        "nsteps": p[f"{kind}_nsteps"] if kind in ("nvt", "npt", "md") else 50000,
        "nstlist": 10, "cutoff-scheme": "Verlet", "pbc": "xyz",
        # rcoulomb 必须与 rvdw 同值：Verlet 下 rcoulomb!=rvdw 仅在 PME 且
        # rcoulomb>rvdw 时允许（ions 步 cut-off 静电直接报错——v0.5-02 实测）
        "rvdw": p["cutoff_nm"], "rcoulomb": p["cutoff_nm"],
        "nstlog": 500, "nstenergy": 500,
    }
    if kind in ("ions", "em"):
        vals["integrator"] = "steep"
        vals["dt"] = 0.01
        vals["emtol"] = p["emtol"]
        vals["emstep"] = 0.01
        if kind == "ions":
            # ions 准备步专用（官方教程做法）：solvate 后体系带净电荷，PME 会触发
            # "Ewald electrostatics in a system with net charge" 警告并使 grompp 失败；
            # 该 tpr 仅用于 genion 选水替换，不用于任何动力学，故用 cut-off。
            vals["coulombtype"] = "cut-off"
            vals["nstenergy"] = 0
            vals["nstlog"] = 0
        else:
            vals["coulombtype"] = "PME"
    else:
        vals.update({
            "integrator": "md", "dt": p["dt_ps"],
            "constraints": p["constraints"],
            "nstxout-compressed": p["nstxout-compressed"],
            "coulombtype": "PME", "rcoulomb": p["cutoff_nm"],
            "tcoupl": "V-rescale", "tc-grps": "System",
            "tau-t": p["tau_t"], "ref-t": p["ref_t"],
            "ld-seed": p["seed"],  # V-rescale/C-rescale 的随机性也由该种子固定
        })
    if kind in ("nvt", "npt"):
        vals["define"] = "-DPOSRES"  # pdb2gmx 的 posre.itp，1000 kJ/mol/nm²
    if kind == "nvt":
        vals.update({"pcoupl": "no", "gen-vel": "yes",
                     "gen-temp": p["ref_t"], "gen-seed": p["seed"]})
    if kind in ("npt", "md"):
        vals.update({
            "pcoupl": "C-rescale", "pcoupltype": "isotropic",
            "tau-p": p["tau_p"], "ref-p": p["ref_p"],
            "compressibility": p["compressibility"], "ref-coordscaling": "com",
            "continuation": "yes", "gen-vel": "no",
        })
    for k, v in vals.items():
        lines.append(f"{k:<22} = {v}")
    return "\n".join(lines) + "\n"


# ==========================================
# GPU 取证
# ==========================================
def extract_gpu_evidence(md_log_path: str) -> dict:
    """从 mdrun 日志提取 GPU 相关证据行（原文）与性能数字。"""
    try:
        lines = open(md_log_path, "r", errors="replace").read().splitlines()
    except OSError:
        return {"log": os.path.basename(md_log_path), "matched_lines": [],
                "gpu_selected_in_log": False, "performance_ns_day": None, "error": "日志不可读"}
    evidence = [ln.rstrip() for ln in lines
                if any(k in ln for k in GPU_EVIDENCE_KEYWORDS)][:120]
    perf = None
    for ln in lines:
        m = re.search(r"Performance:\s*([\d.]+)", ln)
        if m:
            perf = float(m.group(1))
    gpu_selected = any(re.search(r"GPU", ln) and re.search(r"select|Using|assign|run on", ln, re.I)
                       for ln in evidence)
    return {"log": os.path.basename(md_log_path), "matched_lines": evidence,
            "gpu_selected_in_log": gpu_selected, "performance_ns_day": perf}


class GpuSampler:
    """生产段期间对 nvidia-smi 采样：独立于日志的第二证据源。"""

    def __init__(self, interval_s: float = 5.0):
        self.interval = interval_s
        self.rows: list = []
        self._stop = threading.Event()
        self._thread = None
        self.available = False
        try:
            subprocess.run(["nvidia-smi", "--query-gpu=index", "--format=csv,noheader"],
                           capture_output=True, timeout=30)
            self.available = True
        except (OSError, subprocess.TimeoutExpired):
            self.available = False

    def start(self):
        if not self.available:
            return

        def loop():
            while not self._stop.is_set():
                try:
                    r = subprocess.run(
                        ["nvidia-smi", "--query-gpu=index,memory.used,utilization.gpu",
                         "--format=csv,noheader,nounits"],
                        capture_output=True, timeout=30)
                    ts = time.time()
                    for line in r.stdout.decode(errors="replace").splitlines():
                        self.rows.append(f"{ts:.0f},{line.strip()}")
                except Exception:
                    pass
                self._stop.wait(self.interval)

        self._thread = threading.Thread(target=loop, daemon=True)
        self._thread.start()

    def stop_and_save(self, run_dir: str, target_gpu: int, baseline: dict) -> dict:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=self.interval * 2)
        out = os.path.join(run_dir, "gpu", "nvidia_samples.csv")
        os.makedirs(os.path.dirname(out), exist_ok=True)
        with open(out, "w", encoding="utf-8") as f:
            f.write("epoch,gpu_index,mem_mib,util_pct\n" + "\n".join(self.rows) + "\n")
        max_mem: dict = {}
        for row in self.rows:
            parts = row.split(",")
            if len(parts) >= 3:
                try:
                    gi, mem = int(parts[1]), int(parts[2])
                    max_mem[gi] = max(max_mem.get(gi, 0), mem)
                except ValueError:
                    pass
        base = (baseline or {}).get(target_gpu, 0)
        peak = max_mem.get(target_gpu, 0)
        return {
            "n_samples": len(self.rows),
            "csv": os.path.relpath(out, run_dir),
            "available": self.available,
            "max_mem_mib_per_gpu": {str(k): v for k, v in sorted(max_mem.items())},
            "target_gpu_baseline_mem_mib": base,
            "target_gpu_peak_mem_mib": peak,
            # 判定：峰值较该卡基线上升 >200 MiB（GROMACS CUDA context 量级随体系变化，
            # 固定绝对阈值会在小体系上误判——v0.5-01 实测教训）
            "gpu_process_observed": (peak - base) > 200,
        }


def assess_gpu(log_ev: dict, nvidia_ev: dict) -> dict:
    log_ok = log_ev.get("gpu_selected_in_log")
    if not nvidia_ev.get("available"):
        concl = "仅日志证据（无 nvidia-smi 可用，未做运行期采样）：日志%s GPU" % (
            "选择" if log_ok else "未显示选择")
        return {"log_says_gpu_selected": log_ok, "nvidia_smi_process_observed": None,
                "performance_ns_day": log_ev.get("performance_ns_day"), "conclusion": concl}
    ok = bool(log_ok) and bool(nvidia_ev.get("gpu_process_observed"))
    concl = ("GPU 实际参与（mdrun 日志选择 GPU 且 nvidia-smi 观察到进程驻留）" if ok
             else "未确认 GPU 生效——以日志与采样原文为准，如实记录")
    return {"log_says_gpu_selected": log_ok, "nvidia_smi_process_observed": nvidia_ev.get("gpu_process_observed"),
            "performance_ns_day": log_ev.get("performance_ns_day"), "conclusion": concl}


# ==========================================
# 执行（executors._run 的 env 感知变体；共享中断/进程组/资源统计）
# ==========================================
def _run_env(args, cwd, log_path, timeout, env, stdin_bytes=None):
    _STOP["flag"] = False
    t0 = time.time()
    cpu0, _rss0 = _rusage()
    with open(log_path, "w", encoding="utf-8") as log:
        log.write("$ " + " ".join(args) +
                  (f"\n# stdin: {stdin_bytes.decode(errors='replace')!r}" if stdin_bytes else "") + "\n")
        log.flush()
        proc = subprocess.Popen(
            args, cwd=cwd, stdout=log, stderr=subprocess.STDOUT,
            stdin=subprocess.PIPE if stdin_bytes else None,
            start_new_session=True, env=env,
        )
        if stdin_bytes:
            try:
                proc.stdin.write(stdin_bytes)
                proc.stdin.close()
            except (BrokenPipeError, OSError):
                pass
        while True:
            rc = proc.poll()
            if rc is not None:
                break
            if _STOP["flag"]:
                _kill_group(proc)
                log.write("\n[workbench-md] 收到中断信号，子进程组已终止\n")
                raise Interrupted("收到中断信号（SIGINT/SIGTERM），任务已终止")
            if time.time() - t0 > timeout:
                _kill_group(proc)
                log.write(f"\n[workbench-md] 超时（>{timeout}s），进程组已终止\n")
                raise ExecutorError(f"执行超时（>{timeout}s）: {args[0]}")
            time.sleep(0.5)
    duration = round(time.time() - t0, 1)
    cpu1, rss1 = _rusage()
    return rc, duration, {
        "cpu_time_s": round(cpu1 - cpu0, 1),
        "peak_rss_children_mb": round(rss1 / 1024, 1),  # 子进程累计峰值，非单步独占
    }


def _exec(name, argv, cwd, timeout, env, stdin_bytes=None) -> dict:
    """执行单条外部命令（日志落盘），返回执行信息；退出码非 0 即抛错。"""
    log_path = os.path.join(cwd, "logs", f"{name}.log")
    rc, dur, res = _run_env(argv, cwd=cwd, log_path=log_path, timeout=timeout,
                            env=env, stdin_bytes=stdin_bytes)
    if rc != 0:
        raise ExecutorError(
            f"{name} 失败（退出码 {rc}, {dur}s）\n--- 日志尾部 ---\n{_tail(log_path)}")
    return {"returncode": rc, "duration_s": dur, "resource": res,
            "log": os.path.relpath(log_path, cwd)}


def _check_outputs(name, out_dir, expect) -> dict:
    """「退出码 0」之外的第二重判定：预期产物必须存在且非空；返回 sha256 表。"""
    outs = {}
    for fname in expect:
        fpath = os.path.join(out_dir, fname)
        if not os.path.isfile(fpath) or os.path.getsize(fpath) == 0:
            raise ExecutorError(f"{name} 失败：预期产物缺失或为空: {fname}")
        outs[fname] = mf.sha256(fpath)
    return outs


def _stage_reusable(m, name, run_dir) -> bool:
    """恢复模式：该阶段记录的产物哈希与当前文件全部一致才可复用。"""
    st = m.get("steps", {}).get(name, {})
    if st.get("status") != "succeeded":
        return False
    outs = st.get("outputs", {})
    if not outs:
        return False
    for fname, sha in outs.items():
        fpath = os.path.join(run_dir, fname)
        if not os.path.isfile(fpath) or mf.sha256(fpath) != sha:
            return False
    return True


# ==========================================
# 分析（RMSD / RMSF / 能量 → xvg + 图 + summary）
# ==========================================
def _read_xvg(path):
    import numpy as np
    rows, legends = [], {}
    if not os.path.isfile(path):
        raise ExecutorError(f"分析产物缺失: {path}")
    for line in open(path, "r", errors="replace"):
        line = line.strip()
        if line.startswith("@ s") and "legend" in line:
            mm = re.match(r'@ s(\d+) legend "(.+)"', line)
            if mm:
                legends[int(mm.group(1))] = mm.group(2)
        if not line or line[0] in "#@":
            continue
        rows.append([float(x) for x in line.split()])
    return (np.array(rows) if rows else None), legends


def run_analysis(run_dir, g, p, input_info):
    ana = os.path.join(run_dir, "analysis")
    os.makedirs(ana, exist_ok=True)

    def tool(name, args, stdin_bytes):
        log_path = os.path.join(run_dir, "logs", f"ana_{name}.log")
        rc, dur, res = _run_env([g["gmx"], *args], cwd=run_dir, log_path=log_path,
                                timeout=900, env=g["env"], stdin_bytes=stdin_bytes)
        if rc != 0:
            raise ExecutorError(f"分析步 {name} 失败（退出码 {rc}）\n" + _tail(log_path))
        return dur

    tool("rms", ["rms", "-s", "md.tpr", "-f", "md.xtc", "-o", "analysis/rmsd_bb.xvg"],
         b"Backbone\nBackbone\n")
    # 注意：2026.1 的 rmsf -res 开关实测退出 0 却不产出文件（v0.5-01 教训），
    # 故用 C-alpha 逐原子 RMSF（单链连续编号下 CA 顺序 == 残基顺序）。
    tool("rmsf", ["rmsf", "-f", "md.xtc", "-s", "md.tpr", "-o", "analysis/rmsf_atom.xvg"],
         b"C-alpha\n")
    tool("energy", ["energy", "-f", "md.edr", "-o", "analysis/energy.xvg"],
         b"Temperature\nPressure\nPotential\nTotal Energy\n\n")

    rmsd, _ = _read_xvg(os.path.join(ana, "rmsd_bb.xvg"))
    rmsf, _ = _read_xvg(os.path.join(ana, "rmsf_atom.xvg"))
    ener, ener_leg = _read_xvg(os.path.join(ana, "energy.xvg"))
    if rmsd is None or rmsd.shape[1] < 2 or ener is None:
        raise ExecutorError("xvg 数据为空，分析失败")

    col = {name: i + 1 for i, name in ener_leg.items()}
    for need in ("Temperature", "Pressure", "Potential"):
        if need not in col:
            raise ExecutorError(f"energy.xvg 缺少 {need} 列（legends={ener_leg}）")
    t_ps, rmsd_nm = rmsd[:, 0], rmsd[:, 1]

    summary = {
        "system": p["label"], "n_residues": input_info["n_residues"],
        "forcefield": p["ff"], "water_model": p["water"],
        "ions": f"{p['salt_mM'] / 1000:g} M NaCl, neutralized",
        "t_ref": p["ref_t"], "p_ref": p["ref_p"], "dt_ps": p["dt_ps"],
        "prod_ns": p["prod_ns"], "prod_time_range_ps": [float(t_ps.min()), float(t_ps.max())],
        "n_frames_xtc": len(t_ps),
        "rmsd_backbone_nm": {
            "mean": float(rmsd_nm.mean()), "max": float(rmsd_nm.max()),
            "mean_first_10ps": float(rmsd_nm[t_ps <= 10].mean()) if (t_ps <= 10).any() else None,
            "mean_last_half": float(rmsd_nm[t_ps >= t_ps.max() / 2].mean()),
        },
        "temperature_K": {"mean": float(ener[:, col['Temperature']].mean()),
                          "sd": float(ener[:, col['Temperature']].std())},
        "pressure_bar": {"mean": float(ener[:, col['Pressure']].mean()),
                         "sd": float(ener[:, col['Pressure']].std())},
        "potential_kjmol": {"mean": float(ener[:, col['Potential']].mean())},
        "notes": ("Smoke/pipeline observational statistics ONLY. RMSD plateau is NOT "
                  "convergence; must not be used for scientific conclusions."),
    }

    fig_path = _plot(ana, p, input_info, t_ps, rmsd_nm, ener, col, rmsf)
    with open(os.path.join(ana, "summary.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    return summary, fig_path


def _plot(ana, p, input_info, t_ps, rmsd_nm, ener, col, rmsf):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np
    fig, axes = plt.subplots(1, 3, figsize=(15.5, 4.3))
    axes[0].plot(t_ps, rmsd_nm, lw=0.9)
    axes[0].set_xlabel("time (ps)")
    axes[0].set_ylabel("backbone RMSD (nm)")
    axes[0].set_title(f"{p['label']} — RMSD (fit ref: prod t0)")
    ax2 = axes[1]
    ax2.plot(ener[:, 0], ener[:, col["Temperature"]], lw=0.9, color="tab:red")
    ax2.set_xlabel("time (ps)")
    ax2.set_ylabel("temperature (K)", color="tab:red")
    ax3 = ax2.twinx()
    ax3.plot(ener[:, 0], ener[:, col["Pressure"]], lw=0.6, color="tab:blue", alpha=0.7)
    ax3.set_ylabel("pressure (bar)", color="tab:blue")
    ax2.set_title("thermostat / barostat check")
    if rmsf is not None and len(rmsf):
        # rmsf x 列是全体系原子序号，按 CA 出现顺序重建残基号 1..N
        axes[2].plot(np.arange(1, len(rmsf) + 1), rmsf[:, 1], lw=0.9)
    axes[2].set_xlabel("residue number (CA order)")
    axes[2].set_ylabel("C-alpha RMSF (nm)")
    axes[2].set_title("per-residue flexibility")
    fig.suptitle(
        f"GROMACS MD — {p['label']} ({input_info['n_residues']} res, {p['ff']}/{p['water']}, "
        f"{p['salt_mM'] / 1000:g} M NaCl, {p['ref_t']:g} K, {p['ref_p']:g} bar, "
        f"dt {p['dt_ps']:g} fs, {p['prod_ns']:g} ns)", fontsize=11)
    fig.text(0.5, 0.005,
             "Pipeline run only: NOT convergence evidence, NOT a scientific conclusion "
             "(PROTEIN_WORKBENCH_PLAN §6 v0.5).", ha="center", fontsize=8, color="0.35")
    fig.tight_layout(rect=(0, 0.03, 1, 0.94))
    out = os.path.join(ana, "analysis_figure.png")
    fig.savefig(out, dpi=130)
    plt.close(fig)
    return out


# ==========================================
# CLI
# ==========================================
def build_parser():
    p = argparse.ArgumentParser(
        prog="workbench-gromacs",
        description="GROMACS MD 确定性流程（EM→NVT→NPT→生产 MD；流程成功≠收敛≠科学结论）",
    )
    p.add_argument("--resume", metavar="RUN_DIR", default="", help="恢复既有任务目录")
    p.add_argument("--protein", help="输入 PDB（单链、干燥、全标准残基）")
    p.add_argument("--chain", help="目标链 ID（仅支持单链）")
    p.add_argument("--label", help="体系标签（进入 run_id 与 manifest）")
    p.add_argument("--ns", type=float, default=1.0, help="生产段时长（ns），默认 1.0")
    p.add_argument("--nvt-ps", type=float, default=100.0)
    p.add_argument("--npt-ps", type=float, default=100.0)
    p.add_argument("--seed", type=int, default=DEFAULT_SEED, help="全阶段随机种子")
    p.add_argument("--ff", default="amber14sb")
    p.add_argument("--water", default="tip3p")
    p.add_argument("--box-d", type=float, default=1.0, help="蛋白-盒边最小距离 (nm)")
    p.add_argument("--box-shape", default="dodecahedron")
    p.add_argument("--salt-m", type=float, default=0.15, help="NaCl 摩尔浓度")
    p.add_argument("--emtol", type=float, default=1000.0)
    p.add_argument("--gpu-id", type=int, default=0)
    p.add_argument("--gmx", default="", help="显式指定 gmx 二进制（跳过 configs/gromacs_env.sh）")
    p.add_argument("--skip-analysis", action="store_true", help="跳过 RMSD/RMSF/能量分析")
    p.add_argument("--confirm-long-run", action="store_true",
                   help=f"ns > {LONG_RUN_NS} 时必须显式确认（不改变任何结论边界）")
    p.add_argument("--note", default="", help="科学目的备注（如实入 manifest，不做任何校验）")
    p.add_argument("--workdir", default="runs/gromacs")
    p.add_argument("--timeout-prep", type=int, default=600)
    p.add_argument("--timeout-eq", type=int, default=7200)
    p.add_argument("--timeout-prod", type=int, default=0, help="0 = 自动 max(7200, ns*3h)")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)

    def _sig(signum, frame):
        executors.request_stop()
    signal.signal(signal.SIGTERM, _sig)
    signal.signal(signal.SIGINT, _sig)

    try:
        if args.resume:
            return run_resume(os.path.abspath(args.resume), args)
        return run_new(args)
    except Interrupted:
        return 130
    except Exception as e:  # 未预期崩溃兜底：manifest 不停在 running
        import traceback as _tb
        run_dir = mf._last.get("run_dir")
        m = mf._last.get("manifest")
        if run_dir and m:
            try:
                mf.record_step(run_dir, m, "crash", {"status": "failed",
                              "error": repr(e)[:500], "traceback": _tb.format_exc()[-2000:]})
                mf.set_status(run_dir, m, "failed")
            except Exception:
                pass
        traceback.print_exc()
        return 4


def _params(args, input_info):
    dt = 0.002
    return {
        "label": args.label, "chain": args.chain, "ff": args.ff, "water": args.water,
        "box_shape": args.box_shape, "box_padding_nm": args.box_d,
        "salt_mM": int(args.salt_m * 1000), "emtol": args.emtol,
        "ref_t": 300.0, "ref_p": 1.0, "tau_t": 1.0, "tau_p": 2.0,
        "compressibility": "4.5e-5", "constraints": "h-bonds",
        "dt_ps": dt, "cutoff_nm": 1.2,
        "nstxout-compressed": 5000,  # 每 10 ps 一帧
        "seed": args.seed, "gpu_id": args.gpu_id,
        "nvt_nsteps": int(args.nvt_ps * 1000 / (dt * 1000)),
        "npt_nsteps": int(args.npt_ps * 1000 / (dt * 1000)),
        "md_nsteps": int(round(args.ns * 1000 / dt)),
        "prod_ns": args.ns, "nvt_ps": args.nvt_ps, "npt_ps": args.npt_ps,
        "skip_analysis": bool(args.skip_analysis),
    }


def run_new(args) -> int:
    if not (args.protein and args.chain and args.label):
        print("[workbench-md] 拒绝执行：--protein/--chain/--label 均必填（或改用 --resume）。",
              file=sys.stderr)
        return 2
    if args.ns <= 0:
        print("[workbench-md] 拒绝执行：--ns 必须 > 0。", file=sys.stderr)
        return 2
    if args.ns > LONG_RUN_NS and not args.confirm_long_run:
        print(f"[workbench-md] 拒绝执行：--ns={args.ns} 超过 {LONG_RUN_NS} ns，"
              "需显式 --confirm-long-run（提醒：时长不赋予结论效力，正式科研模拟需另行设计方案）。",
              file=sys.stderr)
        return 2

    # ── 0. 输入校验（在解析 gmx 环境之前——坏输入不值得白跑一次 env）──
    protein_abs = os.path.abspath(args.protein)  # 子进程 cwd 会切走，路径必须绝对
    try:
        summary = parse_pdb(protein_abs)
    except FileNotFoundError as e:
        print(f"[workbench-md] {e}", file=sys.stderr)
        return 2
    ok, msg, input_info = validate_md_input(summary)
    print(f"[workbench-md] {msg}")
    if not ok:
        return 2
    if args.chain.strip().upper() != input_info["chain"]:
        print(f"[workbench-md] 拒绝执行：--chain '{args.chain}' 与文件实际链 "
              f"'{input_info['chain']}' 不一致。", file=sys.stderr)
        return 2

    # ── 0.5 依赖前置 ──
    if args.gmx:
        try:
            g = resolve_explicit_gmx(args.gmx)
        except FileNotFoundError as e:
            print(f"[workbench-md] 依赖缺失: {e}", file=sys.stderr)
            return 3
    else:
        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        try:
            g = resolve_gmx(repo_root)
        except ExecutorError as e:
            print(f"[workbench-md] 依赖缺失: {e}", file=sys.stderr)
            return 3
    print(f"[workbench-md] gmx={g['gmx']} 版本={g['version'].get('GROMACS version')} "
          f"GPU={g['version'].get('GPU support')}（{g['mode']}）")

    # ── 2. 任务目录（不覆盖；绝对路径——子进程 cwd 会切换）──
    run_id = f"{time.strftime('%Y%m%d_%H%M%S')}_gmx_{args.label}"
    run_dir = os.path.abspath(os.path.join(args.workdir, run_id))
    n = 2
    while os.path.exists(run_dir):
        run_dir = os.path.abspath(os.path.join(args.workdir, f"{run_id}_{n}"))
        n += 1
    os.makedirs(os.path.join(run_dir, "logs"), exist_ok=True)
    os.makedirs(os.path.join(run_dir, "input"), exist_ok=True)
    os.makedirs(os.path.join(run_dir, "mdps"), exist_ok=True)

    p = _params(args, input_info)
    p["input_check"] = input_info
    local_pdb = os.path.join(run_dir, "input", f"{args.label}_wt_input.pdb")
    with open(protein_abs, "rb") as fin, open(local_pdb, "wb") as fout:
        fout.write(fin.read())
    input_sha = mf.sha256(local_pdb)

    m = _create_manifest(run_dir, args, p, g, input_info, local_pdb, input_sha)
    print(f"[workbench-md] 任务目录: {run_dir}")
    m = mf.set_status(run_dir, m, "validated")

    # mdp 落盘（内容入 config_hash，恢复时校验）
    mdp_dir = os.path.join(run_dir, "mdps")
    for kind in ("ions", "em", "nvt", "npt", "md"):
        with open(os.path.join(mdp_dir, f"{kind}.mdp"), "w", encoding="utf-8") as f:
            f.write(build_mdp(kind, p))
    m["config"]["mdp_sha256"] = {k: mf.sha256(os.path.join(mdp_dir, f"{k}.mdp"))
                                 for k in ("ions", "em", "nvt", "npt", "md")}
    mf.save(run_dir, m)

    m = mf.set_status(run_dir, m, "running")
    try:
        m = _pipeline(run_dir, m, g, p, local_pdb, input_info,
                      args, fresh=True)
    except Interrupted as e:
        mf.set_status(run_dir, m, "interrupted")
        print(f"[workbench-md] ⏸ 已中断（状态已落盘，可 --resume {run_dir}）: {e}", file=sys.stderr)
        return 130
    except ExecutorError as e:
        mf.set_status(run_dir, m, "failed")
        print(f"[workbench-md] ❌ 执行失败:\n{e}", file=sys.stderr)
        return 4
    return _finalize(run_dir, m)


def run_resume(run_dir: str, args) -> int:
    mpath = os.path.join(run_dir, "manifest.json")
    if not os.path.exists(mpath):
        print(f"[workbench-md] 恢复拒绝：{mpath} 不存在。", file=sys.stderr)
        return 2
    with open(mpath, "r", encoding="utf-8") as f:
        m = json.load(f)
    if m.get("status") == "succeeded":
        print(f"[workbench-md] 该任务已完成（succeeded），无需恢复：{run_dir}")
        return 0
    if m.get("status") not in ("failed", "interrupted"):
        print(f"[workbench-md] 恢复拒绝：状态为 {m.get('status')}（可能仍在运行），"
              "仅 failed/interrupted 可恢复。", file=sys.stderr)
        return 2

    # 依赖与配置一致性
    task = m["task"]
    if args.gmx:
        g = resolve_explicit_gmx(args.gmx)
    else:
        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        g = resolve_gmx(repo_root)
    cfg_hash = _config_hash(m["config"])
    if cfg_hash != m["config"].get("config_hash"):
        print("[workbench-md] ❌ 恢复拒绝：配置指纹不匹配（manifest 可能被手改）。",
              file=sys.stderr)
        return 2
    if not os.path.isfile(task["input_copied"]) or mf.sha256(task["input_copied"]) != task["input_sha256"]:
        print("[workbench-md] ❌ 恢复拒绝：任务内输入副本缺失或被改动。", file=sys.stderr)
        return 2
    print(f"[workbench-md] 恢复任务 {m['run_id']}（原状态: {m.get('status')}）")
    m.setdefault("resume_history", []).append({"time": mf.now_iso(), "from_status": m.get("status")})
    m = mf.set_status(run_dir, m, "running")
    p = {k: v for k, v in m["config"].items() if k not in ("mdp_sha256", "config_hash")}
    p["label"] = task["label"]
    p["input_check"] = task["input_check"]
    try:
        m = _pipeline(run_dir, m, g, p, task["input_copied"], task["input_check"],
                      args, fresh=False)
    except Interrupted as e:
        mf.set_status(run_dir, m, "interrupted")
        print(f"[workbench-md] ⏸ 再次中断（可继续 --resume）: {e}", file=sys.stderr)
        return 130
    except ExecutorError as e:
        mf.set_status(run_dir, m, "failed")
        print(f"[workbench-md] ❌ 执行失败:\n{e}", file=sys.stderr)
        return 4
    return _finalize(run_dir, m)


def _create_manifest(run_dir, args, p, g, input_info, local_pdb, input_sha) -> dict:
    import sys as _sys
    m = {
        "schema": "workbench-manifest/0.2",
        "workbench_version": __import__("workbench").__version__,
        "run_id": os.path.basename(run_dir),
        "status": "created",
        "status_history": [{"status": "created", "time": mf.now_iso()}],
        "task": {
            "name": args.label, "note": args.note,
            "calc_type": "GROMACS MD 流程（EM→NVT→NPT→生产 MD）",
            "protein_path": os.path.abspath(args.protein),
            "input_copied": local_pdb, "input_sha256": input_sha,
            "chain": input_info["chain"], "label": args.label,
            "input_check": input_info,
            "duration_class": ("smoke(<=5ns)" if args.ns <= LONG_RUN_NS
                               else f"long({args.ns:g}ns, --confirm-long-run)"),
        },
        "software": {
            "python": _sys.version.split()[0],
            "gmx": g["gmx"], "gmx_version": g["version"],
            "gmx_env_mode": g["mode"], "gmx_env_script": g["env_script"],
            "git_commit": mf.git_commit(os.path.dirname(os.path.dirname(run_dir)) or "."),
        },
        "config": _params(args, input_info),
        "steps": {},
        "result": None,
        "scope_and_limits": MD_SCOPE,
    }
    m["config"]["config_hash"] = _config_hash(m["config"])
    mf.save(run_dir, m)
    return m


def _config_hash(cfg: dict) -> str:
    """配置指纹：排除派生键（config_hash 自身、mdp_sha256——后者在 hash 之后写入）。"""
    import hashlib
    payload = {k: v for k, v in cfg.items() if k not in ("config_hash", "mdp_sha256")}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False)
                          .encode("utf-8")).hexdigest()


# ==========================================
# 流水线主体（新任务与恢复共用；恢复时逐阶段哈希复用）
# ==========================================
def _pipeline(run_dir, m, g, p, local_pdb, input_info, args, fresh: bool):
    gmx, env = g["gmx"], g["env"]
    R = run_dir
    PRISTINE = os.path.join(R, "backups", "topol.pristine.top")

    def stage(m, name, calls, expect, timeout, mutates_topol=False, wipe=None):
        """执行一个阶段（1..n 条命令），整体按「全部退出码 0 + 预期产物 + 哈希」判定。

        mutates_topol: 该阶段会改写 topol.top（solvate/genion）——重算前从 pristine
        备份恢复，保证幂等（重复运行不会二次追加溶剂行）。
        wipe: 重算前要清除的旧产物列表，默认 = expect。注意「经过修改而非本阶段
        创建」的文件（如 genion 的 topol.top）只能记录不能清除（v0.5-02 实测教训）。
        注意 topol.top 只记录在【最后一个改写者 genion】的 outputs 里：若每个改写
        阶段都记录它，下游突变会使上游的恢复复用校验全部失效（v0.5-02 单测实测教训）。
        """
        if not fresh and _stage_reusable(m, name, R):
            print(f"[workbench-md] ♻ 复用已校验阶段 {name}")
            return m
        if not fresh and mutates_topol and os.path.isfile(PRISTINE):
            # 上一轮可能已追加过溶剂行：先还原，避免重复计数
            import shutil
            shutil.copy2(PRISTINE, os.path.join(R, "topol.top"))
        for fname in (wipe if wipe is not None else expect):  # 清掉本阶段创建的旧产物
            fpath = os.path.join(R, fname)
            if os.path.exists(fpath):
                os.remove(fpath)
        m = mf.record_step(R, m, name, {"status": "running", "start_time": mf.now_iso()})
        infos = []
        for i, (argv, sb) in enumerate(calls):
            sub = name if len(calls) == 1 else f"{name}_step{i + 1}"
            infos.append({"command": argv, "stdin": (sb.decode(errors="replace") if sb else None),
                          **_exec(sub, argv, R, timeout, env, stdin_bytes=sb)})
        outs = _check_outputs(name, R, expect)
        m = mf.record_step(R, m, name, {
            "status": "succeeded", "steps": infos, "outputs": outs,
        })
        return m

    # 恢复场景防护：pristine 备份缺失时 topol 状态不可信，强制从 pdb2gmx 重算
    if not fresh and not os.path.isfile(PRISTINE):
        for fname in ("processed.gro", "topol.top", "posre.itp"):
            fpath = os.path.join(R, fname)
            if os.path.exists(fpath):
                os.remove(fpath)

    # —— 准备 ——
    m = stage(m, "pdb2gmx",
              [([gmx, "pdb2gmx", "-f", local_pdb, "-o", "processed.gro", "-p", "topol.top",
                 "-i", "posre.itp", "-ff", p["ff"], "-water", p["water"], "-ignh"], None)],
              ["processed.gro", "posre.itp"], args.timeout_prep)

    # pristine topol 基线（未加溶剂/离子）：solvate/genion 重算前恢复，保证幂等
    import shutil
    if not os.path.isfile(PRISTINE):
        os.makedirs(os.path.dirname(PRISTINE), exist_ok=True)
        shutil.copy2(os.path.join(R, "topol.top"), PRISTINE)

    m = stage(m, "editconf",
              [([gmx, "editconf", "-f", "processed.gro", "-o", "boxed.gro",
                 "-c", "-d", str(p["box_padding_nm"]), "-bt", p["box_shape"]], None)],
              ["boxed.gro"], args.timeout_prep)

    m = stage(m, "solvate",
              [([gmx, "solvate", "-cp", "boxed.gro", "-cs", "spc216.gro",
                 "-o", "solv.gro", "-p", "topol.top"], None)],
              ["solv.gro"], args.timeout_prep, mutates_topol=True)

    m = stage(m, "genion",
              [([gmx, "grompp", "-f", "mdps/ions.mdp", "-c", "solv.gro",
                 "-p", "topol.top", "-o", "ions.tpr"], None),
               ([gmx, "genion", "-s", "ions.tpr", "-o", "solv_ions.gro", "-p", "topol.top",
                 "-pname", "NA", "-nname", "CL", "-conc", str(p["salt_mM"] / 1000),
                 "-neutral"], b"SOL\n")],
              ["solv_ions.gro", "topol.top"], args.timeout_prep, mutates_topol=True,
              wipe=["solv_ions.gro"])

    # —— 能量最小化与平衡 ——
    m = stage(m, "em",
              [([gmx, "grompp", "-f", "mdps/em.mdp", "-c", "solv_ions.gro",
                 "-p", "topol.top", "-o", "em.tpr"], None),
               ([gmx, "mdrun", "-deffnm", "em"], None)],
              ["em.gro"], args.timeout_eq)

    m = stage(m, "nvt",
              [([gmx, "grompp", "-f", "mdps/nvt.mdp", "-c", "em.gro", "-r", "em.gro",
                 "-p", "topol.top", "-o", "nvt.tpr"], None),
               ([gmx, "mdrun", "-deffnm", "nvt", "-gpu_id", str(p["gpu_id"])], None)],
              ["nvt.gro"], args.timeout_eq)

    m = stage(m, "npt",
              [([gmx, "grompp", "-f", "mdps/npt.mdp", "-c", "nvt.gro", "-r", "nvt.gro",
                 "-p", "topol.top", "-o", "npt.tpr"], None),
               ([gmx, "mdrun", "-deffnm", "npt", "-gpu_id", str(p["gpu_id"])], None)],
              ["npt.gro"], args.timeout_eq)

    m = stage(m, "grompp_md",
              [([gmx, "grompp", "-f", "mdps/md.mdp", "-c", "npt.gro",
                 "-p", "topol.top", "-o", "md.tpr"], None)],
              ["md.tpr"], args.timeout_prep)

    # —— 生产段（GPU 采样取证）——
    prod_timeout = args.timeout_prod or max(7200, int(p["prod_ns"] * 3600 * 3))
    prod_expect = ["md.gro", "md.xtc", "md.edr", "md.log", "md.cpt"]
    if not fresh and _stage_reusable(m, "mdrun_md", R):
        print("[workbench-md] ♻ 复用已校验生产段产物")
    else:
        for fname in prod_expect:
            fpath = os.path.join(R, fname)
            if os.path.exists(fpath):
                os.remove(fpath)
        m = mf.record_step(R, m, "mdrun_md", {"status": "running", "start_time": mf.now_iso()})
        sampler = GpuSampler()
        baseline = _nvidia_baseline()
        sampler.start()
        try:
            info = _exec("mdrun_md", [gmx, "mdrun", "-deffnm", "md",
                                      "-gpu_id", str(p["gpu_id"])],
                         R, prod_timeout, env)
            outs = _check_outputs("mdrun_md", R, prod_expect)
        finally:
            nv = sampler.stop_and_save(R, p["gpu_id"], baseline)
        nv["baseline_mem_mib"] = {str(k): v for k, v in sorted(baseline.items())}
        m["gpu_evidence_nvidia_smi"] = nv
        m = mf.record_step(R, m, "mdrun_md", {"status": "succeeded", "steps": [info],
                                              "outputs": outs, "gpu_nvidia_smi": nv})
        print(f"[workbench-md] 生产段完成；nvidia 采样峰值 GPU{p['gpu_id']}="
              f"{nv.get('target_gpu_peak_mem_mib')} MiB（基线 {nv.get('target_gpu_baseline_mem_mib')}）")

    ev = extract_gpu_evidence(os.path.join(R, "md.log"))
    m["gpu_evidence_mdrun_log"] = ev
    m["gpu_used_assessment"] = assess_gpu(ev, m.get("gpu_evidence_nvidia_smi", {}))
    mf.save(R, m)
    print(f"[workbench-md] GPU 结论: {m['gpu_used_assessment']['conclusion']}"
          + (f"；性能 {ev['performance_ns_day']} ns/day" if ev.get("performance_ns_day") else ""))

    # —— 分析 ——
    if p.get("skip_analysis"):
        m = mf.record_step(R, m, "analysis", {"status": "skipped（--skip-analysis）"})
        return m
    m = mf.record_step(R, m, "analysis", {"status": "running", "start_time": mf.now_iso()})
    summary, fig_path = run_analysis(R, g, p, input_info)
    m["analysis_summary"] = summary
    m = mf.record_step(R, m, "analysis", {
        "status": "succeeded",
        "outputs": {"analysis/summary.json": mf.sha256(os.path.join(R, "analysis/summary.json")),
                    "analysis/analysis_figure.png": mf.sha256(fig_path)},
    })
    return m


def _nvidia_baseline() -> dict:
    base = {}
    try:
        r = subprocess.run(["nvidia-smi", "--query-gpu=index,memory.used",
                            "--format=csv,noheader,nounits"], capture_output=True, timeout=30)
        for line in r.stdout.decode(errors="replace").splitlines():
            parts = line.split(",")
            if len(parts) == 2:
                try:
                    base[int(parts[0])] = int(parts[1])
                except ValueError:
                    pass
    except (OSError, subprocess.TimeoutExpired):
        pass
    return base


def _finalize(run_dir, m) -> int:
    # 产物哈希入档
    m["result_outputs"] = {}
    for name in ("processed.gro", "topol.top", "solv_ions.gro", "em.gro", "nvt.gro",
                 "npt.gro", "md.tpr", "md.xtc", "md.edr", "md.log",
                 "analysis/summary.json", "analysis/analysis_figure.png"):
        fpath = os.path.join(run_dir, name)
        if os.path.isfile(fpath):
            m["result_outputs"][name] = {"sha256": mf.sha256(fpath),
                                         "bytes": os.path.getsize(fpath)}
    m["result"] = {
        "prod_ns": m["config"].get("prod_ns"),
        "prod_time_range_ps": (m.get("analysis_summary") or {}).get("prod_time_range_ps"),
        "n_frames_xtc": (m.get("analysis_summary") or {}).get("n_frames_xtc"),
        "gpu_conclusion": (m.get("gpu_used_assessment") or {}).get("conclusion"),
        "performance_ns_day": (m.get("gpu_evidence_mdrun_log") or {}).get("performance_ns_day"),
        "convergence": "not_assessed — 流程成功不代表收敛",
        "scientific_conclusion": "none — 产物不得用于筛选/评分/亲和力判断",
        "finished_time": mf.now_iso(),
    }
    mf.set_status(run_dir, m, "succeeded")
    print(f"\n[workbench-md] ✅ 完成: {run_dir}（manifest.json | logs | analysis/）\n"
          f"[workbench-md] 边界: 流程成功 ≠ 动力学收敛 ≠ 科学结论")
    return 0


if __name__ == "__main__":
    print(f"[workbench-md] {MD_SCOPE.splitlines()[0]}")
    sys.exit(main())
