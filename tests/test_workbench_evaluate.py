"""workbench 稳定性评测单元测试（纯逻辑 + 合成数据，不依赖 Rosetta / 网络）

运行：python tests/test_workbench_evaluate.py
覆盖：链感知匹配（同编号不同链不合并）、五元组不匹配排除、重复测量取均值、
     n 不足不算指标、完美线性数据相关性=1、错误 schema 整体拒绝、未评测模式。
"""

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from workbench.evaluate import (SchemaError, collect_samples, compute_metrics,
                                load_benchmark, match_samples)
from workbench.stats import mae, pearson, spearman

RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond)))
    print(f"{'✅ PASS' if cond else '❌ FAIL'}  {name}" + (f"  | {detail}" if detail and not cond else ""))


tmp = tempfile.mkdtemp(prefix="wb_eval_")

# ---------- stats 原语 ----------
r, n = pearson([1, 2, 3, 4, 5], [2, 4, 6, 8, 10])
check("stats: 完美线性 Pearson=1", abs(r - 1.0) < 1e-9)
rho, _ = spearman([3, 2, 1], [10, 20, 30])
check("stats: Spearman 反序=-1", abs(rho + 1.0) < 1e-9)
check("stats: MAE", abs(mae([1, 2], [2, 4]) - 1.5) < 1e-9)

# ---------- 构造合成 run ----------
def make_run(parent, name, pdb_stem, chain, resnum, wt, mut, ddg, status="succeeded", label="passed"):
    d = os.path.join(parent, name)
    os.makedirs(d, exist_ok=True)
    m = {
        "run_id": name, "status": status,
        "task": {"protein_path": f"/x/{pdb_stem}.pdb", "chain": chain,
                 "resnum_pdb": resnum, "wt_aa": wt, "mut_aa": mut,
                 "seq_index": resnum},
        "result": {"ddg_reu": ddg, "label_check": label, "units": "REU"},
        "steps": {},
    }
    with open(os.path.join(d, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(m, f)
    return d

runs_dir = os.path.join(tmp, "runs")
ra = make_run(runs_dir, "r1", "1ubq", "A", 30, "K", "E", 2.0)        # 链A 30
rb = make_run(runs_dir, "r2", "1ubq", "B", 30, "R", "A", -1.0)       # 链B 30（同编号不同链）
rc = make_run(runs_dir, "r3", "1mc9", "A", 29, "Q", "P", 0.5)
rd = make_run(runs_dir, "r4", "1mc9", "A", 99, "A", "V", 9.9, status="failed")  # 应被排除
re_ = make_run(runs_dir, "r5", "1ubq", "A", 76, "G", "E", 1.1, label="failed")  # 应被排除

samples, excluded = collect_samples([runs_dir])
check("collect: 成功样本 3 个", len(samples) == 3, f"{len(samples)}")
check("collect: 排除 2 个（failed/label）", len(excluded) == 2, str([e["reason"] for e in excluded]))

# ---------- 基准 CSV ----------
bench = os.path.join(tmp, "bench.csv")
with open(bench, "w", encoding="utf-8") as f:
    f.write("pdb_id,chain,resnum_pdb,wt_aa1,mut_aa1,exp_ddg_kcal_mol,source\n")
    f.write("1UBQ,A,30,K,E,1.5,S669\n")       # 匹配 r1
    f.write("1UBQ,B,30,R,A,-0.8,S669\n")     # 匹配 r2（同编号不同链→不得串）
    f.write("1MC9,A,29,Q,P,-0.4,ThermoMutDB\n")  # 匹配 r3
    f.write("1MC9,A,29,Q,P,-0.6,ThermoMutDB\n")  # 同 key 重复测量→均值 -0.5
    f.write("1UBQ,A,30,L,E,9.9,S669\n")      # wt 不同（L≠K）→ 不得匹配 r1

rows, info = load_benchmark(bench)
check("load: 5 行载入", len(rows) == 5 and info["rows_loaded"] == 5)

matched, excluded2 = match_samples(samples, rows)
check("match: 3 个样本全部链感知匹配", len(matched) == 3, str([m["key"] for m in matched]))
by_mut = {m["mut_aa1"] + str(m["resnum_pdb"]) + m["chain"]: m for m in matched}
check("match: 链A30 得 K→E 行 (1.5)", abs(by_mut["E30A"]["exp_ddg_kcal_mol"] - 1.5) < 1e-9)
check("match: 链B30 得 R→A 行 (-0.8)", abs(by_mut["A30B"]["exp_ddg_kcal_mol"] + 0.8) < 1e-9)
check("match: 链碰撞未合并（A30≠B30）",
      by_mut["E30A"]["exp_ddg_kcal_mol"] != by_mut["A30B"]["exp_ddg_kcal_mol"])
check("match: 重复测量取均值 (2条)", by_mut["P29A"]["exp_n_measurements"] == 2
      and abs(by_mut["P29A"]["exp_ddg_kcal_mol"] + 0.5) < 1e-9)

# ---------- 指标门槛 ----------
metrics, notices = compute_metrics(matched, min_samples=20)
check("指标: n=3<20 → 不计算", metrics is None and any("不计算指标" in x for x in notices))

# 构造 25 个完美线性样本 → r=1, rho=1
big = []
for i in range(25):
    big.append({"run_dir": f"/x/{i}", "pdb_id": "1UBQ", "chain": "A", "resnum_pdb": i + 1,
                "wt_aa1": "A", "mut_aa1": "V", "ddg_reu": float(i),
                "key": ["1UBQ", "A", i + 1, "A", "V"],
                "exp_ddg_kcal_mol": float(i) / 2.0, "exp_n_measurements": 1, "exp_sources": []})
metrics2, notices2 = compute_metrics(big, min_samples=20)
check("指标: n=25 完美线性 → r=1", metrics2 and abs(metrics2["pearson_r"] - 1.0) < 1e-6, str(metrics2))
# MAE: pred-exp = i/2, 均值 = sum(0,0.5,...,12)/25 = 150/25 = 6.0
check("指标: ρ=1, MAE 跨单位=6.0", metrics2 and abs(metrics2["spearman_rho"] - 1.0) < 1e-6
      and abs(metrics2["mae_cross_unit_ref_only"] - 6.0) < 1e-9, str(metrics2))

# ---------- 错误 schema 整体拒绝（模拟误传 SKEMPI 表）----------
skempi_like = os.path.join(tmp, "skempi_like.csv")
with open(skempi_like, "w", encoding="utf-8") as f:
    f.write("#Pdb;Mutation(s)_cleaned;Affinity_wt_parsed\n")
    f.write("1UBQ;DA63B;1e-7\n")
try:
    load_benchmark(skempi_like)
    check("schema: SKEMPI 型表被拒绝", False)
except SchemaError:
    check("schema: SKEMPI 型表被拒绝", True)

# ---------- 未评测模式 ----------
v_none = validate_none = None  # 占位避免误用
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# 直接调用 render_report 的未评测分支逻辑（经 CLI 太重，这里测数据流）
from workbench.evaluate import render_report
report = render_report(None, samples, excluded, [], None, [], 20)
check("未评测: 报告含「未评测」与接入步骤", "未评测" in report and "接入步骤" in report)

# ---------- 汇总 ----------
fails = [n2 for n2, ok in RESULTS if not ok]
print("\n========== 结果汇总 ==========")
print(f"通过 {len(RESULTS) - len(fails)} / {len(RESULTS)}")
sys.exit(1 if fails else 0)
