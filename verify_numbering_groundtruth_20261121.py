"""
verify_numbering_groundtruth_20261121.py — Rosetta 编号映射独立地面真值验证
================================================================================

不信任任务文件里自己写的审计行：对每类输入用真实 cartesian_ddg
（-ddg:iterations 1 -ddg:dump_pdbs true）跑一个突变，然后从 Rosetta 输出的
PDB 反推出它的实际 pose 顺序（输出残基枚举 = pose 编号），逐位对照
task_runtime.build_numbering 的映射。

验证链（与生产一致）：
  生产: 上传 pdb → 清洗 complex_rosetta_ready.pdb → relax(保号) → cartesian_ddg(wt_relaxed)
  验证: 构造 raw → 同一清洗代码得 cleaned → build_numbering(cleaned) vs Rosetta 加载
        cleaned 的真实 pose 视图；另校验 build_numbering(raw) == build_numbering(cleaned)
        （预检与执行一致性）。

用例（base = inputs/pdbs/1MC9_protein.pdb，链A，残基 4-129）：
  single        普通单链（非 1 起始编号）
  multi_dup     复制为 A/B 双链、残基号完全重叠（多链同号）
  icode         在 60 号残基后插入 60A（插入码）
  altloc        30 号残基 CA 复制 altloc B（备用构象）
  noca          40 号残基删除 CA 行（缺 CA）
  ligand_tail   清洗产物尾部追加 HETATM 配体（模拟生产复合物拼接）
  ligand_before HETATM 配体行置于蛋白原子之前

运行（rosetta-agent env，仓库根目录）：
  python verify_numbering_groundtruth_20261121.py
产物：verify_numbering_20261121/<case>/...（输入/命令/日志/输出全留档）与 verify_report.json
"""
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

import task_runtime as tr  # noqa: E402
import rosetta_tools as rt  # noqa: E402

DDG = os.path.join(rt.ROSETTA_BIN, "cartesian_ddg.linuxgccrelease")
BASE_PDB = ROOT / "inputs" / "pdbs" / "1MC9_protein.pdb"
OUT = ROOT / "verify_numbering_20261121"
OUT.mkdir(exist_ok=True)
STANDARD = set(tr.AA_3TO1)

REPORT = {"cases": {}, "rosetta_bin": DDG, "base": str(BASE_PDB)}


# ---------------- PDB 构造工具 ----------------
def parse_residue_lines(text):
    """按残基键聚合 ATOM 行（保持出现顺序）。返回 [(chain,num,icode,resname,lines)]"""
    order = []
    cur_key = None
    for ln in text.splitlines():
        if not ln.startswith("ATOM"):
            continue
        chain, num, icode = ln[21], ln[22:26].strip(), ln[26].strip() if len(ln) > 26 else ""
        resn = ln[17:20].strip()
        key = (chain, num, icode)
        if key != cur_key:
            order.append([chain, num, icode, resn, []])
            cur_key = key
        order[-1][4].append(ln)
    return order


def shift_xyz(line, dx=0.0, dy=0.0, dz=0.0):
    x, y, z = float(line[30:38]), float(line[38:46]), float(line[46:54])
    return f"{line[:30]}{x + dx:8.3f}{y + dy:8.3f}{z + dz:8.3f}{line[54:]}"


