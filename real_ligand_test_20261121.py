"""real_ligand_test_20261121.py — Playbook B 真实实测(RCSB 3HTB:单链 HIV 蛋白酶 + 配体 JZ4)。
流程:任务创建 → suggest_plan 体系判定 → JZ4 提取 → parameterize_ligand → 复合物弛豫 →
带 -extra_res_fa 突变扫描(A_25 活性位点,export_structures) → summarize_results/contract_check。
不经 LLM,工具层直调;输出 JSON 汇总供验收。
"""
import glob
import json
import os
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)


def load_state(td):
    p = Path(td) / "task_status.json"
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def main():
    import task_runtime as tr
    import rosetta_tools as rt

    raw = Path("/tmp/3HTB.pdb")
    assert raw.exists(), "先下载 3HTB"

    task_id, task_dir = tr.create_task_dir("3HTB_ligtest")
    task_dir = Path(task_dir)
    shutil.copy(raw, task_dir / "protein.pdb")  # 原样复合物(含 HETATM),模拟用户上传
    # 配体单独提取:仅 JZ4 的 HETATM 行(用户场景=自己提供配体文件)
    lig_lines = [l for l in raw.read_text(encoding="utf-8").splitlines()
                 if l.startswith("HETATM") and l[17:20].strip() == "JZ4"]
    (task_dir / "ligand.pdb").write_text("\n".join(lig_lines) + "\nEND\n", encoding="utf-8")
    tr.state_write(str(task_dir), status="created", stage="created",
                   input_summary={"protein": "3HTB.pdb", "ligand": "JZ4", "entry": "ligand_test"})
    print(f"[任务] {task_id}  配体原子: {len(lig_lines)}")

    # 1) 体系判定(新体系问计划的真实演示)
    rt.current_workspace.set(str(task_dir))
    plan = rt.suggest_plan.func(protein_pdb_path=str(task_dir / "protein.pdb"))
    print("[suggest_plan]\n" + plan)
    info = tr.classify_system(str(task_dir / "protein.pdb"))

    # 2) 预检(活性位点 A_25)
    ok, note = tr.precheck_targets(str(task_dir / "protein.pdb"), "A_25")
    print(f"[预检] ok={ok} note={note[:100]}")
    assert ok, note

    # 3) 配体参数化
    r = rt.parameterize_ligand.func(ligand_path="ligand.pdb")
    print(f"[参数化] {r.strip()[:160]}")
    params = glob.glob(str(task_dir / "parameterize_ligand" / "*.params"))
    if not params:
        print("RESULT:", json.dumps({"stage": "parameterize_ligand", "detail": r[:300]},
                                    ensure_ascii=False))
        return 1

    # 4) 复合物弛豫
    r = rt.run_cartesian_relax.func(protein_pdb="protein.pdb", nstruct=2)
    print(f"[弛豫] {r.strip()[:120]}")

    # 5) 带配体突变扫描(A_25,export_structures)
    r = rt.run_saturation_mutagenesis.func(target_resnum="A_25", jobs=16, delay=1.0,
                                           export_structures=True)
    print(f"[突变] {r.strip()[:160]}")
    assert "【流水线通关】" in r, r

    # 6) 轮询至终态(上限 40 分钟)
    mut_dir = task_dir / "saturation_mutagenesis"
    deadline = time.time() + 40 * 60
    last = ""
    while time.time() < deadline:
        st = load_state(str(task_dir))
        n = len(glob.glob(str(mut_dir / "*.ddg")))
        line = f"status={st.get('status')} ddg={n}"
        if line != last:
            print(f"[轮询] {line}")
            last = line
        if st.get("status") in ("completed", "failed", "interrupted"):
            break
        time.sleep(30)

    final = load_state(str(task_dir))
    cc = tr.contract_check(str(task_dir))
    report = rt.summarize_results.func(task_dir=str(task_dir))
    print("[结果报告]\n" + report)
    sroot = task_dir / "saturation_mutagenesis" / "mutant_structures"
    struct_dirs = sorted(d for d in (os.listdir(sroot) if sroot.exists() else [])
                         if (sroot / d).is_dir())
    n_ddg = len(glob.glob(str(mut_dir / "*.ddg")))
    summary = task_dir / "summary" / "ddg_results.txt"
    rows = [l for l in (summary.read_text(encoding="utf-8").splitlines()[1:]
                        if summary.exists() else []) if l.strip()]
    exit_codes = set()
    for lg in mut_dir.glob("mut_*.log"):
        for x in lg.read_text(encoding="utf-8", errors="ignore").strip().splitlines()[-1:]:
            if "[exit code" in x:
                exit_codes.add(x.strip())
    mf = final.get("manifest") or {}
    result = {
        "task_id": task_id,
        "classify": {"type": info["system_type"], "playbook": info["playbook"],
                     "hetatm": info["hetatm_resnames"]},
        "params_generated": bool(params),
        "final_status": final.get("status"),
        "n_ddg": n_ddg, "summary_rows": len(rows),
        "exit_code_set": sorted(exit_codes),
        "struct_dirs": len(struct_dirs),
        "extra_res_fa": (mf.get("ddg_params") or {}).get("extra_res_fa"),
        "rosetta_version_ok": bool(mf.get("rosetta_version")),
        "contract_ok": cc["ok"], "contract_legacy": cc["legacy"],
    }
    print(json.dumps(result, ensure_ascii=False, indent=1))
    ok = (final.get("status") == "completed" and n_ddg == 19 and len(rows) == 19
          and exit_codes == {"[exit code 0]"} and len(struct_dirs) == 19
          and cc["ok"] and bool(params))
    print("LIGAND-TEST:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
