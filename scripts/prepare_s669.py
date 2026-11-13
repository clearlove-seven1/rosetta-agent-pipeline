#!/usr/bin/env python
"""把 ThermoMPNN(Kuhlman Lab)镜像的 S669 原始 CSV 转换为 workbench 基准 schema。

来源与许可（详见 docs/data_provenance.md）：
  - 原始数据集：S669 —— Pancotti et al., Briefings in Bioinformatics 2022,
    doi:10.1093/bib/bbab555（PDB 编号，单点突变，ΔΔG kcal/mol）
  - 镜像：https://github.com/Kuhlman-Lab/ThermoMPNN（MIT License），
    data_all/testing/s669_clean_dir.csv

用法：
  python scripts/prepare_s669.py <s669_raw_original.csv> rosetta_manuals/stability_benchmark_s669.csv

本脚本只做格式转换，不修改数值；转换在本地完成，数据文件不入仓库（见 .gitignore）。
"""

import csv
import re
import sys

SOURCE = "S669"
REFERENCE = "Pancotti et al. 2022, doi:10.1093/bib/bbab555"
MUT_RE = re.compile(r"^([A-Za-z])(\d+)([A-Za-z])$")


def main(src: str, dst: str) -> int:
    n_ok, n_bad = 0, 0
    with open(src, "r", encoding="utf-8", errors="replace") as fin, \
         open(dst, "w", newline="", encoding="utf-8") as fout:
        w = csv.writer(fout)
        w.writerow(["pdb_id", "chain", "resnum_pdb", "wt_aa1", "mut_aa1",
                    "exp_ddg_kcal_mol", "source", "reference"])
        reader = csv.DictReader(fin)
        for row in reader:
            pdb_chain = (row.get("PDB") or "").strip()
            mut = (row.get("MUT") or "").strip()
            ddg = (row.get("DDG") or "").strip()
            if len(pdb_chain) < 5:
                n_bad += 1
                continue
            m = MUT_RE.match(mut)
            if not m:
                n_bad += 1
                continue
            pdb_id, chain = pdb_chain[:4].upper(), pdb_chain[4:].upper()
            wt, resnum, mut_aa = m.group(1).upper(), m.group(2), m.group(3).upper()
            try:
                ddg_val = float(ddg)
            except ValueError:
                n_bad += 1
                continue
            w.writerow([pdb_id, chain, resnum, wt, mut_aa,
                        f"{ddg_val:.3f}", SOURCE, REFERENCE])
            n_ok += 1
    print(f"转换完成: {n_ok} 条写入 {dst}（跳过 {n_bad} 条格式异常）")
    return 0 if n_ok > 0 else 1


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print(__doc__)
        sys.exit(2)
    sys.exit(main(sys.argv[1], sys.argv[2]))
