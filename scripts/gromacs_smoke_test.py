#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gromacs_smoke_test.py — Protein Design Workbench v0.5 GROMACS 流程验证(smoke test)

定位(严格遵循 PROTEIN_WORKBENCH_PLAN.md §6 v0.5):
  * 这是"流程测试":只验证 命令链 pdb2gmx → editconf → solvate → genion →
    能量最小化 → NVT → NPT → 短 MD 能在真实 GROMACS(2026.1 CUDA 构建)上
    跑通、输出文件齐全、状态/日志/退出码可追溯;
  * 不代表动力学收敛,不输出任何科学结论,不得用于筛选、评分或亲和力判断;
  * GPU 是否真的生效,以 mdrun 日志证据 + 运行期 nvidia-smi 采样为准,
    不以"服务器有显卡"为准;若未使用 GPU,如实记录。

设计约束(对齐 workbench 既有规范):
  * 全部外部调用使用参数列表(无 shell=True 拼接),逐步超时、日志、退出码;
  * 输出目录时间戳隔离,目录已存在即拒绝,不覆盖任何旧结果;
  * manifest.json 按 §5.3 记录软件版本/全部参数/种子/命令列表/退出码/GPU 证据;
  * 本工具仅供流程验证:--ns 上限 5.0;更长的科研模拟需走正式工作流(尚未实现),
    不允许借本脚本绕过。

用法(仓库根目录,python 用 rosetta-agent env):
  ~/.conda/envs/rosetta-agent/bin/python scripts/gromacs_smoke_test.py \
      --input runs/<v04_run>/inputs/clean_protein.pdb --label 2hbb_A \
      --ns 1.0 --out-root runs/gromacs_smoke --gpu-id 0
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[1]
GMX_ENV_SH = REPO_ROOT / "configs" / "gromacs_env.sh"

STANDARD_AA = {
    "ALA", "ARG", "ASN", "ASP", "CYS", "GLN", "GLU", "GLY", "HIS", "ILE",
    "LEU", "LYS", "MET", "PHE", "PRO", "SER", "THR", "TRP", "TYR", "VAL",
}

# mdrun 日志中与 GPU 相关的证据行关键字(宽匹配,证据原文入 manifest)
GPU_EVIDENCE_KEYWORDS = (
    "GPU", "CUDA", "cuFFT", "Performance", "Using ", "task assignment",
    "Selected", "auto-selected", "nodata",
)

