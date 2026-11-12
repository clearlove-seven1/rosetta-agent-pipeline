"""Protein Design Workbench — 确定性 Rosetta 单点突变稳定性计算（v0.2 MVP）

口径（必须与 README 第 0 节一致）：
  单体蛋白 · 指定单点突变 · cartesian_ddg 常规稳定性模式 · 输出 ΔΔG (REU)。
  本包不调用任何 LLM / 网络 API；计算筛选 ≠ 实验验证。
"""

__version__ = "0.2.0a0"

SCOPE = (
    "任务类型: 单体蛋白指定单点突变稳定性计算\n"
    "Protocol: relax (cartesian, ref2015_cart) → cartesian_ddg (常规模式, 非界面模式)\n"
    "输出量: ΔΔG = avg(MUT) - avg(WT)，单位 REU（Rosetta Energy Units，非 kcal/mol）\n"
    "方向约定: ΔΔG > 0 倾向去稳定化；ΔΔG < 0 倾向稳定化\n"
    "边界: 本结果是计算筛选参考，不等于实验验证，不能外推为结合亲和力"
)
