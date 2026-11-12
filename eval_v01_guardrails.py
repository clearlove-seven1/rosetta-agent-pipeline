"""
eval_v01_guardrails.py — v0.1 科学纠偏的针对性验证
================================================

沿用 eval_context_changes.py 的"纯逻辑 + 实数据"风格，证明 v0.1 行为改变真实生效。
不调 LLM、不跑 Rosetta 计算、不修改任何原始数据；临时文件写在系统临时目录。

验证点：
  1. 默认（不开 WB_EXPLORATORY_EVAL_ENABLED）SKEMPI 对照输出「未评测」，
     不出现相关系数与任何可信度评级
  2. 显式开启后输出「探索性 Pearson」，且仍无"可信度高"式结论
  3. 默认 PDBbind 对照输出「未评测」，不出现 Spearman 与强/中/弱评级
  4. 显式开启后输出「探索性 Spearman」，且仍无评级
  5. 单位口径：实验 ΔΔG 详情不再误标 REU；输出含 kcal/mol 口径说明
  6. predict_disorder 输出改为「探索性信号」措辞，不再有"预测可信度低"式断言
     （强制走 B-factor 启发式回退路径，无需 torch/网络）
  7. rosetta_agent.py 源码：disorder 节点默认关闭（未评估）+ summarize 口径声明在位

运行（rosetta-agent conda env，仓库根目录）：
  python eval_v01_guardrails.py
"""

import math
import os
import re
import sys
import tempfile
from collections import defaultdict
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent
os.chdir(PROJECT_ROOT)

# 确保被测对象处于默认（关闭）状态
os.environ.pop("WB_EXPLORATORY_EVAL_ENABLED", None)
os.environ.pop("WB_DISORDER_FILTER_ENABLED", None)

import rosetta_tools as rt  # noqa: E402  (依赖 rosetta-agent env 中已装的 langchain)

RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond), detail))
    print(f"{'✅ PASS' if cond else '❌ FAIL'}  {name}" + (f"  | {detail}" if detail and not cond else ""))


# ---------------------------------------------------------------
# 0. 开关默认值
# ---------------------------------------------------------------
check("默认 exploratory_eval_enabled()=False", rt.exploratory_eval_enabled() is False)
check("默认 disorder_filter_enabled()=False", rt.disorder_filter_enabled() is False)

# ---------------------------------------------------------------
# 1-2. SKEMPI 对照：默认未评测 / 开启后探索性
# ---------------------------------------------------------------
skempi = rt._load_skempi()
R_KCAL = 1.987e-3  # kcal/(mol·K)，与被测函数一致
cands = defaultdict(list)
if skempi:
    for entry in skempi:
        pdb_field = (entry.get('#Pdb') or entry.get('#Pdb id') or entry.get('Pdb id')
                     or entry.get('PDB id') or '').strip().upper()
        mut_field = (entry.get('Mutation(s)_cleaned') or entry.get('Mutation(s)_PDB')
                     or entry.get('Mutation(s)') or '').strip()
        if not pdb_field or not mut_field or ',' in mut_field:
            continue  # 只取单突变条目，构造可映射样本
        ddg = None
        for cand in ('ddG_exp_kcal_mol', 'DDG(kcal/mol)', 'ΔΔG', 'ddG', 'ddG_exp'):
            v = entry.get(cand)
            if v:
                try:
                    ddg = float(v)
                    break
                except (ValueError, TypeError):
                    pass
        if ddg is None:
            try:
                kw = float(entry.get('Affinity_wt_parsed') or entry.get('Affinity_wt (M)'))
                km = float(entry.get('Affinity_mut_parsed') or entry.get('Affinity_mut (M)'))
                T = float(entry.get('Temperature') or 298.0)
                if kw > 0 and km > 0:
                    ddg = R_KCAL * T * math.log(km / kw)
            except (ValueError, TypeError):
                ddg = None
        if ddg is None:
            continue
        p = rt._parse_skempi_mutation(mut_field)
        if p:
            cands[pdb_field].append((rt._skempi_key_from_parsed(p), ddg))

if cands:
    pdb_pick = max(cands, key=lambda k: len(cands[k]))
    pairs = cands[pdb_pick][:5]  # 取 ≤5 个位点
    tmpdir = Path(tempfile.mkdtemp(prefix="wb_v01_"))
    ddg_file = tmpdir / "ddg_results.txt"
    with open(ddg_file, "w", encoding="utf-8") as f:
        f.write("突变\t平均ΔΔG(REU)\n")
        for i, (key, ddg) in enumerate(pairs):
            wt, pos, mut = key
            # 预测值 = 实验 + 偏移（构造不同数字，避免巧合相等）
            f.write(f"{wt}_A{pos}_{mut}\t{ddg + 0.5 + i * 0.1:.4f}\n")

    out_default = rt.evaluate_against_skempi.func(pdb_id=pdb_pick, ddg_results_path=str(ddg_file))
    check("SKEMPI 默认输出「未评测」", "状态：未评测" in out_default, out_default[:200])
    check("SKEMPI 默认不输出 Pearson", "Pearson r =" not in out_default)
    check("SKEMPI 默认无可信度评级", "可信度高" not in out_default and "相关性强" not in out_default)
    # 数据行不得再把实验值标成 REU（格式如 "D_A63_B: -1.23  vs  0.45 REU"）
    check("SKEMPI 实验值不再误标 REU（含 kcal/mol 口径）",
          not re.search(r"\d\.\d{2}\s+REU", out_default.split("位点详情")[-1]) and "kcal/mol" in out_default,
          out_default.split("位点详情")[-1][:200])

    os.environ["WB_EXPLORATORY_EVAL_ENABLED"] = "1"
    out_on = rt.evaluate_against_skempi.func(pdb_id=pdb_pick, ddg_results_path=str(ddg_file))
    os.environ.pop("WB_EXPLORATORY_EVAL_ENABLED", None)
    check("SKEMPI 开启后输出探索性 Pearson", "探索性 Pearson r =" in out_on)
    check("SKEMPI 开启后仍无评级断言", "可信度高" not in out_on and "相关性强" not in out_on)
    check("SKEMPI 开启后注明局限", "不构成" in out_on)