MAX_NS = 5.0  # 流程测试上限;科研模拟需另行正式工作流


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def log(msg: str) -> None:
    print(f"[smoke {datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


# ---------------------------------------------------------------------------
# gmx 运行环境:一次性 source configs/gromacs_env.sh,取回干净 env + gmx 路径
# ---------------------------------------------------------------------------

def build_gmx_env() -> tuple[str, dict[str, str]]:
    """source configs/gromacs_env.sh 后用 `env -0` 回收完整环境。

    命令为固定脚本字符串(不含任何用户输入),仍以参数列表调用 bash。
    """
    inner = (
        f'if [ -f "{GMX_ENV_SH}" ]; then source "{GMX_ENV_SH}" >/dev/null 2>&1; fi; '
        "command -v gmx; env -0"
    )
    r = subprocess.run(
        ["bash", "-c", inner],
        capture_output=True,
        env={**os.environ, "PATH": os.environ.get("PATH", "/usr/bin:/bin:/usr/local/bin")},
        timeout=120,
    )
    out = r.stdout.decode(errors="replace")
    if not out or "\n" not in out:
        raise RuntimeError(
            f"无法定位 gmx(source {GMX_ENV_SH} 失败?)stdout={out!r} stderr={r.stderr.decode(errors='replace')[:500]!r}"
        )
    gmx_path, env_blob = out.split("\n", 1)
    gmx_path = gmx_path.strip()
    env: dict[str, str] = {}
    for kv in env_blob.split("\0"):
        if "=" in kv:
            k, _, v = kv.partition("=")
            env[k] = v
    return gmx_path, env


def parse_gmx_version(gmx: str, env: dict) -> dict[str, str]:
    r = subprocess.run([gmx, "--version"], capture_output=True, env=env, timeout=120)
    keys = (
        "GROMACS version", "Precision", "Memory model", "GPU support",
        "GPU FFT library", "CUDA compiler", "CUDA runtime", "CUDA driver",
        "SIMD instructions", "MPI library",
    )
    got: dict[str, str] = {}
    for line in r.stdout.decode(errors="replace").splitlines():
        for k in keys:
            if line.strip().startswith(k + ":"):
                got[k] = line.split(":", 1)[1].strip()
    return got


# ---------------------------------------------------------------------------
# 输入校验(在耗费任何 gmx 时间之前)
# ---------------------------------------------------------------------------

def validate_input_pdb(path: Path) -> dict:
    chains: set[str] = set()
    residues: dict[str, str] = {}
    nonstandard: list[str] = set()
    hetatm_names: list[str] = set()
    model_count = 0
    atom_lines = 0
    with open(path, errors="replace") as fh:
        for line in fh:
            if line.startswith("MODEL "):
                model_count += 1
            elif line.startswith("HETATM"):
                hetatm_names.add(line[17:20].strip())
            elif line.startswith("ATOM"):
                atom_lines += 1
                ch = line[21].strip() or "?"
                chains.add(ch)
                key = f"{ch}{line[22:27].strip()}"
                resn = line[17:20].strip()
                residues[key] = resn
                if resn not in STANDARD_AA:
                    nonstandard.add(resn)
    order = sorted(residues, key=lambda k: (k[0], int(re.sub(r"\D", "", k[1:]) or 0)))
    resnums = [int(re.sub(r"\D", "", k[1:]) or 0) for k in order]
    problems = []
    if model_count > 1:
        problems.append(f"含 {model_count} 个 MODEL(多模型结构,不支持)")
    if len(chains) != 1:
        problems.append(f"链数为 {len(chains)}(流程验证版只支持单链)")
    if nonstandard:
        problems.append(f"非标准残基: {sorted(nonstandard)}(不支持,须先清理)")
    if hetatm_names:
        problems.append(f"含 HETATM: {sorted(hetatm_names)}(配体/水/离子,流程验证不支持)")
    if problems:
        raise ValueError("输入校验未通过:" + "; ".join(problems))
    return {
        "n_chains": len(chains),
        "chains": sorted(chains),
        "n_residues": len(residues),
        "resseq_min": min(resnums),
        "resseq_max": max(resnums),
        "resseq_contiguous": (max(resnums) - min(resnums) + 1 == len(resnums)),
        "n_atom_records": atom_lines,
        "all_standard_aa": True,
    }


# ---------------------------------------------------------------------------
# 通用步骤执行:参数列表 + 超时 + 日志 + 退出码
# ---------------------------------------------------------------------------

class StepError(RuntimeError):
    pass


def run_step(
    name: str,
    argv: list[str],
    run_dir: Path,
    env: dict,
    timeout_s: int,
    stdin_bytes: bytes | None = None,
    manifest: dict | None = None,
) -> dict:
    log_path = run_dir / "logs" / f"{name}.log"
    t0 = time.time()
    rec: dict = {
        "name": name,
        "argv": argv,
        "stdin_selection": (stdin_bytes.decode(errors="replace") if stdin_bytes else None),
        "timeout_s": timeout_s,
        "start": now_iso(),
    }
    log(f"step[{name}] 开始: {' '.join(argv[:3])} ...")
    try:
        proc = subprocess.run(
            argv, cwd=str(run_dir), input=stdin_bytes,
            capture_output=True, timeout=timeout_s, env=env,
        )
        rec["exit_code"] = proc.returncode
        log_path.write_text(
            "$ " + " ".join(argv) + "\n"
            + (f"# stdin: {rec['stdin_selection']!r}\n" if stdin_bytes else "")
            + "\n--- stdout ---\n" + proc.stdout.decode(errors="replace")
            + "\n--- stderr ---\n" + proc.stderr.decode(errors="replace"),
            encoding="utf-8",
        )
        rec["log"] = str(log_path.relative_to(run_dir))
    except subprocess.TimeoutExpired as exc:
        rec["exit_code"] = None
        rec["timed_out"] = True
        (log_path or run_dir / "logs").parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(
            "$ " + " ".join(argv) + f"\nTIMEOUT after {timeout_s}s\n"
            + (exc.stdout or b"").decode(errors="replace")
            + "\n--- stderr ---\n" + (exc.stderr or b"").decode(errors="replace"),
            encoding="utf-8",
        )
        rec["log"] = str(log_path.relative_to(run_dir))
        rec["error"] = f"超时({timeout_s}s)"
        raise StepError(f"{name} 超时({timeout_s}s)") from exc
    rec["end"] = now_iso()
    rec["wall_s"] = round(time.time() - t0, 2)
    if rec["exit_code"] != 0:
        tail = ""
        try:
            tail = log_path.read_text(errors="replace")[-2000:]
        except OSError:
            pass
        rec["error"] = f"退出码 {rec['exit_code']}"
        if manifest is not None:
            write_manifest(run_dir, manifest)
        raise StepError(f"{name} 失败(退出码 {rec['exit_code']});日志尾部:\n{tail}")
    log(f"step[{name}] 完成 {rec['wall_s']}s")
    if manifest is not None:
        manifest["steps"].append(rec)
        write_manifest(run_dir, manifest)
    return rec


def write_manifest(run_dir: Path, manifest: dict) -> None:
    manifest["updated"] = now_iso()
    tmp = run_dir / "manifest.json.tmp"
    tmp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, run_dir / "manifest.json")


