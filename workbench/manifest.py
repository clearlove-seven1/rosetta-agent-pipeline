"""任务 manifest：可追溯性记录（v0.2）

对应规划 §5.3：run_id、时间线、git commit、输入哈希、软件版本、命令参数列表、
退出码、状态机、单位与适用边界。无法自动获取的字段写 unknown + 原因，不编造。
"""

import datetime
import hashlib
import json
import os
import subprocess

from . import SCOPE, __version__


def sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def git_commit(repo_dir: str) -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo_dir,
            capture_output=True, text=True, timeout=10,
        )
        if out.returncode == 0:
            return out.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        pass
    return "unknown"


def now_iso() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def new_run_id(pdb_stem: str, chain: str, resnum: int, mut_aa1: str) -> str:
    """如 20261112_214530_1ubq_A76E（秒级时间戳，防同分钟重跑互相覆盖）；仅含 [A-Za-z0-9_.-]"""
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    stem = "".join(c if c.isalnum() or c in "._-" else "_" for c in pdb_stem)
    return f"{ts}_{stem}_{chain}{resnum}{mut_aa1.upper()}"


def create(run_dir: str, task: dict) -> dict:
    """创建初始 manifest（status=created）并落盘。"""
    manifest = {
        "schema": "workbench-manifest/0.2",
        "workbench_version": __version__,
        "run_id": task["run_id"],
        "status": "created",
        "status_history": [{"status": "created", "time": now_iso()}],
        "task": {
            "name": task.get("name", ""),
            "note": task.get("note", ""),
            "calc_type": "单体单点突变稳定性计算 (cartesian_ddg 常规模式)",
            "protein_path": task["protein_path"],
            "protein_sha256": sha256(task["protein_path"]),
            "chain": task["chain"],
            "resnum_pdb": task["resnum"],
            "insertion_code": task.get("icode", ""),
            "wt_aa": task["wt_aa"],
            "mut_aa": task["mut_aa"],
            "seq_index": task.get("seq_index"),
        },
        "software": {
            "python": "unknown",
            "rosetta": "unknown",  # 执行后由 executors 回填
            "rosetta_bin_dir": os.getenv("ROSETTA_BIN_DIR", "/opt/software/rosetta/main/source/bin"),
            "git_commit": git_commit(os.path.dirname(run_dir) or "."),
        },
        "config": {
            "seed": task["seed"],
            "nstruct": task.get("nstruct", 1),
            "ddg_iterations": task.get("iterations", 3),
            "score_function": "ref2015_cart",
            "timeout_relax_s": task.get("timeout_relax", 1800),
            "timeout_ddg_s": task.get("timeout_ddg", 3600),
        },
        "steps": {},
        "result": None,
        "scope_and_limits": SCOPE,
    }
    import sys
    manifest["software"]["python"] = sys.version.split()[0]
    save(run_dir, manifest)
    return manifest


def set_status(run_dir: str, manifest: dict, status: str) -> dict:
    manifest["status"] = status
    manifest["status_history"].append({"status": status, "time": now_iso()})
    save(run_dir, manifest)
    return manifest


def record_step(run_dir: str, manifest: dict, step: str, info: dict) -> dict:
    info = dict(info)
    info["start_time"] = info.get("start_time", now_iso())
    manifest["steps"][step] = info
    save(run_dir, manifest)
    return manifest


def set_result(run_dir: str, manifest: dict, result: dict) -> dict:
    manifest["result"] = result
    save(run_dir, manifest)
    return manifest


_last = {"run_dir": None, "manifest": None}


def save(run_dir: str, manifest: dict) -> str:
    path = os.path.join(run_dir, "manifest.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    # 记录最近一次落盘上下文：供 CLI 兜底在未预期崩溃时如实写回状态
    _last["run_dir"] = run_dir
    _last["manifest"] = manifest
    return path
