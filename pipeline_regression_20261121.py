"""
pipeline_regression_20261121.py — 小规模真实流水线回归 + 可控失败模拟（独立验收）
================================================================================

在不影响任何现有任务/服务的独立任务目录里跑一遍真实 Rosetta 流水线
（直接调用 rosetta_tools 工具层，不经 LLM，确定性可复核）：

  阶段一（真实回归）：
    task_runtime.create_task_dir("1MC9_verify") → 上传 1MC9 → 公共层预检
    → run_cartesian_relax(nstruct=2) → run_saturation_mutagenesis("67", jobs=16)
    → 轮询至终态 → 核对 ddg_results.txt(19 行突变)、mut 日志退出码、
      task_status.json 终态、编号审计行
  阶段二（可控失败模拟）：
    独立任务目录，手工放置 relax 产物，ROSETTA_BIN_DIR=/nonexistent 启动突变
    → 19 个计算全部"启动失败(rc=-999)" → 0 个成功突变
    → 断言终态=failed(NoSuccessfulMutants)，绝不虚报 completed

不改科学参数（relax/cartesian_ddg 旗标由工具内部生成，与本轮修改一致）；
不清理任何历史产物。运行（rosetta-agent env，仓库根目录）：
  python pipeline_regression_20261121.py            # 阶段一
  python pipeline_regression_20261121.py --fail-sim # 阶段二
"""
import glob
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

PY = sys.executable


def load_state(task_dir):
    p = Path(task_dir) / "task_status.json"
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except ValueError:
        return {}


