"""workbench 稳定性评测（v0.4）——把批量 run 的预测 ΔΔG(REU) 与任务匹配的
实验稳定性基准（ΔΔG kcal/mol）做链感知匹配与可追踪评测。

方案：docs/evaluation_plan.md（§1-7）。要点：
  - 匹配 key = (pdb_id, chain, resnum_pdb, wt, mut) 五元组，链必须一致
  - 每个样本的匹配/排除逐条记录（机器可读 JSON + 人可读报告）
  - n < 最小样本数：不算相关系数，明示"样本不足"
  - 永不输出"强/中/弱可信度"或"评测通过"
  - 基准文件缺失 → 输出「未评测」与接入步骤（诚实默认）
  - schema 不符的数据集（如误传结合亲和力表）→ 整体拒绝评测

用法：
  python -m workbench.evaluate --benchmark bench.csv --runs runs/A runs/B [runs ...]
  python -m workbench.evaluate --runs runs            # 未接入基准时：输出「未评测」报告
"""

import argparse
import csv
import datetime
import hashlib
import json
import os
import sys

from .stats import mae, pearson, spearman

REQUIRED_COLS = ["pdb_id", "chain", "resnum_pdb", "wt_aa1", "mut_aa1", "exp_ddg_kcal_mol"]
OPTIONAL_COLS = ["icode", "ph", "temp_c", "source", "reference"]
MIN_SAMPLES_DEFAULT = 20
EXIT_OK, EXIT_REFUSE = 0, 2


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class SchemaError(ValueError):
    """数据集 schema 与本任务不匹配（如误传结合亲和力数据）"""


# ==========================================
# 基准数据加载与 schema 校验
# ==========================================
def load_benchmark(path: str) -> "tuple[list, dict]":
    """加载并校验基准 CSV。返回 (rows, info)。

    rows: [{pdb_id, chain, resnum_pdb:int, wt_aa1, mut_aa1, exp_ddg_kcal_mol:float,
            ph, temp_c, source, reference, line_no}]
    schema 不符抛 SchemaError——结构上杜绝"跑通即通过"。
    """
    if not os.path.exists(path):
        raise FileNotFoundError(path)
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        head = f.read(4096)

    delim = ";" if ";" in head.splitlines()[0] else ("\t" if "\t" in head.splitlines()[0] else ",")
    rows, bad = [], []
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        reader = csv.DictReader(f, delimiter=delim)
        cols = [c.strip() for c in (reader.fieldnames or [])]
        missing = [c for c in REQUIRED_COLS if c not in cols]
        if missing:
            raise SchemaError(
                f"数据集缺少必要列 {missing}——与本任务（单体稳定性 ΔΔG）不匹配，拒绝评测。"
                f"需要列：{REQUIRED_COLS}。若这是结合亲和力数据（如 SKEMPI/PDBbind 原始表），"
                "它不适用于本科学问题。"
            )
        for i, raw in enumerate(reader, start=2):
            row = {k: (raw.get(k) or "").strip() for k in REQUIRED_COLS + OPTIONAL_COLS}
            if not any(row.values()):
                continue
            try:
                row["resnum_pdb"] = int(row["resnum_pdb"])
                row["exp_ddg_kcal_mol"] = float(row["exp_ddg_kcal_mol"])
            except (ValueError, TypeError):
                bad.append({"line_no": i, "reason": "resnum/exp_ddg 数值解析失败",
                            "raw": {k: raw.get(k) for k in REQUIRED_COLS}})
                continue
            row["pdb_id"] = row["pdb_id"].upper()
            row["chain"] = row["chain"].upper()
            row["wt_aa1"] = row["wt_aa1"].upper()
            row["mut_aa1"] = row["mut_aa1"].upper()
            row["line_no"] = i
            rows.append(row)

    info = {
        "path": os.path.abspath(path),
        "sha256": _sha256(path),
        "rows_total": len(rows) + len(bad),
        "rows_loaded": len(rows),
        "rows_skipped": bad,
        "columns_detected": REQUIRED_COLS + [c for c in OPTIONAL_COLS if c in cols],
    }
    return rows, info


