""".ddg 结果解析（v0.2）

格式（本机 Rosetta release-280 实测）：
  COMPLEX:   Round1: WT:  -393.944  fa_atr: ...
  COMPLEX:   Round1: MUT_69ALA:  -390.646  fa_atr: ...
ΔΔG = avg(MUT 各轮) - avg(WT 各轮)。

⚠️ 注意：文件内标签 MUT_<n><aa> 的 n 是 Rosetta 内部顺序编号，可能与 PDB
晶体学编号不同（本机证据：PDB 72 → 标签 69）。突变身份以我们生成的 mutfile
为准；这里仅做交叉核对（label_check），不一致时在报告中如实警告。
"""

import re


def parse_ddg(path: str) -> dict:
    """解析 .ddg 文件。返回 wt_scores/mut_scores/均值/ΔΔG 与标签核对所需信息。

    解析失败（无 WT 或无 MUT 行）抛 ValueError。
    """
    wt_scores, mut_labels = [], []
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            # MUT 标签为 3 字母氨基酸代码，如 MUT_69ALA
            m = re.match(r"COMPLEX:\s+Round(\d+):\s+(WT|MUT_\d+[A-Z]{3}):\s+(-?\d+\.?\d*)", line)
            if not m:
                continue
            label = m.group(2)
            score = float(m.group(3))
            if label == "WT":
                wt_scores.append(score)
            else:
                mut_labels.append((label, score))

    if not wt_scores:
        raise ValueError(f"{path}: 未解析到 WT 分数行")
    if not mut_labels:
        raise ValueError(f"{path}: 未解析到 MUT 分数行")

    mut_scores = [s for _, s in mut_labels]
    wt_avg = sum(wt_scores) / len(wt_scores)
    mut_avg = sum(mut_scores) / len(mut_scores)

    # 交叉核对：所有 MUT 标签应一致（同一残基同一突变）
    labels = {lb for lb, _ in mut_labels}
    return {
        "wt_scores": wt_scores,
        "mut_scores": mut_scores,
        "mut_labels": sorted(labels),
        "wt_avg": wt_avg,
        "mut_avg": mut_avg,
        "ddg": mut_avg - wt_avg,
        "n_rounds": len(mut_scores),
    }


def label_matches(ddg_info: dict, seq_index: int, mut_aa1: str) -> "tuple[bool, str]":
    """核对 .ddg 标签是否就是我们提交的突变。

    标签格式 MUT_<顺序编号><3字母代码>（如 MUT_69ALA），
    与我们 mutfile 的（顺序编号 + 单字母突变）一一对应。
    """
    from .validation import ONE_TO_THREE
    expected = f"MUT_{seq_index}{ONE_TO_THREE[mut_aa1]}"
    if ddg_info["mut_labels"] == [expected]:
        return True, f"标签核对通过: {expected}"
    return False, (
        f"标签核对不一致: 期望 {expected}，实际 {ddg_info['mut_labels']}。"
        "可能发生了残基错位——结果不可信，请人工核查编号映射。"
    )