def phase_main():
    import task_runtime as tr
    import rosetta_tools as rt

    # 0) 资源检查
    load1 = os.getloadavg()[0]
    if load1 > 150:
        print(f"[中止] 服务器负载过高 load1={load1}")
        return 2
    print(f"[资源] load1={load1} cores={os.cpu_count()}")

    # 1) 独立任务目录 + 输入
    task_id, task_dir = tr.create_task_dir("1MC9_verify")
    task_dir = Path(task_dir)
    shutil.copy(ROOT / "inputs" / "pdbs" / "1MC9_protein.pdb", task_dir / "protein.pdb")
    tr.state_write(str(task_dir), status="created", stage="created",
                   input_summary={"protein": "1MC9_protein.pdb", "mutation": "67",
                                  "entry": "verify_regression"})
    print(f"[任务] {task_id}")

    # 2) 公共层预检（与生产同一实现）
    ok, note = tr.precheck_targets(str(task_dir / "protein.pdb"), "67")
    print(f"[预检] ok={ok} note={note}")
    assert ok, note

    # 3) 弛豫（后台 runner）+ 突变（runner 自动等待弛豫完成）
    rt.current_workspace.set(str(task_dir))
    r = rt.run_cartesian_relax.func(protein_pdb="protein.pdb", nstruct=2)
    print(f"[弛豫] {r.strip()[:80]}")
    r = rt.run_saturation_mutagenesis.func(target_resnum="67", jobs=16, delay=1.0)
    print(f"[突变] {r.strip()[:80]}")
    assert "【流水线通关】" in r, r

    # 4) 轮询至终态（上限 30 分钟）
    mut_dir = task_dir / "saturation_mutagenesis"
    deadline = time.time() + 30 * 60
    last = ""
    while time.time() < deadline:
        st = load_state(str(task_dir))
        n = len(glob.glob(str(mut_dir / "*.ddg")))
        line = f"status={st.get('status')} stage={st.get('stage')} ddg={n}"
        if line != last:
            print(f"[轮询] {line}")
            last = line
        if st.get("status") in ("completed", "failed", "interrupted"):
            break
        time.sleep(30)

    # 5) 核对
    summary = task_dir / "summary" / "ddg_results.txt"
    n_ddg = len(glob.glob(str(mut_dir / "*.ddg")))
    rows = [l for l in summary.read_text(encoding="utf-8").splitlines()[1:] if l.strip()] \
        if summary.exists() else []
    exit_codes = set()
    for lg in mut_dir.glob("mut_*.log"):
        tail = lg.read_text(encoding="utf-8", errors="ignore").strip().splitlines()[-1:]
        for t in tail:
            if "[exit code" in t:
                exit_codes.add(t.strip())
    tasks_txt = (mut_dir / "rosetta_tasks.txt").read_text(encoding="utf-8")
    audit = [l for l in tasks_txt.splitlines() if l.startswith("# [编号映射]")]
    final = load_state(str(task_dir))
    result = {
        "task_id": task_id,
        "final_status": final.get("status"),
        "status_note": final.get("status_note"),
        "n_ddg": n_ddg,
        "summary_rows": len(rows),
        "exit_code_set": sorted(exit_codes),
        "audit_line": audit[0] if audit else None,
        "runner_pids": final.get("runner_pids"),
        "manifest_keys": sorted((final.get("manifest") or {}).keys()),
    }
    print(json.dumps(result, ensure_ascii=False, indent=1))
    ok = (final.get("status") == "completed" and n_ddg == 19 and len(rows) == 19
          and exit_codes == {"[exit code 0]"} and audit)
    print("PHASE1:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def phase_fail_sim():
    import task_runtime as tr
    import rosetta_tools as rt

    task_id, task_dir = tr.create_task_dir("FAILSIM")
    task_dir = Path(task_dir)
    tr.state_write(str(task_dir), status="created", stage="created",
                   input_summary={"protein": "sim", "mutation": "67", "entry": "fail_sim"})
    # 手工放置"弛豫产物"（从真实任务拷贝或最小构造：工具只检查存在性）
    relax_dir = task_dir / "cartesian_relax"
    relax_dir.mkdir(parents=True)
    (relax_dir / "score.sc").write_text(
        "SEQUENCE:\nSCORE: total_score description\nSCORE: -100.0 dummy_0001\n", encoding="utf-8")
    src_wt = ROOT / "inputs" / "pdbs" / "1MC9_protein.pdb"
    shutil.copy(src_wt, relax_dir / "wt_relaxed.pdb")
    # 工具前置检查还要求清洗结构存在（否则直接【执行报错】，不会走到 runner）
    shutil.copy(src_wt, relax_dir / "complex_rosetta_ready.pdb")

    # 子进程以 ROSETTA_BIN_DIR=/nonexistent 运行 → 19 个计算全部启动失败
    child = (f"import sys; sys.path.insert(0, {str(ROOT)!r}); import os; os.chdir({str(ROOT)!r})\n"
             f"import task_runtime as tr, rosetta_tools as rt\n"
             f"rt.current_workspace.set({str(task_dir)!r})\n"
             f"print(rt.run_saturation_mutagenesis.func(target_resnum='67', jobs=8, delay=0.0))\n")
    env = dict(os.environ)
    env["ROSETTA_BIN_DIR"] = "/nonexistent_rosetta_bin"
    r = subprocess.run([PY, "-c", child], env=env, capture_output=True, text=True,
                       timeout=300, cwd=str(ROOT))
    print("[fail-sim] 工具返回:", (r.stdout or r.stderr).strip()[:120])

    deadline = time.time() + 5 * 60
    final = {}
    while time.time() < deadline:
        final = load_state(str(task_dir))
        if final.get("status") in ("completed", "failed", "interrupted"):
            break
        time.sleep(10)

    n_ddg = len(glob.glob(str(task_dir / "saturation_mutagenesis" / "*.ddg")))
    summary = task_dir / "summary" / "ddg_results.txt"
    logs = sorted((task_dir / "saturation_mutagenesis").glob("mut_*.log"))
    bad_codes = set()
    for lg in logs:
        t = lg.read_text(encoding="utf-8", errors="ignore")
        for line in t.splitlines():
            if "[exit code" in line:
                bad_codes.add(line.strip())
    result = {
        "task_id": task_id,
        "final_status": final.get("status"),
        "error_type": final.get("error_type"),
        "n_ddg": n_ddg,
        "summary_exists_header_only": summary.exists() and n_ddg == 0,
        "mut_logs": len(logs),
        "exit_code_set": sorted(bad_codes),
    }
    print(json.dumps(result, ensure_ascii=False, indent=1))
    ok = (final.get("status") == "failed"
          and final.get("error_type") == "NoSuccessfulMutants"
          and n_ddg == 0)
    print("PHASE2(fail-sim):", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    if "--fail-sim" in sys.argv:
        sys.exit(phase_fail_sim())
    sys.exit(phase_main())