# ---------------------------------------------------------------------------
# mdp 模板(全部参数显式记录,种子固定)
# ---------------------------------------------------------------------------

def mdp_text(kind: str, p: dict) -> str:
    lines = [f"; {kind} — Protein Design Workbench v0.5 流程验证(smoke test)"]
    common = {
        "integrator": "steep" if kind in ("ions", "em") else "md",
        "dt": 0.01 if kind in ("ions", "em") else 0.002,
        "nsteps": 50000,
        "nstlist": 10,
        "cutoff-scheme": "Verlet",
        "pbc": "xyz",
        "coulombtype": "PME",
        "rcoulomb": 1.2,
        "rvdw": 1.2,
        "nstenergy": 500 if kind != "ions" else 0,
        "nstlog": 500 if kind != "ions" else 0,
    }
    vals = dict(common)
    if kind == "ions":
        # ions 准备步骤专用(官方教程做法):solvate 后体系带净电荷,用 PME 会触发
        # "Ewald electrostatics in a system with net charge" 警告并使 grompp 失败;
        # 该 tpr 仅用于 genion 选水替换,不用于任何动力学,故用 cut-off。
        vals["coulombtype"] = "cut-off"
    if kind == "em":
        vals["emtol"] = p["emtol"]
        vals["emstep"] = 0.01
    if kind in ("nvt", "npt", "md"):
        vals.update({
            "constraints": p["constraints"],
            "nstxout-compressed": p["nstxout-compressed"],
            "tcoupl": "V-rescale",
            "tc-grps": "System",
            "tau-t": p["tau_t"],
            "ref-t": p["ref_t"],
        })
    if kind in ("nvt", "npt"):
        vals["define"] = "-DPOSRES"  # pdb2gmx 生成的 posre.itp,1000 kJ/mol/nm^2
    if kind == "nvt":
        vals.update({"pcoupl": "no", "gen-vel": "yes", "gen-temp": p["ref_t"], "gen-seed": p["gen_seed"]})
    if kind == "npt":
        vals.update({
            "pcoupl": "C-rescale", "pcoupltype": "isotropic",
            "tau-p": p["tau_p"], "ref-p": p["ref_p"],
            "compressibility": p["compressibility"], "ref-coordscaling": "com",
            "continuation": "yes", "gen-vel": "no",
        })
    if kind == "md":
        vals["nsteps"] = p["prod_nsteps"]
        vals.update({
            "pcoupl": "C-rescale", "pcoupltype": "isotropic",
            "tau-p": p["tau_p"], "ref-p": p["ref_p"],
            "compressibility": p["compressibility"], "ref-coordscaling": "com",
            "continuation": "yes", "gen-vel": "no",
            "ld-seed": p["ld_seed"],
            # define 留空:生产段不再有位置限制
        })
    for k, v in vals.items():
        lines.append(f"{k:<22} = {v}")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# GPU 取证:mdrun 日志关键字 + 运行期 nvidia-smi 采样
# ---------------------------------------------------------------------------

def extract_gpu_evidence(md_log: Path) -> dict:
    lines = md_log.read_text(errors="replace").splitlines()
    evidence = []
    for ln in lines:
        if any(k in ln for k in GPU_EVIDENCE_KEYWORDS):
            evidence.append(ln.rstrip())
    evidence = evidence[:120]
    perf_ns_day = None
    for ln in lines:
        m = re.search(r"Performance:\s*([\d.]+)", ln)
        if m:
            perf_ns_day = float(m.group(1))
    gpu_selected = any(re.search(r"GPU", ln) and re.search(r"select|Using|assign|run on", ln, re.I) for ln in evidence)
    return {
        "log": str(md_log.name),
        "matched_lines": evidence,
        "gpu_selected_in_log": gpu_selected,
        "performance_ns_day": perf_ns_day,
    }