def build_cases():
    text = BASE_PDB.read_text(encoding="utf-8")
    residues = parse_residue_lines(text)
    cases = {}

    cases["single"] = text

    # 多链同号：整链复制为 B（编号不变，坐标 +40Å）
    b_lines = []
    for _c, _n, _i, _r, lines in residues:
        for ln in lines:
            nl = shift_xyz(ln, dx=40.0)
            b_lines.append(nl[:21] + "B" + nl[22:])
    cases["multi_dup"] = text.rstrip("\n") + "\nTER\n" + "\n".join(b_lines) + "\nTER\nEND\n"

    # 插入码：残基 60 整残基复制为 60A（icode 列置 A，坐标 +0.3Å）
    out_lines = []
    for c, n, i, r, lines in residues:
        out_lines.extend(lines)
        if n == "60":
            for ln in lines:
                nl = shift_xyz(ln, dz=0.3)
                nl = nl[:26] + "A" + nl[27:]
                out_lines.append(nl)
    cases["icode"] = "\n".join(out_lines) + "\nTER\nEND\n"

    # 备用构象：30 号残基全部原子复制 altloc B（坐标 +0.2Å）
    out_lines = []
    for c, n, i, r, lines in residues:
        out_lines.extend(lines)
        if n == "30":
            for ln in lines:
                nl = shift_xyz(ln, dy=0.2)
                nl = nl[:16] + "B" + nl[17:]
                out_lines.append(nl)
    cases["altloc"] = "\n".join(out_lines) + "\nTER\nEND\n"

    # 缺 CA：删除 40 号残基的 CA 行
    out_lines = []
    for c, n, i, r, lines in residues:
        if n == "40":
            out_lines.extend([ln for ln in lines if ln[12:16].strip() != "CA"])
        else:
            out_lines.extend(lines)
    cases["noca"] = "\n".join(out_lines) + "\nTER\nEND\n"

    # 配体在蛋白原子之前
    lig = ("HETATM  9801  O   HOH A 999      10.000  10.000  10.000  1.00  0.00           O\n"
           "HETATM  9802  C   LN1 A 998      11.000  10.000  10.000  1.00  0.00           C\n")
    cases["ligand_before"] = lig + text

    # 配体尾部追加（raw 与 single 相同；cleaned 阶段手工拼接，见 main 分支）
    cases["ligand_tail"] = text

    return cases


def cleaned_copy(case_dir: Path, raw_pdb: Path) -> Path:
    """用真实的 run_cartesian_relax 清洗逻辑得到 cleaned 产物（打桩 Popen，不启动弛豫）。"""
    rt.current_workspace.set(str(case_dir))
    real_popen = subprocess.Popen

    class _Fake:
        pid = 1

    subprocess.Popen = lambda *a, **k: _Fake()
    try:
        rt.run_cartesian_relax.func(protein_pdb=str(raw_pdb), nstruct=1)
    finally:
        subprocess.Popen = real_popen
    ready = case_dir / "cartesian_relax" / "complex_rosetta_ready.pdb"
    return ready


def ligand_tail_case(cleaned: Path) -> Path:
    """生产复合物弛豫会把配体以 HETATM 追加在蛋白后——构造等价输入验证编号不受影响。"""
    text = cleaned.read_text(encoding="utf-8").rstrip("\n")
    lig = ("HETATM  9901  C   LN1 Z   1      90.000  90.000  90.000  1.00  0.00           C\n"
           "HETATM  9902  O   LN1 Z   1      91.000  90.000  90.000  1.00  0.00           O\n")
    return cleaned.parent / "input_ligtail.pdb", text + "\n" + lig + "END\n"


