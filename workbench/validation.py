"""输入校验：静态 PDB 解析 + 突变目标核对（v0.2）

只依赖标准库。在消耗任何 Rosetta 算力**之前**拦住：
  - 多 MODEL 文件、目标链不存在、残基号不存在、野生型氨基酸不匹配、
    插入码目标、HETATM 配体（超出单体口径时明确警告/拒绝）。
"""

import os
from collections import Counter
from dataclasses import dataclass, field

# 标准氨基酸 3→1（与 rosetta_tools._AA_3TO1 同表；此处独立定义以保持 workbench 零重依赖）
THREE_TO_ONE = {
    "ALA": "A", "CYS": "C", "ASP": "D", "GLU": "E", "PHE": "F", "GLY": "G",
    "HIS": "H", "ILE": "I", "LYS": "K", "LEU": "L", "MET": "M", "ASN": "N",
    "PRO": "P", "GLN": "Q", "ARG": "R", "SER": "S", "THR": "T", "VAL": "V",
    "TRP": "W", "TYR": "Y",
}
ONE_TO_THREE = {v: k for k, v in THREE_TO_ONE.items()}


@dataclass
class ResidueInfo:
    """一条蛋白残基（按 ATOM 记录文件顺序）"""
    seq_index: int      # 文件内顺序编号（1 起）——cartesian_ddg mutfile 使用该编号
    chain: str
    resnum: int         # PDB 晶体学编号
    icode: str          # 插入码（" " 表示无）
    aa3: str
    aa1: str            # 非标准残基为 "X"


@dataclass
class PdbSummary:
    path: str
    residues: list = field(default_factory=list)          # [ResidueInfo]
    chains: dict = field(default_factory=dict)            # {chain: [ResidueInfo]}
    has_multi_model: bool = False
    hetatm_resnames: Counter = field(default_factory=Counter)  # 非 HOH 的 HETATM 计数
    n_water: int = 0
    warnings: list = field(default_factory=list)


def parse_pdb(path: str) -> PdbSummary:
    """解析 PDB 的 ATOM/HETATM/MODEL 记录（顺序即 Rosetta 顺序编号依据）。"""
    if not os.path.isfile(path):
        raise FileNotFoundError(f"PDB 文件不存在: {path}")

    summary = PdbSummary(path=os.path.abspath(path))
    seen_ca = set()  # (chain, resnum, icode) —— 一个残基只记录一次（首次出现，跳过 altloc 副本）

    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            rec = line[:6]
            if rec.startswith("MODEL "):
                summary.has_multi_model = True
            elif rec.startswith("HETATM"):
                resn = line[17:20].strip()
                if resn == "HOH":
                    summary.n_water += 1
                else:
                    summary.hetatm_resnames[resn] += 1
            elif rec.startswith("ATOM  "):
                altloc = line[16]
                if altloc not in (" ", "A"):
                    continue  # 只取首个 altloc，避免同一残基重复计数
                chain = line[21].strip()
                aa3 = line[17:20].strip()
                if aa3 not in THREE_TO_ONE:
                    continue  # 非：标准氨基酸残基（如 MSE 由 HETATM/其他记录处理，本版不 silently 算数）
                try:
                    resnum = int(line[22:26])
                except ValueError:
                    continue
                icode = line[26] if len(line) > 26 else " "
                key = (chain, resnum, icode)
                if key in seen_ca:
                    continue
                seen_ca.add(key)

                seq_index = len(summary.residues) + 1
                info = ResidueInfo(
                    seq_index=seq_index, chain=chain, resnum=resnum,
                    icode=icode, aa3=aa3, aa1=THREE_TO_ONE[aa3],
                )
                summary.residues.append(info)
                summary.chains.setdefault(chain, []).append(info)

    if summary.hetatm_resnames:
        summary.warnings.append(
            f"检测到非水 HETATM 残基: {dict(summary.hetatm_resnames)} —— "
            "单体稳定性口径不处理配体/修饰残基，Rosetta 可能忽略或报错，请确认是否符合预期"
        )
    return summary


def validate_target(summary: PdbSummary, chain: str, resnum: int,
                    expected_wt: str = "") -> "tuple[bool, str, ResidueInfo | None]":
    """核对突变目标。返回 (ok, message, residue)；失败时 residue 为 None。"""
    if summary.has_multi_model:
        return False, "拒绝执行：PDB 含多个 MODEL（NMR/多构象文件）。请先提取单一模型再提交。", None

    if chain not in summary.chains:
        found = ", ".join(f"'{c}'({len(rs)} 残基)" for c, rs in summary.chains.items()) or "无"
        return False, f"拒绝执行：链 '{chain}' 不存在。文件中的链: {found}", None

    chain_res = summary.chains[chain]
    hit = None
    for r in chain_res:
        if r.resnum == resnum:
            hit = r
            break
    if hit is None:
        nums = sorted(r.resnum for r in chain_res)
        return False, (
            f"拒绝执行：链 '{chain}' 中不存在残基编号 {resnum}。"
            f"该链编号范围 {nums[0]}–{nums[-1]}（共 {len(nums)} 残基）"
        ), None

    if hit.icode.strip():
        return False, (
            f"拒绝执行：残基 {chain}:{resnum} 带插入码 '{hit.icode}'。"
            "v0.2 不支持插入码位点，请改用不含插入码的结构或换位点。"
        ), None

    if expected_wt:
        if len(expected_wt) != 1 or expected_wt.upper() not in "ACDEFGHIKLMNPQRSTVWY":
            return False, f"拒绝执行：野生型氨基酸 '{expected_wt}' 不是合法的单字母编码。", None
        if hit.aa1 != expected_wt.upper():
            return False, (
                f"拒绝执行：野生型氨基酸不匹配。你在 {chain}:{resnum} 声明 WT={expected_wt.upper()}，"
                f"但文件里实际是 {hit.aa1}（{hit.aa3}）。请核对编号或使用 --wt 留空自动读取。"
            ), None

    # 同链内编号唯一性（重复编号会让“晶体学编号”变得有歧义）
    dup = [r.resnum for r in chain_res if r.resnum == resnum]
    if len(dup) > 1:
        return False, f"拒绝执行：链 '{chain}' 中编号 {resnum} 出现 {len(dup)} 次（含插入码分段），映射有歧义。", None

    return True, f"目标核对通过: {chain}:{resnum} {hit.aa1} ({hit.aa3})，文件内顺序编号 #{hit.seq_index}", hit