# ==========================================
# run 结果收集
# ==========================================
def collect_samples(run_paths: "list[str]") -> "tuple[list, list]":
    """从 run 目录收集成功样本。返回 (samples, excluded)。

    sample = {run_dir, pdb_id, chain, resnum_pdb, wt_aa1, mut_aa1, ddg_reu}
    """
    samples, excluded = [], []
    for p in run_paths:
        candidates = []
        if os.path.exists(os.path.join(p, "manifest.json")):
            candidates = [p]
        else:
            for child in sorted(os.listdir(p)) if os.path.isdir(p) else []:
                cp = os.path.join(p, child)
                if os.path.exists(os.path.join(cp, "manifest.json")):
                    candidates.append(cp)
        for run_dir in candidates:
            try:
                with open(os.path.join(run_dir, "manifest.json"), "r", encoding="utf-8") as f:
                    m = json.load(f)
            except (OSError, json.JSONDecodeError) as e:
                excluded.append({"run_dir": run_dir, "reason": f"manifest 不可读: {e}"})
                continue
            task, result = m.get("task") or {}, m.get("result") or {}
            if m.get("status") != "succeeded":
                excluded.append({"run_dir": run_dir, "reason": f"状态 {m.get('status')} ≠ succeeded"})
                continue
            if result.get("label_check") != "passed":
                excluded.append({"run_dir": run_dir, "reason": "标签核对未通过或缺失"})
                continue
            ddg = result.get("ddg_reu")
            if not isinstance(ddg, (int, float)):
                excluded.append({"run_dir": run_dir, "reason": "缺 ddg_reu 数值"})
                continue
            stem = os.path.splitext(os.path.basename(task.get("protein_path", "")))[0]
            pdb_id = "".join(ch for ch in stem.upper() if ch.isalnum())[:4]
            samples.append({
                "run_dir": run_dir,
                "pdb_id": pdb_id,
                "chain": (task.get("chain") or "").upper(),
                "resnum_pdb": task.get("resnum_pdb"),
                "wt_aa1": (task.get("wt_aa") or "").upper(),
                "mut_aa1": (task.get("mut_aa") or "").upper(),
                "ddg_reu": float(ddg),
            })
    return samples, excluded


# ==========================================
# 链感知匹配（§3）
# ==========================================
def match_samples(samples: list, rows: list) -> "tuple[list, list]":
    """五元组精确匹配。返回 (matched, exclusions)。

    matched: [{key, ddg_reu(均值), n_predictions, exp_ddg_kcal_mol(均值), exp_n_measurements, ...}]
    - 同一突变的多次独立 run（如重跑/恢复重算）→ 预测取均值并记录 n_predictions
    - 同 key 多条实验测量 → 取均值并记录 exp_n_measurements
    - wt 不一致/缺测量 → 排除并记录原因
    """
    by_key = {}
    for r in rows:
        by_key.setdefault((r["pdb_id"], r["chain"], r["resnum_pdb"], r["wt_aa1"], r["mut_aa1"]),
                          []).append(r)

    exclusions = []
    pred_by_key = {}
    for s in samples:
        key = (s["pdb_id"], s["chain"], s["resnum_pdb"], s["wt_aa1"], s["mut_aa1"])
        if key not in by_key:
            exclusions.append({**s, "key": key, "reason": "基准中无该五元组（含链）记录"})
            continue
        pred_by_key.setdefault(key, []).append(s)

    matched = []
    for key, ss in pred_by_key.items():
        exp_vals = [h["exp_ddg_kcal_mol"] for h in by_key[key]]
        ddgs = [s["ddg_reu"] for s in ss]
        rep = dict(ss[0])
        matched.append({
            **rep,
            "key": list(key),
            "ddg_reu": sum(ddgs) / len(ddgs),
            "n_predictions": len(ddgs),
            "exp_ddg_kcal_mol": sum(exp_vals) / len(exp_vals),
            "exp_n_measurements": len(exp_vals),
            "exp_sources": sorted({h.get("source", "") for h in by_key[key] if h.get("source")}),
        })
    matched.sort(key=lambda m: m["key"])
    return matched, exclusions


