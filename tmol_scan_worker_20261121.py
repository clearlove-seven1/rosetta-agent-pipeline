"""TMol GPU 扫描 worker(2026-11-21)——在 tmol conda env(/opt/software/tmol/bin/python)内运行,
PYTHONPATH=$HOME/.tmol_torch(torch 2.11+cu130 影子加载,修 ABI)。

argv: pdb_path tokens_csv [out_json]
  tokens: PDB 晶体学编号,逗号分隔;可带链(A_25)或不带(76,仅单链)。
输出: out_json(JSON)+stdout 文本表。口径: beta_nov2016_cart(GPU),GPU 原生突变+Dunbrack 侧链重排;
固定骨架单点近似;不产出突变体结构。失败:非 0 退出 + stderr。
"""
import json
import os
import sys
import time

import torch

from tmol.io._structure_io import atom_array_from_file
from tmol.io._pose_stack_from_biotite import pose_stack_from_atom_array
from tmol.score import beta2016_score_function
from tmol.pack._mutate import mutate_residue

AA20 = ["ALA", "CYS", "ASP", "GLU", "PHE", "GLY", "HIS", "ILE", "LYS", "LEU",
        "MET", "ASN", "PRO", "GLN", "ARG", "SER", "THR", "VAL", "TRP", "TYR"]


def main():
    pdb = sys.argv[1]
    tokens = [t.strip() for t in sys.argv[2].split(",") if t.strip()]
    out_json = sys.argv[3] if len(sys.argv) > 3 else ""
    dev = torch.device("cuda" if torch.cuda.is_available() else "cpu")

    aa = atom_array_from_file(pdb)
    ps = pose_stack_from_atom_array(aa, dev)
    n_blocks = ps.max_n_blocks

    # 块顺序 == atom_array 残基顺序(1MC9 实测验证;多链按文件顺序逐链)
    res_key, seen = [], set()
    resname_of = {}
    for c, r, rn in zip(aa.chain_id, aa.res_id, aa.res_name):
        k = (str(c), int(r))
        if k not in seen:
            seen.add(k)
            res_key.append(k)
            resname_of[k] = str(rn)
    if len(res_key) != n_blocks:
        raise SystemExit(f"[worker] 块数 {n_blocks} != 文件残基数 {len(res_key)},映射失败,如实报错")
    blk_of = {k: i for i, k in enumerate(res_key)}

    sfxn = beta2016_score_function(dev)
    scoring = sfxn.render_whole_pose_scoring_module(ps)
    wt_total = float(scoring(ps.coords)[0])

    results = {}
    for tok in tokens:
        if "_" in tok:
            ch, num = tok.split("_", 1)
            key = (ch, int(num))
        else:
            chains = {k[0] for k in res_key}
            if len(chains) != 1:
                raise SystemExit(f"[worker] 多链结构位点必须带链号: {tok}(可用链: {sorted(chains)})")
            key = (chains.pop(), int(tok))
        if key not in blk_of:
            raise SystemExit(f"[worker] 位点 {tok} 不在结构中(可用: {res_key[0]}..{res_key[-1]})")
        wt3 = resname_of[key]
        blk = blk_of[key]
        muts = {}
        for name3 in AA20:
            if name3 == wt3:
                continue
            t0 = time.time()
            mut = mutate_residue(ps, 0, blk, name3)
            sc = float(sfxn.render_whole_pose_scoring_module(mut)(mut.coords)[0])
            muts[name3] = {"total": round(sc, 3), "ddG": round(sc - wt_total, 3),
                           "sec": round(time.time() - t0, 1)}
        srt = sorted(muts.items(), key=lambda kv: kv[1]["ddG"])
        results[f"{key[0]}_{key[1]}"] = {"wt": wt3, "wt_total": round(wt_total, 3),
                                         "device": str(dev),
                                         "top_stable": [{"aa": n, "ddG": d["ddG"],
                                                         "total": d["total"]} for n, d in srt[:3]],
                                         "mutations": muts}

    out = {"engine": "tmol", "weights": "beta_nov2016_cart", "device": str(dev),
           "pdb": os.path.basename(pdb), "n_blocks": n_blocks,
           "wall_sec": None, "results": results}
    if out_json:
        json.dump(out, open(out_json, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    for site, r in results.items():
        print(f"== {site} (WT={r['wt']}, wt_total={r['wt_total']}, {r['device']}) ==")
        for n, d in sorted(r["mutations"].items(), key=lambda kv: kv[1]["ddG"]):
            print(f"  {n}: ddG={d['ddG']:+.3f}")
    print("TMOL_SCAN_DONE")


if __name__ == "__main__":
    main()
