"""real_export_test_20261121.py — 真实 Rosetta 结构导出验收(1MC9 A_67,19 突变全导出)。
独立任务目录,不经 LLM;核对:终态/19 ddg/19 目录/内容级落点(独立复核)/manifest/契约。
运行(rosetta-agent env,仓库根): python real_export_test_20261121.py
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

AA3 = {"A": "ALA", "C": "CYS", "D": "ASP", "E": "GLU", "F": "PHE", "G": "GLY", "H": "HIS",
       "I": "ILE", "K": "LYS", "L": "LEU", "M": "MET", "N": "ASN", "P": "PRO", "Q": "GLN",
       "R": "ARG", "S": "SER", "T": "THR", "V": "VAL", "W": "TRP", "Y": "TYR"}


def load_state(td):
    p = Path(td) / "task_status.json"
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def main():
    import task_runtime as tr
    import rosetta_tools as rt

    load1 = os.getloadavg()[0]
    if load1 > 150:
        print(f"[中止] load1={load1}")
        return 2
    print(f"[资源] load1={load1} cores={os.cpu_count()}")

    task_id, task_dir = tr.create_task_dir("1MC9_export")
    task_dir = Path(task_dir)
    shutil.copy(ROOT / "inputs" / "pdbs" / "1MC9_protein.pdb", task_dir / "protein.pdb")
    tr.state_write(str(task_dir), status="created", stage="created",
                   input_summary={"protein": "1MC9_protein.pdb", "mutation": "67",
                                  "entry": "export_test"})
    print(f"[任务] {task_id}")
    ok, note = tr.precheck_targets(str(task_dir / "protein.pdb"), "67")
    print(f"[预检] ok={ok} note={note}")
    assert ok, note

    rt.current_workspace.set(str(task_dir))
    r = rt.run_cartesian_relax.func(protein_pdb="protein.pdb", nstruct=2)
    print(f"[弛豫] {r.strip()[:80]}")
    r = rt.run_saturation_mutagenesis.func(target_resnum="67", jobs=16, delay=1.0,
                                           export_structures=True)
    print(f"[突变] {r.strip()[:110]}")
    assert "【流水线通关】" in r, r

    mut_dir = task_dir / "saturation_mutagenesis"
    deadline = time.time() + 30 * 60
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
        time.sleep(20)

    final = load_state(str(task_dir))
    sroot = mut_dir / "mutant_structures"
    dirs_ = sorted(d for d in (os.listdir(sroot) if sroot.exists() else [])
                   if (sroot / d).is_dir())
    sm_path = sroot / "structures_manifest.json"
    sm = json.loads(sm_path.read_text(encoding="utf-8")) if sm_path.exists() else {}
    # 独立复核:每个目录取第一个 MUT 文件,核对 A_67 残基 == 目录名突变氨基酸
    bad = []
    for d in dirs_:
        mut = d.split("_")[-1]
        files = sorted((sroot / d).glob("MUT_*.pdb"))
        if not files:
            bad.append((d, "no files"))
            continue
        names = set()
        for l in files[0].read_text(errors="ignore").splitlines():
            if l.startswith("ATOM") and l[21:22].strip() == "A" and l[22:26].strip() == "67":
                names.add(l[17:20].strip())
        if names != {AA3.get(mut, "?")}:
            bad.append((d, sorted(names)))
    n_ddg = len(glob.glob(str(mut_dir / "*.ddg")))
    summary = task_dir / "summary" / "ddg_results.txt"
    rows = [l for l in (summary.read_text(encoding="utf-8").splitlines()[1:]
                        if summary.exists() else []) if l.strip()]
    exit_codes = set()
    for lg in mut_dir.glob("mut_*.log"):
        for x in lg.read_text(encoding="utf-8", errors="ignore").strip().splitlines()[-1:]:
            if "[exit code" in x:
                exit_codes.add(x.strip())
    cc = tr.contract_check(str(task_dir))
    mf = final.get("manifest") or {}
    result = {
        "task_id": task_id,
        "final_status": final.get("status"),
        "n_ddg": n_ddg,
        "summary_rows": len(rows),
        "exit_code_set": sorted(exit_codes),
        "struct_dirs": len(dirs_),
        "sample_dir": dirs_[0] if dirs_ else None,
        "sm_entries": len(sm.get("entries", [])),
        "sm_verified_yes": sum(1 for e in sm.get("entries", []) if e.get("verified") == "yes"),
        "independent_recheck_bad": bad,
        "manifest": {k: mf.get(k) for k in ("structures_export", "structures_exported",
                                            "structures_verified", "rosetta_version")},
        "artifacts": final.get("artifacts"),
        "contract_ok": cc["ok"],
        "contract_legacy": cc["legacy"],
    }
    print(json.dumps(result, ensure_ascii=False, indent=1))
    ok = (final.get("status") == "completed" and n_ddg == 19 and len(rows) == 19
          and exit_codes == {"[exit code 0]"} and len(dirs_) == 19
          and result["sm_verified_yes"] == 19 and not bad
          and mf.get("structures_exported") == 19 and mf.get("structures_verified") == 19
          and cc["ok"])
    print("EXPORT-TEST:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
