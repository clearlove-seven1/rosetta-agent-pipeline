#!/usr/bin/env python
"""v0.4 负相关排查·扩样本选结构（对 S669 基准做结构级验证与突变挑选）

流程：条数≥5 的结构 → 逐个下载 PDB → 验证（非 NMR、链存在、WT 逐条比对
通过率 ≥ 阈值）→ 每结构按实验 ddG 均匀取至多 --per-struct 条已验证突变。
输出 TSV：pdb, chain, resnum, wt, mut, exp_ddg —— 供批量脚本消费。

只读 RCSB 公开文件；不改任何现有产物。
"""

import argparse
import csv
import os
import sys
import urllib.request
from collections import defaultdict

RCSB = "https://files.rcsb.org/download/{}.pdb"
THREE_TO_ONE = {
    "ALA": "A", "CYS": "C", "ASP": "D", "GLU": "E", "PHE": "F", "GLY": "G",
    "HIS": "H", "ILE": "I", "LYS": "K", "LEU": "L", "MET": "M", "ASN": "N",
    "PRO": "P", "GLN": "Q", "ARG": "R", "SER": "S", "THR": "T", "VAL": "V",
    "TRP": "W", "TYR": "Y",
}


def fetch(pdb_id: str, cache_dir: str) -> str:
    os.makedirs(cache_dir, exist_ok=True)
    path = os.path.join(cache_dir, f"{pdb_id}.pdb")
    if os.path.exists(path) and os.path.getsize(path) > 1000:
        return path
    req = urllib.request.Request(RCSB.format(pdb_id), headers={"User-Agent": "workbench-expansion"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r, open(path, "wb") as f:
            f.write(r.read())
    except Exception as e:
        print(f"  [skip] {pdb_id} 下载失败: {e}")
        return ""
    return path


def verify(path: str, chain: str, claims: "dict[int, str]") -> "tuple[bool, str, dict[int, str]]":
    """验证结构可用性。返回 (ok, reason, 实测wt字典)。"""
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        lines = f.readlines()
    if any(l.startswith("MODEL ") for l in lines):
        return False, "NMR 多模型", {}
    # 结构实测 (resnum -> aa1)，仅目标链
    struct = {}
    for l in lines:
        if l.startswith("ATOM  ") and l[16] in (" ", "A"):
            ch = l[21].strip()
            if ch == chain:
                aa3 = l[17:20].strip()
                if aa3 in THREE_TO_ONE:
                    try:
                        struct[int(l[22:26])] = THREE_TO_ONE[aa3]
                    except ValueError:
                        pass
    ok_n, miss = 0, 0
    for pos, wt in claims.items():
        got = struct.get(pos)
        if got is None:
            miss += 1
        elif got == wt:
            ok_n += 1
    checked = ok_n + miss
    if checked == 0:
        return False, "声称位点在结构中全部缺失", struct
    frac = ok_n / checked
    if frac < 0.8:
        return False, f"WT 比对通过率 {frac:.0%}（{ok_n}/{checked}）——编号体系不符", struct
    return True, f"WT 比对 {ok_n}/{checked}", struct


def pick_mutations(entries: list, struct_wt: dict, per_struct: int) -> list:
    """按实验 ddG 均匀取点；只保留结构实测 WT 与声称一致的突变。"""
    valid = [e for e in entries if struct_wt.get(e["resnum_pdb"]) == e["wt_aa1"]]
    valid.sort(key=lambda e: e["exp_ddg_kcal_mol"])
    if len(valid) <= per_struct:
        return valid
    step = (len(valid) - 1) / (per_struct - 1)
    picked, used = [], set()
    for i in range(per_struct):
        idx = round(i * step)
        if idx in used:
            idx = min(idx + 1, len(valid) - 1)
        while idx in used:
            idx += 1
            if idx >= len(valid):
                break
        if idx >= len(valid):
            break
        used.add(idx)
        picked.append(valid[idx])
    return picked


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--benchmark", default="rosetta_manuals/stability_benchmark_s669.csv")
    ap.add_argument("--out", default="runs/expansion_selection.tsv")
    ap.add_argument("--cache", default="/tmp/s669_pdbs")
    ap.add_argument("--min-entries", type=int, default=5)
    ap.add_argument("--per-struct", type=int, default=15)
    ap.add_argument("--max-structs", type=int, default=10)
    ap.add_argument("--exclude", default="2HBB", help="已跑过的结构（逗号分隔）")
    a = ap.parse_args()
    done = {x.strip().upper() for x in a.exclude.split(",") if x.strip()}

    groups = defaultdict(list)
    with open(a.benchmark, "r", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            groups[(row["pdb_id"], row["chain"])].append({
                "resnum_pdb": int(row["resnum_pdb"]), "wt_aa1": row["wt_aa1"],
                "mut_aa1": row["mut_aa1"], "exp_ddg_kcal_mol": float(row["exp_ddg_kcal_mol"]),
            })
    cand = sorted(((k, v) for k, v in groups.items() if len(v) >= a.min_entries and k[0] not in done),
                  key=lambda kv: -len(kv[1]))

    os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)
    n_struct, n_mut = 0, 0
    with open(a.out, "w", encoding="utf-8") as out:
        out.write("pdb_id\tchain\tresnum_pdb\twt_aa1\tmut_aa1\texp_ddg_kcal_mol\n")
        for (pdb_id, chain), entries in cand:
            if n_struct >= a.max_structs:
                break
            path = fetch(pdb_id, a.cache)
            if not path:
                continue
            claims = defaultdict(str)
            for e in entries:
                claims[e["resnum_pdb"]] = e["wt_aa1"]
            ok, reason, struct_wt = verify(path, chain, claims)
            print(f"{pdb_id}_{chain}  条数={len(entries)}  {reason}")
            if not ok:
                continue
            picked = pick_mutations(entries, struct_wt, a.per_struct)
            if len(picked) < 3:
                print(f"  [skip] 可用突变不足 3 条")
                continue
            for e in picked:
                out.write(f"{pdb_id}\t{chain}\t{e['resnum_pdb']}\t{e['wt_aa1']}\t"
                          f"{e['mut_aa1']}\t{e['exp_ddg_kcal_mol']}\n")
            n_struct += 1
            n_mut += len(picked)
            print(f"  ✓ 入选 {len(picked)} 条突变")
    print(f"\n合计: {n_struct} 个结构, {n_mut} 条突变 → {a.out}")
    return 0 if n_struct > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