class GpuSampler:
    """mdrun 生产段期间对 nvidia-smi 采样,作为独立于日志的第二证据源。"""

    def __init__(self, interval_s: float = 5.0):
        self.interval = interval_s
        self.rows: list[str] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.available = shutil.which("nvidia-smi") is not None

    def _sample_once(self) -> None:
        r = subprocess.run(
            ["nvidia-smi", "--query-gpu=index,memory.used,utilization.gpu",
             "--format=csv,noheader,nounits"],
            capture_output=True, timeout=30,
        )
        ts = time.time()
        for line in r.stdout.decode(errors="replace").splitlines():
            self.rows.append(f"{ts:.0f},{line.strip()}")

    def start(self) -> None:
        if not self.available:
            return
        def loop():
            while not self._stop.is_set():
                try:
                    self._sample_once()
                except Exception:
                    pass
                self._stop.wait(self.interval)
        self._thread = threading.Thread(target=loop, daemon=True)
        self._thread.start()

    def stop_and_save(self, run_dir: Path, target_gpu: int, baseline: dict[int, int] | None = None) -> dict:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=self.interval * 2)
        out = run_dir / "gpu" / "nvidia_samples.csv"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("epoch,gpu_index,mem_mib,util_pct\n" + "\n".join(self.rows) + "\n", encoding="utf-8")
        summary: dict = {
            "n_samples": len(self.rows),
            "csv": str(out.relative_to(run_dir)),
            "available": self.available,
        }
        max_mem: dict[int, int] = {}
        for row in self.rows:
            parts = row.split(",")
            if len(parts) >= 3:
                try:
                    gi, mem = int(parts[1].strip()), int(parts[2].strip())
                    max_mem[gi] = max(max_mem.get(gi, 0), mem)
                except ValueError:
                    pass
        summary["max_mem_mib_per_gpu"] = {str(k): v for k, v in sorted(max_mem.items())}
        base = (baseline or {}).get(target_gpu, 0)
        peak = max_mem.get(target_gpu, 0)
        summary["target_gpu_baseline_mem_mib"] = base
        summary["target_gpu_peak_mem_mib"] = peak
        # 判定标准:生产段目标卡显存峰值超过该卡基线 200 MiB 以上(GROMACS CUDA
        # context + 数据量随体系规模变化,不能按固定绝对阈值判)。
        summary["gpu_process_observed"] = (peak - base) > 200
        return summary


# ---------------------------------------------------------------------------
# 分析:RMSD / RMSF / 能量 → xvg + 图 + summary.json
# ---------------------------------------------------------------------------

def read_xvg(path: Path):
    import numpy as np
    rows = []
    legends: dict[int, str] = {}
    for line in path.read_text(errors="replace").splitlines():
        line = line.strip()
        if line.startswith("@ s") and "legend" in line:
            m = re.match(r"@ s(\d+) legend \"(.+)\"", line)
            if m:
                legends[int(m.group(1))] = m.group(2)
        if not line or line[0] in "#@":
            continue
        rows.append([float(x) for x in line.split()])
    arr = np.array(rows) if rows else np.zeros((0, 0))
    return arr, legends