# ==========================================
# 指标（§5：n 不足不算；永不评级）
# ==========================================
def compute_metrics(matched: list, min_samples: int) -> "tuple[dict | None, list]":
    pred = [s["ddg_reu"] for s in matched]
    exp = [s["exp_ddg_kcal_mol"] for s in matched]
    if len(matched) < min_samples:
        return None, [f"样本数 n={len(matched)} < 最小要求 {min_samples}——不计算指标（§5）"]
    r, _ = pearson(pred, exp)
    rho, _ = spearman(pred, exp)
    metrics = {
        "n": len(matched),
        "pearson_r": round(r, 4),
        "spearman_rho": round(rho, 4),
        "mae_cross_unit_ref_only": round(mae(pred, exp), 4),
        "note": "预测单位 REU、实验单位 kcal/mol，MAE 跨单位仅作相对参考；"
                "不提供可信度评级，解释由研究者负责。",
    }
    return metrics, []


def per_pdb_breakdown(matched: list, min_group: int = 8) -> dict:
    """按结构分组的相关性（诊断用：定位负相关是结构特有还是全局系统性）。"""
    groups = {}
    for s in matched:
        groups.setdefault(s["pdb_id"], []).append(s)
    out = {}
    for pdb in sorted(groups):
        ss = groups[pdb]
        if len(ss) < min_group:
            continue
        r, _ = pearson([x["ddg_reu"] for x in ss], [x["exp_ddg_kcal_mol"] for x in ss])
        rho, _ = spearman([x["ddg_reu"] for x in ss], [x["exp_ddg_kcal_mol"] for x in ss])
        out[pdb] = {"n": len(ss), "pearson_r": round(r, 4), "spearman_rho": round(rho, 4)}
    return out