else:
    print("⚠️ SKIP  SKEMPI 行为测试：CSV 中未找到可解析的单突变 ddG 样本")

# ---------------------------------------------------------------
# 3-4. PDBbind 对照：默认未评测 / 开启后探索性
# ---------------------------------------------------------------
pdbbind = rt._load_pdbbind()
picks = []
if pdbbind:
    seen = set()
    for entry in pdbbind:
        pid = (entry.get('pdb_id') or entry.get('PDB') or entry.get('#Pdb')
               or entry.get('PDB ID') or '').strip().upper()
        aff, aff_type = rt._extract_pdbbind_affinity(entry)
        if pid and aff is not None and pid not in seen:
            seen.add(pid)
            picks.append((pid, aff))
        if len(picks) == 3:
            break

if len(picks) == 3:
    tmpdir = Path(tempfile.mkdtemp(prefix="wb_v01_"))
    sc_paths = []
    for i, (_pid, _aff) in enumerate(picks, 1):
        sc = tmpdir / f"score_{i}.sc"
        sc.write_text(
            "SCORE:     total_score description\n"
            f"SCORE:    -{100 + i * 7}.50    mock_{i}.pdb\n",
            encoding="utf-8",
        )
        sc_paths.append(str(sc))

    ids = ",".join(p[0] for p in picks)
    paths = ";".join(sc_paths)
    out_default = rt.evaluate_against_pdbbind.func(pdb_ids=ids, score_paths=paths)
    check("PDBbind 默认输出「未评测」", "状态：未评测" in out_default, out_default[:200])
    # 不输出的是计算出的 ρ 值；未评测说明文字里允许出现 "Spearman" 一词
    check("PDBbind 默认不输出 Spearman 计算值", "Spearman ρ =" not in out_default)
    check("PDBbind 默认无强/中/弱评级", "强负相关" not in out_default and "中等负相关" not in out_default)
    check("PDBbind 含 total_score 口径说明", "不是结合自由能" in out_default)

    os.environ["WB_EXPLORATORY_EVAL_ENABLED"] = "1"
    out_on = rt.evaluate_against_pdbbind.func(pdb_ids=ids, score_paths=paths)
    os.environ.pop("WB_EXPLORATORY_EVAL_ENABLED", None)
    check("PDBbind 开启后输出探索性 Spearman", "探索性 Spearman" in out_on)
    check("PDBbind 开启后仍无评级", "强负相关" not in out_on and "中等负相关" not in out_on and "初筛" not in out_on)
else:
    print("⚠️ SKIP  PDBbind 行为测试：CSV 中未凑齐 3 个带亲和力的 PDB")

# ---------------------------------------------------------------
# 5-6. disorder 工具措辞（强制启发式回退，不依赖 torch/网络）
# ---------------------------------------------------------------
real_pdb = PROJECT_ROOT / "output" / "task_20260910_1004_best_relaxed_99170" / "protein.pdb"
if real_pdb.exists():
    orig = rt._predict_disorder_esm2

    def _force_fallback(_seq, _res):
        raise ImportError("test: 强制回退 B-factor 启发式（不依赖 torch）")

    rt._predict_disorder_esm2 = _force_fallback
    try:
        report = rt.predict_disorder.func(pdb_path=str(real_pdb))
    finally:
        rt._predict_disorder_esm2 = orig
    check("disorder 报告改为「探索性信号」措辞", "【探索性信号】" in report, report[:200])
    check("disorder 报告不再断言「预测可信度低」", "预测可信度低" not in report)
    check("disorder 报告声明未经验证", "未经验证" in report)
else:
    print("⚠️ SKIP  disorder 措辞测试：找不到样例 protein.pdb")

# ---------------------------------------------------------------
# 7. 源码级断言（agent 侧护栏在位）
# ---------------------------------------------------------------
agent_src = (PROJECT_ROOT / "rosetta_agent.py").read_text(encoding="utf-8")
tools_src = (PROJECT_ROOT / "rosetta_tools.py").read_text(encoding="utf-8")

check("agent: disorder 节点默认关闭护栏", "WB_DISORDER_FILTER_ENABLED" in agent_src and "状态：未评估（默认关闭）" in agent_src)
check("agent: 关闭时 disordered_sites 恒为 []", '"disordered_sites": [],' in agent_src)
check("agent: summarize 含口径声明", "不等于实验验证" in agent_src)
check("agent: binding 报告含口径声明头", "【口径声明 v0.1】" in agent_src)
check("tools: 旧 SKEMPI 评级分支已移除", "本批次 Rosetta 预测可信度高" not in tools_src)
check("tools: 旧 PDBbind 评级分支已移除", "✅ 强负相关" not in tools_src and "可作初筛" not in tools_src)
check("tools: 旧 disorder 提示已移除", "预测可信度低" not in tools_src)
check("tools: 旧实验值 REU 误标已移除", 'vs  {experimental_avg[k]:+.2f}  REU' not in tools_src)

# ---------------------------------------------------------------
failures = [name for name, ok, _ in RESULTS if not ok]
print("\n========== 结果汇总 ==========")
print(f"通过 {len(RESULTS) - len(failures)} / {len(RESULTS)}")
if failures:
    print("失败项：", failures)
    sys.exit(1)
print("全部通过 ✅")