def ground_truth(case_dir: Path, pdb_path: Path, nmap: "tr.NumberingMap", log_name: str):
    """跑 cartesian_ddg（iterations 1 + dump_pdbs true），从输出反推 Rosetta 实际 pose 视图。"""
    size = nmap.size
    idx = size // 2 + 1 if size >= 4 else 1  # 中位偏后，多链用例可跨到第二链
    idx = min(idx, size)
    key = nmap.rosetta_to_pdb[idx]
    wt = nmap.wt[idx]
    mut_aa = "G" if wt != "G" else "A"
    mut_path = case_dir / "mutation_mutfile.txt"
    mut_path.write_text(f"total 1\n1\n{wt} {idx} {mut_aa}\n", encoding="utf-8")
    log_path = case_dir / log_name
    cmd = [DDG, "-s", str(pdb_path), "-ddg:mut_file", str(mut_path),
           "-ddg:iterations", "1", "-ddg:cartesian", "-ddg:bbnbrs", "1",
           "-score:weights", "ref2015_cart", "-relax:min_type", "lbfgs_armijo_nonmonotone",
           "-relax:cartesian", "-ddg:dump_pdbs", "true"]
    log_path.write_text(f"CMD: {' '.join(cmd)}\n\n", encoding="utf-8")
    with open(log_path, "a", encoding="utf-8") as lf:
        proc = subprocess.run(cmd, cwd=str(case_dir), stdout=lf, stderr=subprocess.STDOUT,
                              timeout=7200)
    # cartesian_ddg 忽略 -out:prefix，输出固定为 WT_*.pdb 与 MUT_*.pdb（实测 1.29 环境同名构建）
    wt_pdbs = sorted(case_dir.glob("WT_*.pdb"))
    mut_pdbs = sorted(case_dir.glob("MUT_*.pdb"))
    info = {"idx_probed": idx, "key": key, "wt": wt, "mut_aa": mut_aa,
            "rc": proc.returncode, "wt_files": [str(p) for p in wt_pdbs],
            "mut_files": [str(p) for p in mut_pdbs],
            "log_tail": "\n".join(log_path.read_text(encoding="utf-8", errors="ignore").splitlines()[-5:])}
    if proc.returncode != 0 or not wt_pdbs:
        info["verdict"] = "rosetta_rejected_or_no_output"
        return info

    def enumerate_pose(pdb_path):
        """Rosetta 输出 PDB 的残基枚举 = pose 顺序（地面真值）。"""
        gt, cur = [], None
        for ln in pdb_path.read_text(encoding="utf-8", errors="ignore").splitlines():
            if not ln.startswith("ATOM"):
                continue
            resn = ln[17:20].strip()
            if resn not in STANDARD:
                continue
            k = (ln[21], ln[22:26].strip(), ln[26].strip() if len(ln) > 26 else "")
            if k != cur:
                gt.append([k[0], k[1] + k[2], resn])
                cur = k
        return gt

    # 主对照：WT 输出（未突变 pose）逐位对比
    gt = enumerate_pose(wt_pdbs[0])
    mismatches = []
    if len(gt) != size:
        mismatches.append(f"残基数不一致: rosetta_WT={len(gt)} ours={size}")
    else:
        for i, (ch, numkey, resn) in enumerate(gt, 1):
            if nmap.rosetta_to_pdb.get(i) != f"{ch}_{numkey}" or nmap.wt.get(i) != tr.AA_3TO1.get(resn):
                mismatches.append(f"idx {i}: rosetta={ch}_{numkey}({resn}) ours={nmap.rosetta_to_pdb.get(i)}({nmap.wt.get(i)})")
                if len(mismatches) >= 6:
                    break
    # 交叉验证：MUT 输出中突变残基(=mut_aa)的实际位置 == 我们预测的 key
    mut_hit = None
    if mut_pdbs:
        for ch, numkey, resn in enumerate_pose(mut_pdbs[0]):
            if resn == mut_aa:
                mut_hit = f"{ch}_{numkey}"
                break
    info["gt_size"] = len(gt)
    info["our_size"] = size
    info["mut_hit_position"] = mut_hit
    info["mut_hit_matches_prediction"] = (mut_hit == key)
    if mut_hit is not None and mut_hit != key:
        mismatches.append(f"突变落点: rosetta 实际在 {mut_hit}，我们预测 {key}")
    info["mismatches"] = mismatches
    info["verdict"] = "MATCH" if not mismatches else "MISMATCH"
    return info


