#!/usr/bin/env python
"""从选择 TSV 中剔除已有 succeeded run 的突变，生成剩余待跑清单。

用法：python scripts/filter_done.py <selection.tsv> <rest.tsv>
"""

import csv
import glob
import json
import sys


def main(src: str, dst: str) -> int:
    done = set()
    for f in glob.glob("runs/*/manifest.json"):
        try:
            m = json.load(open(f))
            if m.get("status") != "succeeded":
                continue
            t = m["task"]
            pdb = t["protein_path"].replace("\\", "/").split("/")[-1].split(".")[0].upper()
            done.add((pdb, t["chain"].upper(), int(t["resnum_pdb"]),
                      t["wt_aa"].upper(), t["mut_aa"].upper()))
        except Exception:
            continue  # 损坏的 manifest 不参与判重

    rows = list(csv.reader(open(src, "r", encoding="utf-8"), delimiter="\t"))
    kept, skipped = [], 0
    for r in rows[1:]:
        if len(r) < 6:
            continue
        key = (r[0].upper(), r[1].upper(), int(r[2]), r[3].upper(), r[4].upper())
        if key in done:
            skipped += 1
        else:
            kept.append(r)

    with open(dst, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, delimiter="\t")
        w.writerow(rows[0])
        w.writerows(kept)
    print(f"已完成跳过 {skipped} 条，剩余 {len(kept)} 条 → {dst}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2]))
