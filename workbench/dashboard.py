"""Workbench 只读网页面板（v0.5-04 可视化入口）

用法（仓库根目录）：
  python -m workbench.dashboard [--port 7860] [--host 0.0.0.0]

只读：扫描 runs/（Rosetta / MD / MMPBSA manifest）、评测结果与 docs/ 图表并展示；
不触发任何计算、不修改任何文件。数据均为计算参考值，边界声明见各报告。
"""

import argparse
import glob
import json
import os
import sys
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

DISCLAIMER = (
    "**计算筛选 ≠ 实验验证。** 所有数值均为计算参考值；MD/ML 分析不代表动力学收敛，"
    "不得用于筛选/评分/亲和力判断；评测指标受样本量与协议限制。详见 docs/ 各报告。"
)


def _load(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def collect_runs(runs_root=None):
    """扫描全部 manifest（纯函数，供 CLI 汇总与测试使用）。

    注意递归：Rosetta run 在 runs/<id>/，而 MD/MMPBSA 在 runs/gromacs/<id>/、
    runs/mmpbsa/<id>/（深度 2）——必须 ** 递归 glob（v0.5-04 实测教训）。
    """
    runs_root = runs_root or os.path.join(REPO_ROOT, "runs")
    rosetta, md, mmpbsa, other = [], [], [], 0
    for mpath in sorted(glob.glob(os.path.join(runs_root, "**", "manifest.json"),
                                  recursive=True)):
        m = _load(mpath)
        if not m:
            continue
        run_id = m.get("run_id", os.path.basename(os.path.dirname(mpath)))
        status = m.get("status", "?")
        task = m.get("task") or {}
        calc = str(task.get("calc_type", ""))
        result = m.get("result") or {}
        if "MM-GB/PB" in calc:
            sec = (result.get("sections") or {})
            k = list(sec.keys())[0] if sec else None
            mmpbsa.append([
                run_id, status, task.get("calc_type", ""),
                task.get("md_run", ""),
                f"{sec[k]['total']:.2f}±{sec[k]['sd']:.2f}" if k and sec[k].get("total") is not None else "—",
                (result.get("frame_totals") or {}).get("n_frames", "—"),
            ])
            continue
        if "MD" in calc or "GROMACS" in calc:
            perf = (m.get("gpu_evidence_mdrun_log") or {}).get("performance_ns_day")
            md.append([
                run_id, status, task.get("label", ""),
                task.get("duration_class", ""), perf,
                result.get("prod_time_range_ps"),
            ])
            continue
        mut = f"{task.get('chain','')}{task.get('resnum_pdb','')}{task.get('mut_aa1','')}"
        rosetta.append([
            run_id, status, os.path.basename(task.get("protein_path", "?")),
            mut, result.get("ddg_reu", "—"),
        ])
    for mpath in sorted(glob.glob(os.path.join(runs_root, "*", "manifest.json"))):
        m = _load(mpath)
        if m and not m.get("task", {}).get("calc_type"):
            other += 1
    return {"rosetta": rosetta, "md": md, "mmpbsa": mmpbsa, "other": other}


def collect_evals(runs_root=None):
    runs_root = runs_root or os.path.join(REPO_ROOT, "runs")
    out = []
    for d in sorted(glob.glob(os.path.join(runs_root, "eval_*"))):
        ed = _load(os.path.join(d, "eval_details.json"))
        if not ed:
            continue
        out.append({
            "dir": os.path.basename(d),
            "time": ed.get("generated_time", ""),
            "summary": ed.get("summary"),
            "n_matched": len(ed.get("matched") or []) or len(ed.get("samples") or []),
        })
    return out


def collect_figures():
    """MD 分析图与 docs 图表路径（存在且非空）。"""
    figs = []
    for p in sorted(glob.glob(os.path.join(REPO_ROOT, "docs", "*.png"))):
        figs.append((p, os.path.basename(p)))
    for p in sorted(glob.glob(os.path.join(REPO_ROOT, "runs", "gromacs", "*", "analysis",
                                           "analysis_figure.png")))[-12:]:
        figs.append((p, os.path.basename(os.path.dirname(os.path.dirname(p)))))
    return figs


def build_ui(runs_root=None):
    import gradio as gr

    data = collect_runs(runs_root)
    evals = collect_evals(runs_root)
    figs = collect_figures()

    n_ok = sum(1 for r in data["rosetta"] if r[1] == "succeeded")
    n_md = sum(1 for r in data["md"] if r[1] == "succeeded")

    with gr.Blocks(title="Protein Design Workbench") as ui:
        gr.Markdown("# Protein Design Workbench — 任务面板（只读）")
        gr.Markdown(DISCLAIMER)
        gr.Markdown(
            f"**概览**：Rosetta 任务 {len(data['rosetta'])}（succeeded {n_ok}）｜ "
            f"MD 任务 {len(data['md'])}（succeeded {n_md}）｜ MMPBSA 分析 {len(data['mmpbsa'])} ｜ "
            f"评测报告 {len(evals)} ｜ 其他 {data['other']}"
        )

        with gr.Tab("Rosetta ΔΔG 任务"):
            gr.Dataframe(
                headers=["run_id", "状态", "蛋白", "突变", "ΔΔG (REU)"],
                value=data["rosetta"], interactive=False, wrap=True)

        with gr.Tab("MD 轨迹"):
            gr.Dataframe(
                headers=["run_id", "状态", "体系", "时长档", "ns/day", "时间范围(ps)"],
                value=data["md"], interactive=False, wrap=True)

        with gr.Tab("MMPBSA（探索性）"):
            gr.Markdown(
                "**探索性工具输出**：单体系端点法能量（无熵贡献、协议未经验证）；"
                "跨体系差异不得解读为突变效应。")
            gr.Dataframe(
                headers=["run_id", "状态", "类型", "来源 MD", "TOTAL (kJ/mol)", "帧数"],
                value=data["mmpbsa"], interactive=False, wrap=True)

        with gr.Tab("评测"):
            for e in evals[-3:]:
                gr.Markdown(f"### {e['dir']}（{e['time']}，匹配样本 {e['n_matched']}）")
                gr.JSON(e["summary"])
            if not evals:
                gr.Markdown("暂无评测报告。")

        with gr.Tab("图表"):
            gr.Markdown("MD 分析图（每 run 一张：RMSD / 温度压强 / RMSF）与 docs/ 图表")
            gr.Gallery(value=figs, columns=3, height="500px", show_label=False)

        gr.Markdown(
            "---\n*面板只读生成于 "
            + time.strftime("%Y-%m-%d %H:%M:%S")
            + "；数据来源 runs/*/manifest.json 与 runs/eval_*/eval_details.json*"
        )
    return ui


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="workbench-dashboard", description="只读任务面板")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=7860)
    ap.add_argument("--runs-root", default=None)
    args = ap.parse_args(argv)
    ui = build_ui(args.runs_root)
    ui.launch(server_name=args.host, server_port=args.port, share=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
