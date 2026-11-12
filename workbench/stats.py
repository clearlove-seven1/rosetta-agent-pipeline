"""workbench 统计原语（v0.4）：纯 Python 实现，零第三方依赖。

与 rosetta_tools._pearson/_spearman 同算法，独立实现以保持 workbench 无 langchain 依赖。
"""

import math


def pearson(x, y) -> "tuple[float, int]":
    """Pearson 相关系数。返回 (r, n)；n<2 或零方差返回 (nan, n)。"""
    n = len(x)
    if n < 2:
        return float("nan"), n
    mx, my = sum(x) / n, sum(y) / n
    cov = sum((xi - mx) * (yi - my) for xi, yi in zip(x, y))
    vx = sum((xi - mx) ** 2 for xi in x)
    vy = sum((yi - my) ** 2 for yi in y)
    if vx == 0 or vy == 0:
        return float("nan"), n
    return cov / math.sqrt(vx * vy), n


def _ranks(data) -> list:
    """平均秩（并列取平均），1 起始。"""
    order = sorted(range(len(data)), key=lambda i: data[i])
    ranks = [0.0] * len(data)
    i = 0
    while i < len(data):
        j = i
        while j + 1 < len(data) and data[order[j + 1]] == data[order[i]]:
            j += 1
        avg = (i + j) / 2 + 1
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def spearman(x, y) -> "tuple[float, int]":
    """Spearman 秩相关。返回 (rho, n)；n<3 返回 (nan, n)。"""
    n = len(x)
    if n < 3:
        return float("nan"), n
    rho, _ = pearson(_ranks(x), _ranks(y))
    return rho, n


def mae(pred, exp) -> float:
    """平均绝对误差。注意：本工具中 pred(REU) 与 exp(kcal/mol) 单位不同，仅作相对参考。"""
    n = len(pred)
    if n == 0:
        return float("nan")
    return sum(abs(p - e) for p, e in zip(pred, exp)) / n