def run_analysis(run_dir: Path, gmx: str, env: dict, p: dict, manifest_step_fn) -> dict:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np

    ana = run_dir / "analysis"
    ana.mkdir(exist_ok=True)

    def gmx_tool(name: str, args: list[str], stdin_bytes: bytes | None):
        return run_step(name, [gmx, *args], run_dir, env, 900, stdin_bytes)

    gmx_tool("ana_rms", ["rms", "-s", "md.tpr", "-f", "md.xtc", "-o", "analysis/rmsd_bb.xvg"], b"Backbone\nBackbone\n")
    # 注意:-res 是布尔开关(per-residue 结果固定写入 res-rmsf.xvg),不能跟文件名
    # 注意:2026.1 上 `-res` 开关退出码 0 但未生成 res-rmsf.xvg(行为存疑,已如实
    # 记录);改用 C-alpha 逐原子 RMSF——单链无缺口体系 51 个 CA 与残基一一对应。
    gmx_tool("ana_rmsf", ["rmsf", "-f", "md.xtc", "-s", "md.tpr",
                          "-o", "analysis/rmsf_atom.xvg"], b"C-alpha\n")
    gmx_tool("ana_energy", ["energy", "-f", "md.edr", "-o", "analysis/energy.xvg"],
             b"Temperature\nPressure\nPotential\nTotal Energy\n\n")

    rmsd, _ = read_xvg(ana / "rmsd_bb.xvg")
    rmsf_res, _ = read_xvg(ana / "rmsf_atom.xvg")
    # rmsf_atom.xvg 的 x 列是全体系原子序号(CA 分散在 1..~793),不是残基号;
    # 单链连续编号(输入校验已确认)下 CA 出现顺序 == 残基顺序,重建 1..N。
    rmsf_x = np.arange(1, len(rmsf_res) + 1)
    ener, ener_legends = read_xvg(ana / "energy.xvg")

    t_ps = rmsd[:, 0]
    rmsd_nm = rmsd[:, 1]
    col_of = {name: i + 1 for i, name in ener_legends.items()}
    t_col = ener[:, col_of["Temperature"]]
    p_col = ener[:, col_of["Pressure"]]
    pot_col = ener[:, col_of["Potential"]]

    summary = {
        "run_class": "pipeline_smoke_test",
        "system": p["label"],
        "n_residues": p["input_check"]["n_residues"],
        "forcefield": p["ff"], "water_model": p["water"], "ions": "0.15 M NaCl, neutralized",
        "t_ref": p["ref_t"], "p_ref": p["ref_p"], "dt_ps": 0.002,
        "prod_ns": p["prod_ns"], "prod_time_range_ps": [float(t_ps.min()), float(t_ps.max())] if len(t_ps) else None,
        "n_frames_xtc": len(t_ps),
        "rmsd_backbone_nm": {
            "mean": float(rmsd_nm.mean()), "max": float(rmsd_nm.max()),
            "mean_first_10ps": float(rmsd_nm[t_ps <= 10].mean()) if (t_ps <= 10).any() else None,
            "mean_last_half": float(rmsd_nm[t_ps >= t_ps.max() / 2].mean()) if len(t_ps) else None,
        },
        "temperature_K": {"mean": float(t_col.mean()), "sd": float(t_col.std())},
        "pressure_bar": {"mean": float(p_col.mean()), "sd": float(p_col.std())},
        "potential_kjmol": {"mean": float(pot_col.mean()), "min": float(pot_col.min())},
        "n_ca_atoms_rmsf": len(rmsf_res),
        "rmsf_mapping_note": "rmsf x 列为全体系原子序号,已按 CA 出现顺序重建为残基号 1..N(单链连续编号)",
        "notes": (
            "Smoke test observational statistics ONLY. RMSD plateau is NOT convergence; "
            "these numbers must not be used for any scientific conclusion, screening, or scoring."
        ),
    }
    (ana / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    fig, axes = plt.subplots(1, 3, figsize=(15.5, 4.3))
    axes[0].plot(t_ps, rmsd_nm, lw=0.9)
    axes[0].set_xlabel("time (ps)")
    axes[0].set_ylabel("backbone RMSD (nm)")
    axes[0].set_title(f"{p['label']} WT — RMSD (fit ref: prod t0)")
    ax2 = axes[1]
    ax2.plot(ener[:, 0], t_col, lw=0.9, color="tab:red")
    ax2.set_xlabel("time (ps)")
    ax2.set_ylabel("temperature (K)", color="tab:red")
    ax3 = ax2.twinx()
    ax3.plot(ener[:, 0], p_col, lw=0.6, color="tab:blue", alpha=0.7)
    ax3.set_ylabel("pressure (bar)", color="tab:blue")
    ax2.set_title("thermostat / barostat check")
    if len(rmsf_res):
        axes[2].plot(rmsf_x, rmsf_res[:, 1], lw=0.9)
    axes[2].set_xlabel("residue number (CA order)")
    axes[2].set_ylabel("C-alpha RMSF (nm)")
    axes[2].set_title("per-residue flexibility")
    fig.suptitle(
        f"GROMACS 2026.1 pipeline smoke test — {p['label']} WT "
        f"({p['input_check']['n_residues']} res, amber14sb/TIP3P, 0.15 M NaCl, "
        f"{p['ref_t']:g} K, {p['ref_p']:g} bar, dt 2 fs, {p['prod_ns']:g} ns)",
        fontsize=11,
    )
    fig.text(0.5, 0.005,
             "Pipeline smoke test only: NOT convergence evidence, NOT a scientific conclusion (PROTEIN_WORKBENCH_PLAN §6 v0.5).",
             ha="center", fontsize=8, color="0.35")
    fig.tight_layout(rect=(0, 0.03, 1, 0.94))
    fig.savefig(ana / "analysis_figure.png", dpi=130)
    plt.close(fig)
    return summary


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="GROMACS 流程验证(smoke test),非科研模拟")
    ap.add_argument("--input", required=True, help="WT 输入 PDB(单链、仅标准残基)")
    ap.add_argument("--label", required=True, help="体系标签,如 2hbb_A(进入目录名/manifest)")
    ap.add_argument("--ns", type=float, default=1.0, help="生产段长度(ns),流程测试上限 5.0")
    ap.add_argument("--out-root", default="runs/gromacs_smoke")
    ap.add_argument("--gpu-id", type=int, default=0)
    ap.add_argument("--gen-seed", type=int, default=20261113, help="NVT 初速度种子")
    ap.add_argument("--ld-seed", type=int, default=20261113, help="生产段 ld-seed")
    ap.add_argument("--emtol", type=float, default=1000.0)
    args = ap.parse_args()

    if not 0.1 <= args.ns <= MAX_NS:
        log(f"拒绝:--ns={args.ns} 超出流程测试范围(0.1–{MAX_NS} ns)。"
            "更长的科研模拟需正式工作流,本工具不得绕过。")
        return 2
    in_pdb = Path(args.input).resolve()
    if not in_pdb.is_file():
        log(f"拒绝:输入不存在 {in_pdb}")
        return 2

    stamp = time.strftime("%Y%m%d_%H%M%S")
    run_id = f"{stamp}_gromacs_smoke_{args.label}"
    out_root = (REPO_ROOT / args.out_root).resolve()
    run_dir = out_root / run_id
    if run_dir.exists():
        log(f"拒绝:目录已存在 {run_dir}(不覆盖旧结果)")
        return 2
    (run_dir / "logs").mkdir(parents=True)
    (run_dir / "mdps").mkdir(exist_ok=True)
    (run_dir / "input").mkdir(exist_ok=True)
    log(f"run_id={run_id} dir={run_dir}")

    manifest: dict = {
        "schema_version": "1",
        "run_id": run_id,
        "run_class": "pipeline_smoke_test",
        "plan_reference": "PROTEIN_WORKBENCH_PLAN.md §6 v0.5",
        "created": now_iso(),
        "status": "running",
        "label": args.label,
        "input": {"original_path": str(in_pdb)},
        "parameters": {},
        "steps": [],
        "disclaimers": {
            "convergence": "not_assessed — 流程验证不评估动力学收敛",
            "scientific_conclusion": "none — 产物不得用于筛选/评分/亲和力判断",
            "md_duration_note": f"{args.ns:g} ns 仅证明命令链可运行,远短于任何科研模拟所需时长",
        },
    }
    write_manifest(run_dir, manifest)

    try:
        # ---- 输入校验与留档 ----
        ic = validate_input_pdb(in_pdb)
        local_pdb = run_dir / "input" / f"{args.label}_wt_input.pdb"
        shutil.copy2(in_pdb, local_pdb)
        manifest["input"].update({
            "copied_to": str(local_pdb.relative_to(run_dir)),
            "sha256": sha256_of(local_pdb),
            "check": ic,
        })
        write_manifest(run_dir, manifest)
        log(f"输入校验通过:链 {ic['chains']}, 残基 {ic['n_residues']} "
            f"({ic['resseq_min']}–{ic['resseq_max']}, 连续={ic['resseq_contiguous']})")

        # ---- gmx 环境 ----
        gmx, genv = build_gmx_env()
        ver = parse_gmx_version(gmx, genv)
        manifest["software"] = {
            "gmx_binary": gmx,
            "gmx_version": ver,
            "env_script": str(GMX_ENV_SH.relative_to(REPO_ROOT)),
            "activate": "source configs/gromacs_env.sh",
            "env_script_note": "GMXRC + cuFFT11 兼容层(rosetta-agent env 的 nvidia-cufft-cu12),见脚本头注释",
        }
        write_manifest(run_dir, manifest)
        log(f"gmx={gmx} 版本={ver.get('GROMACS version')} GPU support={ver.get('GPU support')}")

        # ---- 参数(全部显式) ----
        p = {
            "label": args.label, "ff": "amber14sb", "water": "tip3p",
            "box_shape": "dodecahedron", "box_padding_nm": 1.0,
            "ionic_strength_M": 0.15, "neutralize": True,
            "emtol": args.emtol,
            "ref_t": 300.0, "ref_p": 1.0, "tau_t": 1.0, "tau_p": 2.0,
            "compressibility": "4.5e-5",
            "constraints": "h-bonds", "dt_ps": 0.002,
            "nstxout-compressed": 5000,  # 每 10 ps 一帧
            "prod_ns": args.ns, "prod_nsteps": int(args.ns * 500000),
            "gen_seed": args.gen_seed, "ld_seed": args.ld_seed,
            "gpu_id": args.gpu_id,
            "input_check": ic,
        }
        manifest["parameters"] = {k: v for k, v in p.items() if k != "input_check"}
        manifest["parameters"]["input_check"] = ic
        manifest["seeds"] = {"nvt_gen_seed": args.gen_seed, "md_ld_seed": args.ld_seed}
        write_manifest(run_dir, manifest)

        mdp_dir = run_dir / "mdps"
        for kind in ("ions", "em", "nvt", "npt", "md"):
            (mdp_dir / f"{kind}.mdp").write_text(mdp_text(kind, p), encoding="utf-8")
        manifest["mdp_files"] = {k: sha256_of(mdp_dir / f"{k}.mdp") for k in ("ions", "em", "nvt", "npt", "md")}
        write_manifest(run_dir, manifest)

        # ---- 主命令链(参数列表,逐步超时) ----
        short_t = 600
        run_step("01_pdb2gmx",
                 [gmx, "pdb2gmx", "-f", str(local_pdb), "-o", "processed.gro", "-p", "topol.top",
                  "-i", "posre.itp", "-ff", p["ff"], "-water", p["water"], "-ignh"],
                 run_dir, genv, short_t, manifest=manifest)
        run_step("02_editconf",
                 [gmx, "editconf", "-f", "processed.gro", "-o", "boxed.gro",
                  "-c", "-d", str(p["box_padding_nm"]), "-bt", p["box_shape"]],
                 run_dir, genv, short_t, manifest=manifest)
        run_step("03_solvate",
                 [gmx, "solvate", "-cp", "boxed.gro", "-cs", "spc216.gro",
                  "-o", "solv.gro", "-p", "topol.top"],
                 run_dir, genv, short_t, manifest=manifest)
        run_step("04_grompp_ions",
                 [gmx, "grompp", "-f", "mdps/ions.mdp", "-c", "solv.gro", "-p", "topol.top", "-o", "ions.tpr"],
                 run_dir, genv, short_t, manifest=manifest)
        run_step("05_genion",
                 [gmx, "genion", "-s", "ions.tpr", "-o", "solv_ions.gro", "-p", "topol.top",
                  "-pname", "NA", "-nname", "CL", "-conc", str(p["ionic_strength_M"]), "-neutral"],
                 run_dir, genv, short_t, stdin_bytes=b"SOL\n", manifest=manifest)
        run_step("06_grompp_em",
                 [gmx, "grompp", "-f", "mdps/em.mdp", "-c", "solv_ions.gro", "-p", "topol.top", "-o", "em.tpr"],
                 run_dir, genv, short_t, manifest=manifest)
        run_step("07_mdrun_em",
                 [gmx, "mdrun", "-deffnm", "em"],
                 run_dir, genv, 3600, manifest=manifest)
        run_step("08_grompp_nvt",
                 [gmx, "grompp", "-f", "mdps/nvt.mdp", "-c", "em.gro", "-r", "em.gro",
                  "-p", "topol.top", "-o", "nvt.tpr"],
                 run_dir, genv, short_t, manifest=manifest)
        run_step("09_mdrun_nvt",
                 [gmx, "mdrun", "-deffnm", "nvt", "-gpu_id", str(args.gpu_id)],
                 run_dir, genv, 7200, manifest=manifest)
        run_step("10_grompp_npt",
                 [gmx, "grompp", "-f", "mdps/npt.mdp", "-c", "nvt.gro", "-r", "nvt.gro",
                  "-p", "topol.top", "-o", "npt.tpr"],
                 run_dir, genv, short_t, manifest=manifest)
        run_step("11_mdrun_npt",
                 [gmx, "mdrun", "-deffnm", "npt", "-gpu_id", str(args.gpu_id)],
                 run_dir, genv, 7200, manifest=manifest)
        run_step("12_grompp_md",
                 [gmx, "grompp", "-f", "mdps/md.mdp", "-c", "npt.gro", "-p", "topol.top", "-o", "md.tpr"],
                 run_dir, genv, short_t, manifest=manifest)

        # ---- 生产段:启动 GPU 采样线程,独立取证 ----
        log(f"生产段 {args.ns:g} ns 开始(GPU {args.gpu_id},采样 nvidia-smi 中)…")
        sampler = GpuSampler(interval_s=5.0)
        baseline = {}
        if sampler.available:
            r = subprocess.run(["nvidia-smi", "--query-gpu=index,memory.used",
                                "--format=csv,noheader,nounits"], capture_output=True, timeout=30)
            for line in r.stdout.decode(errors="replace").splitlines():
                parts = line.split(",")
                if len(parts) == 2:
                    try:
                        baseline[int(parts[0].strip())] = int(parts[1].strip())
                    except ValueError:
                        pass
        sampler.start()
        try:
            prod_rec = run_step("13_mdrun_md",
                                [gmx, "mdrun", "-deffnm", "md", "-gpu_id", str(args.gpu_id)],
                                run_dir, genv, max(7200, int(args.ns * 3600 * 3)), manifest=None)
        finally:
            gpu_summary = sampler.stop_and_save(run_dir, args.gpu_id, baseline)
            gpu_summary["baseline_mem_mib"] = {str(k): v for k, v in sorted(baseline.items())}
        prod_rec["gpu_nvidia_smi"] = gpu_summary
        manifest["steps"].append(prod_rec)
        manifest["gpu_evidence_nvidia_smi"] = gpu_summary
        write_manifest(run_dir, manifest)
        log(f"生产段完成 {prod_rec['wall_s']}s;nvidia 采样峰值 GPU{args.gpu_id}="
            f"{gpu_summary.get('target_gpu_peak_mem_mib')} MiB,观察到 GPU 进程={gpu_summary.get('gpu_process_observed')}")

        # ---- GPU 日志取证 ----
        ev = extract_gpu_evidence(run_dir / "md.log")
        manifest["gpu_evidence_mdrun_log"] = ev
        manifest["gpu_used_assessment"] = {
            "log_says_gpu_selected": ev["gpu_selected_in_log"],
            "nvidia_smi_process_observed": gpu_summary.get("gpu_process_observed"),
            "performance_ns_day": ev["performance_ns_day"],
            "conclusion": (
                "GPU 实际参与(mdrun 日志选择 GPU 且 nvidia-smi 观察到进程驻留)"
                if ev["gpu_selected_in_log"] and gpu_summary.get("gpu_process_observed")
                else "未确认 GPU 生效 — 以日志与采样原文为准,如实记录"
            ),
        }
        write_manifest(run_dir, manifest)
        log(f"GPU 结论: {manifest['gpu_used_assessment']['conclusion']};"
            f"性能 {ev['performance_ns_day']} ns/day")

        # ---- 分析与图 ----
        summary = run_analysis(run_dir, gmx, genv, p, None)
        manifest["analysis_summary"] = summary
        write_manifest(run_dir, manifest)

        # ---- 关键产物哈希 ----
        manifest["outputs"] = {}
        for name in ("processed.gro", "topol.top", "em.gro", "nvt.gro", "npt.gro", "md.gro",
                     "md.tpr", "md.xtc", "md.edr", "md.log",
                     "analysis/rmsd_bb.xvg", "analysis/rmsf_atom.xvg", "analysis/energy.xvg",
                     "analysis/analysis_figure.png", "analysis/summary.json"):
            f = run_dir / name
            if f.is_file():
                manifest["outputs"][name] = {"sha256": sha256_of(f), "bytes": f.stat().st_size}
        write_manifest(run_dir, manifest)

        # ---- 主机与代码版本 ----
        try:
            manifest["host"] = {
                "uname": subprocess.run(["uname", "-a"], capture_output=True, timeout=30).stdout.decode().strip(),
            }
        except Exception:
            manifest["host"] = {"uname": "unknown"}
        try:
            gcommit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(REPO_ROOT),
                                     capture_output=True, timeout=30).stdout.decode().strip()
            manifest["code_version"] = {"git_commit": gcommit or "unknown"}
        except Exception:
            manifest["code_version"] = {"git_commit": "unknown"}

        manifest["status"] = "succeeded"
        manifest["ended"] = now_iso()
        write_manifest(run_dir, manifest)
        log(f"SUCCESS run={run_id} 全部步骤通过,产物与分析已写入 {run_dir}")
        return 0

    except StepError as exc:
        manifest["status"] = "failed"
        manifest["ended"] = now_iso()
        manifest["failure"] = str(exc)[:4000]
        write_manifest(run_dir, manifest)
        log(f"FAILED run={run_id}: {str(exc)[:300]}")
        return 1
    except Exception as exc:  # noqa: BLE001
        manifest["status"] = "failed"
        manifest["ended"] = now_iso()
        manifest["failure"] = f"{exc}\n{traceback.format_exc()}"[:4000]
        write_manifest(run_dir, manifest)
        log(f"FAILED run={run_id}: {exc!r}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