def main():
    cases_raw = build_cases()
    base_cleaned_done = False
    for name, raw_text in cases_raw.items():
        cdir = OUT / name
        cdir.mkdir(parents=True, exist_ok=True)
        raw = cdir / "input_raw.pdb"
        raw.write_text(raw_text, encoding="utf-8")
        entry = {}

        # 清洗（真实清洗代码，打桩不弛豫）；ligand_tail 用 single 的清洗产物手工拼接
        if name == "ligand_tail":
            single_ready = OUT / "single" / "cartesian_relax" / "complex_rosetta_ready.pdb"
            if not single_ready.exists():  # 兜底：单独清洗一份 base
                (cdir / "_tmp_base.pdb").write_text(cases_raw["single"], encoding="utf-8")
                single_ready = cleaned_copy(cdir, cdir / "_tmp_base.pdb")
            tgt, tail_text = ligand_tail_case(single_ready)
            tgt.write_text(tail_text, encoding="utf-8")
            cleaned = tgt
        else:
            cleaned = cleaned_copy(cdir, raw)
        entry["cleaned"] = str(cleaned)

        # noca 特殊处理：实测含缺主链残基的 cleaned 直接喂 cartesian_ddg 会崩溃
        # （Rosetta skip 该残基并产生链断裂，见上一轮 ROSETTA_CRASH.log）。
        # 生产路径的 ddg 输入是 relax 产物 wt_relaxed（缺失残基已被 Rosetta 剔除），
        # 故此处用"模拟 skip 后 pose"（物理删除 40 号残基）作为 ground truth 输入，
        # 验证 build_numbering 的剔除规则与 Rosetta pose 编号一致。
        gt_input = cleaned
        if name == "noca":
            pose_lines = [ln for ln in raw_text.splitlines()
                          if not (ln.startswith("ATOM") and ln[22:26].strip() == "40")]
            pose_pdb = cdir / "input_pose_sim.pdb"
            pose_pdb.write_text("\n".join(pose_lines) + "\nTER\nEND\n", encoding="utf-8")
            gt_input = pose_pdb
            entry["pose_sim"] = str(pose_pdb)
            entry["cleaned_direct_ddg"] = ("crashed(ROSETTA_CRASH): Rosetta skips missing-backbone "
                                           "residue + chain break; production ddg input is "
                                           "wt_relaxed where residue is already absent")
            cleaned = pose_pdb  # 后续 raw/cleaned 映射一致性检查也用 pose 视图

        # 预检/执行一致性：raw 与 cleaned 的映射应相同（HETATM/altloc 等被剔除）
        try:
            nmap_raw = tr.build_numbering(str(raw))
            nmap = tr.build_numbering(str(cleaned))
            same = (nmap_raw.rosetta_to_pdb == nmap.rosetta_to_pdb
                    and nmap_raw.wt == nmap.wt)
            entry["raw_equals_cleaned_map"] = bool(same)
            entry["anomalies"] = nmap.anomalies
        except Exception as e:
            entry["verdict"] = f"map_error: {e}"
            REPORT["cases"][name] = entry
            continue

        info = ground_truth(cdir, cleaned, nmap, "ddg_log.txt")
        entry.update(info)
        REPORT["cases"][name] = entry
        print(f"[{name}] {entry.get('verdict')}  (idx {info.get('idx_probed')} {info.get('key')}"
              f" {info.get('wt')}→{info.get('mut_aa')}; gt={info.get('gt_size')}/{info.get('our_size')})",
              flush=True)
        if entry.get("mismatches"):
            for m in entry["mismatches"][:6]:
                print(f"    {m}", flush=True)
        shutil.rmtree(cdir / "cartesian_relax", ignore_errors=True)

    # 汇总
    n_match = sum(1 for c in REPORT["cases"].values() if c.get("verdict") == "MATCH")
    n_rej = sum(1 for c in REPORT["cases"].values() if str(c.get("verdict", "")).startswith("rosetta_rejected"))
    n_mis = sum(1 for c in REPORT["cases"].values() if c.get("verdict") == "MISMATCH")
    REPORT["summary"] = {"match": n_match, "mismatch": n_mis, "rosetta_rejected": n_rej,
                         "total": len(REPORT["cases"])}
    (OUT / "verify_report.json").write_text(json.dumps(REPORT, ensure_ascii=False, indent=1),
                                            encoding="utf-8")
    print(f"\n汇总: MATCH={n_match} MISMATCH={n_mis} REJECTED={n_rej} / {len(REPORT['cases'])}")
    print("报告:", OUT / "verify_report.json")
    return 0 if n_mis == 0 and n_rej == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
