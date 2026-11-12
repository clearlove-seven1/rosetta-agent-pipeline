"""编号映射：PDB 晶体学编号 ↔ Rosetta 顺序编号（v0.2）

算法与 rosetta_tools._build_rosetta_numbering 一致（CA 原子扫描、文件内顺序计数），
独立实现以保证 workbench 不依赖 langchain。
背景（本机实测证据，2026-09-10 任务 .ddg 标签）：本机的 cartesian_ddg mutfile
按【结构内顺序编号】解释残基号（例：PDB 编号 72 → .ddg 标签 MUT_69ALA），
因此 mutfile 必须写 seq_index；弛豫前后顺序编号一致性由 executors 校验。
"""

import csv
import os

from .validation import parse_pdb


def build_numbering(pdb_path: str):
    """返回 (residues, by_key, by_seq)

    residues : [ResidueInfo] 文件顺序
    by_key   : {(chain, resnum): ResidueInfo}（无插入码前提下唯一）
    by_seq   : {seq_index: ResidueInfo}
    """
    summary = parse_pdb(pdb_path)
    by_key = {(r.chain, r.resnum): r for r in summary.residues}
    by_seq = {r.seq_index: r for r in summary.residues}
    return summary.residues, by_key, by_seq


def save_mapping_csv(residues, csv_path: str) -> str:
    """落盘映射表（审计用）：seq_index, chain, resnum, aa1, aa3"""
    os.makedirs(os.path.dirname(csv_path) or ".", exist_ok=True)
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["seq_index", "chain", "pdb_resnum", "wt_aa1", "wt_aa3"])
        for r in residues:
            w.writerow([r.seq_index, r.chain, r.resnum, r.aa1, r.aa3])
    return csv_path


def mutfile_text(seq_index: int, wt_aa1: str, mut_aa1: str) -> str:
    """cartesian_ddg mutfile（单突变）。注意行内编号 = 结构内顺序编号。"""
    return f"total 1\n1\n{wt_aa1} {seq_index} {mut_aa1}\n"