# ==========================================
# 报告
# ==========================================
def render_report(dataset_info, samples, excluded, matched, metrics, notices, min_samples,
                  breakdown=None) -> str:
    lines = [
        "=" * 64,
        "稳定性评测报告（Protein Design Workbench v0.4）",
        "=" * 64,
        "",
        "── 口径 ──",
        "科学问题: cartesian_ddg 稳定性 ΔΔG(REU) vs 实验稳定性 ΔΔG(kcal/mol) 的趋势一致性",
        "匹配规则: (pdb_id, chain, resnum_pdb, wt, mut) 五元组, 链必须一致",
        "声明    : 计算筛选 ≠ 实验验证; 不提供可信度评级",
        "",
    ]
    if dataset_info is None:
        lines += [
            "状态: 【未评测】—— 未接入任务匹配的稳定性基准数据。",
            "",
            "接入步骤（详见 docs/evaluation_plan.md §2）:",
            "  1. 获取任务匹配的实验稳定性数据集（候选: ProThermDB / ThermoMutDB / S669 等）",
            "  2. 整理为 CSV, 必需列: " + ", ".join(REQUIRED_COLS),
            "  3. python -m workbench.evaluate --benchmark <csv> --runs runs/...",
            "  4. 在 docs/data_provenance.md 登记来源/版本/下载日期/许可",
            "",
            f"当前已就绪的 run 样本: {len(samples)} 个（其中 {len(excluded)} 个不可用已列明）",
        ]
        if excluded:
            lines.append("")
            lines.append("── 不可用 run 及原因 ──")
            for e in excluded:
                lines.append(f"  {os.path.basename(e['run_dir'])}: {e['reason']}")
        return "\n".join(lines)

    lines += [
        f"数据集: {dataset_info['path']}",
        f"  sha256: {dataset_info['sha256'][:16]}… | 载入 {dataset_info['rows_loaded']} 行, "
        f"跳过 {len(dataset_info['rows_skipped'])} 行",
        "",
        f"── 样本统计 ──",
        f"run 样本: {len(samples)} | 匹配成功: {len(matched)} | 排除: {len(excluded)}",
        "",
    ]
    if metrics is None:
        lines += ["状态: 【未评测】—— " + "; ".join(notices)]
    else:
        lines += ["状态: 【已评测】（数值如下, 无评级）", ""]
        lines += [
            f"  Pearson r   = {metrics['pearson_r']}",
            f"  Spearman ρ  = {metrics['spearman_rho']}",
            f"  MAE(跨单位) = {metrics['mae_cross_unit_ref_only']}  (REU vs kcal/mol, 仅相对参考)",
            f"  n           = {metrics['n']}",
        ]
        if breakdown:
            lines += ["", "── 按结构分组（n≥8；定位负相关是结构特有还是系统性）──"]
            for pdb, b in breakdown.items():
                lines.append(
                    f"  {pdb}: n={b['n']}  r={b['pearson_r']:+.3f}  ρ={b['spearman_rho']:+.3f}"
                )
    lines += ["", "── 逐样本明细（匹配与排除逐条可追踪, 详见 eval_details.json）──"]
    for s in matched:
        lines.append(
            f"  ✓ {s['pdb_id']} {s['chain']}:{s['resnum_pdb']}{s['wt_aa1']}→{s['mut_aa1']}  "
            f"预测 {s['ddg_reu']:+.3f} REU vs 实验 {s['exp_ddg_kcal_mol']:+.3f} kcal/mol"
            f"（{s['exp_n_measurements']} 条测量取均值）"
        )
    for e in excluded:
        reason = e.get("reason", "")
        key = e.get("key", "")
        lines.append(f"  ✗ {os.path.basename(e['run_dir'])} {key}: {reason}" if key
                     else f"  ✗ {os.path.basename(e['run_dir'])}: {reason}")
    return "\n".join(lines)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="workbench.evaluate",
                                description="稳定性评测（链感知匹配; 无评级; 缺数据时「未评测」）")
    p.add_argument("--benchmark", default="", help="基准 CSV（缺省=未评测模式, 只收集样本）")
    p.add_argument("--runs", nargs="+", required=True, help="run 目录或包含多个 run 的父目录")
    p.add_argument("--workdir", default="runs", help="评测输出根目录")
    p.add_argument("--min-samples", type=int, default=MIN_SAMPLES_DEFAULT)
    args = p.parse_args(argv)

    dataset_info = None
    rows = []
    notices = []
    if args.benchmark:
        try:
            rows, dataset_info = load_benchmark(args.benchmark)
        except FileNotFoundError:
            print(f"[workbench.evaluate] 基准文件不存在: {args.benchmark} → 按「未评测」输出", file=sys.stderr)
            notices.append("指定的基准文件不存在")
        except SchemaError as e:
            print(f"[workbench.evaluate] ❌ {e}", file=sys.stderr)
            return EXIT_REFUSE

    samples, excluded = collect_samples(args.runs)
    matched = []
    breakdown = {}
    if rows:
        matched, excluded_extra = match_samples(samples, rows)
        excluded += excluded_extra
        metrics, notices_metrics = compute_metrics(matched, args.min_samples)
        notices += notices_metrics
        breakdown = per_pdb_breakdown(matched) if metrics else {}
    else:
        metrics = None

    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    eval_dir = _ensure(os.path.join(args.workdir, f"eval_{ts}"))

    details = {
        "schema": "workbench-eval/0.4",
        "generated_time": datetime.datetime.now().isoformat(timespec="seconds"),
        "evaluation_plan": "docs/evaluation_plan.md",
        "dataset": dataset_info,
        "min_samples": args.min_samples,
        "summary": {
            "runs_collected": len(samples),
            "matched": len(matched),
            "excluded": len(excluded),
            "status": ("未评测" if metrics is None else "已评测"),
            "metrics": metrics,
            "per_pdb_breakdown": breakdown,
        },
        "samples": samples,
        "matched": matched,
        "excluded": excluded,
        "notices": notices,
    }
    with open(os.path.join(eval_dir, "eval_details.json"), "w", encoding="utf-8") as f:
        json.dump(details, f, ensure_ascii=False, indent=2)

    report = render_report(dataset_info, samples, excluded, matched, metrics, notices,
                           args.min_samples, breakdown)
    with open(os.path.join(eval_dir, "report.txt"), "w", encoding="utf-8") as f:
        f.write(report + "\n")
    print(report)
    print(f"\n[workbench.evaluate] 输出: {eval_dir}/report.txt | eval_details.json")
    return EXIT_OK


def _ensure(path):
    os.makedirs(path, exist_ok=True)
    return path


if __name__ == "__main__":
    sys.exit(main())
