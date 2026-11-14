"""Workbench 智能体（v0.6-01）：介绍页 + 对话式操作入口（只读查询 + 受控提交）

用法（仓库根目录）：
  python -m workbench.agent [--port 7860]

设计红线（对应 PROTEIN_WORKBENCH_PLAN §6 v0.6）：
  * LLM 只负责组织语言；**一切任务状态/结果必须来自工具返回**，查询不到就明说
    "不存在"，禁止编造 run id、状态或数值；
  * 提交类工具仅在用户明确要求执行计算时调用，且走 workbench 既有的确定性
    CLI（参数校验在 CLI 层完成，智能体不新增任何科学口径）；
  * 网页提交护栏：MD ≤ 3 ns（CLI 仍可用 --confirm-long-run 走正式流程）；
  * 拒绝任意 shell 命令——所有外部调用均为固定参数列表；
  * LLM 不可用时，CLI 与面板完全不受影响（本页只加一层自然语言入口）。
"""

import argparse
import glob
import json
import os
import re
import subprocess
import sys
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = sys.executable or "python3"

INTRO_MD = """# Protein Design Workbench — 智能体入口

**一句话**：给一个蛋白结构和想要的突变，自动计算"突变让蛋白更稳定还是更不稳定"（Rosetta cartesian_ddg），
并可运行分子动力学（GROMACS）与 MM/PBSA 端点能量分析；全部计算可追溯、可复现、失败不伪装成功。

## 两条流水线
| 流水线 | 问题 | 入口 |
|---|---|---|
| Rosetta 稳定性 ΔΔG | 突变是否去稳定化？输出 ΔΔG (REU) | `python -m workbench.cli --protein X --chain A --resnum N --to Y` |
| GROMACS MD | 蛋白/突变体在水中的动力学观察 | `python -m workbench.gromacs --protein X --chain A --label L --ns 1.0` |
| MM/PBSA（探索性） | 既有轨迹的端点能量分析 | `python -m workbench.mmpbsa --md-run <run> --calc gb` |

## 评测（v0.4 闭环）
S669 基准 · 11 结构 121 突变 · 全局 **r = +0.34 / ρ = +0.34**，与文献 cartesian_ddG 表现一致；
符号口径已与 ThermoMPNN 官方加载代码核对一致（docs/data_provenance.md §6.1）。

## 边界（必读）
**计算筛选 ≠ 实验验证。** MD/ML 分析不代表动力学收敛；不预测结合亲和力；
无实验数据时评测显示"未评测"；所有工具输出不得用于筛选/评分/论文结论，除非走完整验证方案。
"""

SYSTEM_PROMPT = """你是 Protein Design Workbench 的操作智能体。工作目录: """ + REPO_ROOT + """

规则(必须遵守):
1. 一切任务状态/结果/数值必须来自工具返回。查不到就回答"不存在该任务",禁止编造 run id、状态或数值。
2. 查询类问题(状态/结果/系统情况)必须调用对应工具;提交类工具仅在用户明确要求执行计算时调用。
3. 用户提交计算时:调用 submit_ddg / submit_md,并告知"已提交,请稍后用查询工具确认状态"。不要谎称已完成。
4. 回答中保持科学边界:计算筛选≠实验验证;MD 不代表收敛;不预测亲和力;结果不得用于评分结论。
5. 用简体中文,简洁、准确,数字引用工具返回值。
"""

TOOLS_SPEC = [
    {"type": "function", "function": {
        "name": "list_runs",
        "description": "列出最近的计算任务(Rosetta/MD/MMPBSA)。返回 run_id、状态、关键结果。",
        "parameters": {"type": "object", "properties": {
            "kind": {"type": "string", "enum": ["rosetta", "md", "mmpbsa"], "description": "任务类型"},
            "limit": {"type": "integer", "description": "条数,默认 8"}},
            "required": ["kind"]}}},
    {"type": "function", "function": {
        "name": "get_run_detail",
        "description": "查询单个任务的详情(状态/参数/结果/报告摘录)。run_id 不存在时返回不存在。",
        "parameters": {"type": "object", "properties": {
            "run_id": {"type": "string", "description": "任务 run_id,如 20261113_230809_gmx_2hbb_f31a_3ns"}},
            "required": ["run_id"]}}},
    {"type": "function", "function": {
        "name": "get_eval_summary",
        "description": "获取最新的 S669 评测摘要(r/rho/样本数)。",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "get_system_status",
        "description": "获取服务器状态:GPU 显存/利用率、负载、磁盘。",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "submit_ddg",
        "description": "提交 Rosetta 单点突变稳定性计算(单体、cartesian_ddg 常规模式)。仅当用户明确要求执行时调用。",
        "parameters": {"type": "object", "properties": {
            "protein_path": {"type": "string", "description": "PDB 绝对路径或仓库相对路径"},
            "chain": {"type": "string", "description": "链 ID,单字符"},
            "resnum": {"type": "integer", "description": "PDB 残基编号"},
            "to_aa": {"type": "string", "description": "突变后氨基酸单字母"},
            "wt_aa": {"type": "string", "description": "可选,野生型单字母"},
            "note": {"type": "string", "description": "备注"}},
            "required": ["protein_path", "chain", "resnum", "to_aa"]}}},
    {"type": "function", "function": {
        "name": "submit_md",
        "description": "提交 GROMACS MD 流程(EM→NVT→NPT→MD)。仅当用户明确要求执行时调用。网页上限 3 ns。",
        "parameters": {"type": "object", "properties": {
            "protein_path": {"type": "string"}, "label": {"type": "string"},
            "ns": {"type": "number", "description": "时长,0.1-3"}},
            "required": ["protein_path", "label"]}}},
]


# ---------------------------------------------------------------------------
# 工具实现(确定性,无 shell)
# ---------------------------------------------------------------------------
def _load_env():
    d = {}
    p = os.path.join(REPO_ROOT, ".env")
    if os.path.isfile(p):
        for ln in open(p, encoding="utf-8"):
            ln = ln.strip()
            if ln and not ln.startswith("#") and "=" in ln:
                k, _, v = ln.partition("=")
                d[k.strip()] = v.strip().strip('"').strip("'")
    return d


def _find_manifest(run_id: str):
    for p in sorted(glob.glob(os.path.join(REPO_ROOT, "runs", "**", "manifest.json"), recursive=True)):
        m = _safe_json(p)
        if m and m.get("run_id") == run_id:
            return p, m
    return None, None


def _safe_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def tool_list_runs(kind: str = "md", limit: int = 8):
    from .dashboard import collect_runs
    kind = {"rosetta": "rosetta", "md": "md", "mmpbsa": "mmpbsa"}.get(str(kind).lower(), "md")
    rows = collect_runs()[kind][-int(limit):][::-1]
    return json.dumps(rows, ensure_ascii=False, default=str)


def tool_get_run_detail(run_id: str):
    p, m = _find_manifest(str(run_id))
    if not m:
        return json.dumps({"error": "不存在该任务", "run_id": run_id}, ensure_ascii=False)
    out = {
        "run_id": m.get("run_id"), "status": m.get("status"),
        "calc_type": (m.get("task") or {}).get("calc_type"),
        "task": {k: v for k, v in (m.get("task") or {}).items()
                 if k in ("protein_path", "chain", "resnum_pdb", "wt_aa", "mut_aa", "label",
                          "prod_ns", "duration_class", "input_sha256")},
        "result": m.get("result"),
        "gpu": (m.get("gpu_used_assessment") or {}).get("conclusion"),
        "analysis": m.get("analysis_summary"),
        "dir": os.path.dirname(p),
    }
    report = os.path.join(os.path.dirname(p), "report.txt")
    if os.path.isfile(report):
        out["report_head"] = "".join(open(report, errors="replace").readlines()[:30])
    return json.dumps(out, ensure_ascii=False, default=str)


def tool_get_eval_summary():
    evals = sorted(glob.glob(os.path.join(REPO_ROOT, "runs", "eval_*", "eval_details.json")))
    if not evals:
        return json.dumps({"error": "暂无评测报告"}, ensure_ascii=False)
    ed = _safe_json(evals[-1]) or {}
    return json.dumps({"dir": os.path.basename(os.path.dirname(evals[-1])),
                       "summary": ed.get("summary"),
                       "n_samples": len(ed.get("samples") or []),
                       "notices": (ed.get("notices") or [])[:3]}, ensure_ascii=False, default=str)


def tool_system_status():
    out = {"load": open("/proc/loadavg").read().split()[0:3]}
    try:
        r = subprocess.run(["nvidia-smi", "--query-gpu=index,memory.used,utilization.gpu",
                            "--format=csv,noheader"], capture_output=True, timeout=30)
        out["gpus"] = r.stdout.decode().strip().splitlines()
    except (OSError, subprocess.TimeoutExpired):
        out["gpus"] = "nvidia-smi 不可用"
    st = os.statvfs(REPO_ROOT)
    out["disk_free_GB"] = round(st.f_bavail * st.f_frsize / 1e9, 1)
    return json.dumps(out, ensure_ascii=False)


def _submit(argv_extra: list, kind: str):
    log_dir = os.path.join(REPO_ROOT, "runs", "agent_launch")
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, f"{kind}_{time.strftime('%Y%m%d_%H%M%S')}.log")
    argv = [PY, "-u", "-m", kind, *argv_extra]
    proc = subprocess.Popen(argv, cwd=REPO_ROOT, stdout=open(log_path, "w"),
                            stderr=subprocess.STDOUT, start_new_session=True)
    return {"submitted": True, "pid": proc.pid, "launch_log": os.path.relpath(log_path, REPO_ROOT),
            "hint": "任务已后台启动;请稍后用 list_runs/get_run_detail 查询真实状态,不要假定已完成"}


def tool_submit_ddg(protein_path, chain, resnum, to_aa, wt_aa="", note=""):
    p = protein_path if os.path.isabs(protein_path) else os.path.join(REPO_ROOT, protein_path)
    p = os.path.abspath(p)
    if not os.path.isfile(p):
        return json.dumps({"error": f"文件不存在: {protein_path}"}, ensure_ascii=False)
    if not (isinstance(chain, str) and len(chain) == 1 and chain.isalnum()):
        return json.dumps({"error": "chain 必须是单字符"}, ensure_ascii=False)
    if not (isinstance(resnum, int) and 1 <= resnum <= 10000):
        return json.dumps({"error": "resnum 必须是 1-10000 整数"}, ensure_ascii=False)
    if not (isinstance(to_aa, str) and len(to_aa) == 1 and to_aa.upper() in "ACDEFGHIKLMNPQRSTVWY"):
        return json.dumps({"error": "to_aa 必须是合法单字母氨基酸"}, ensure_ascii=False)
    argv = ["workbench.cli", "--protein", p, "--chain", chain.upper(),
            "--resnum", str(int(resnum)), "--to", to_aa.upper()]
    if wt_aa:
        argv += ["--wt", str(wt_aa)[:1].upper()]
    if note:
        argv += ["--note", str(note)[:200].replace("\n", " ")]
    return json.dumps(_submit(argv, "workbench.cli"), ensure_ascii=False)


def tool_submit_md(protein_path, label, ns=1.0):
    p = protein_path if os.path.isabs(protein_path) else os.path.join(REPO_ROOT, protein_path)
    p = os.path.abspath(p)
    if not os.path.isfile(p):
        return json.dumps({"error": f"文件不存在: {protein_path}"}, ensure_ascii=False)
    try:
        ns = float(ns)
    except (TypeError, ValueError):
        return json.dumps({"error": "ns 必须是数字"}, ensure_ascii=False)
    if not 0.1 <= ns <= 3.0:
        return json.dumps({"error": "网页提交上限 0.1-3 ns;更长模拟请用 CLI --confirm-long-run 走正式流程"},
                          ensure_ascii=False)
    label = str(label)
    if not re.fullmatch(r"[A-Za-z0-9_.\-]{1,40}", label):
        return json.dumps({"error": "label 只能含字母/数字/._- 且 ≤40 字符"}, ensure_ascii=False)
    return json.dumps(_submit(["workbench.gromacs", "--protein", p, "--chain", "A",
                               "--label", label, "--ns", str(ns)],
                              "workbench.gromacs"), ensure_ascii=False)


TOOL_IMPL = {
    "list_runs": tool_list_runs,
    "get_run_detail": tool_get_run_detail,
    "get_eval_summary": tool_get_eval_summary,
    "get_system_status": tool_system_status,
    "submit_ddg": tool_submit_ddg,
    "submit_md": tool_submit_md,
}


# ---------------------------------------------------------------------------
# LLM 循环
# ---------------------------------------------------------------------------
def _call_llm(messages):
    import time as _time
    cfg = _load_env()
    from openai import OpenAI
    client = OpenAI(api_key=cfg["LLM_API_KEY"], base_url=cfg["LLM_BASE_URL"])
    last_exc = None
    for attempt in range(3):
        try:
            r = client.chat.completions.create(model=cfg["LLM_MODEL"], messages=messages,
                                               tools=TOOLS_SPEC, max_tokens=2048,
                                               temperature=0.2)
            return r.choices[0].message
        except Exception as e:  # 429 限流退避重试(glm-4.7-flash 免费档常见)
            last_exc = e
            if "429" in str(e) or "RateLimit" in type(e).__name__ or "1305" in str(e):
                _time.sleep([4, 12, 30][min(attempt, 2)])
                continue
            raise
    raise last_exc


def answer(question: str, history: list | None = None) -> str:
    """完整回答一条用户问题(工具循环 ≤6 轮);返回 markdown(含工具调用痕迹)。"""
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    for h in (history or [])[-6:]:
        messages.append({"role": h["role"], "content": str(h.get("content", ""))[:2000]})
    messages.append({"role": "user", "content": str(question)[:2000]})
    trace = []
    try:
        for _ in range(6):
            msg = _call_llm(messages)
            if msg.tool_calls:
                messages.append({"role": "assistant", "content": msg.content or "",
                                 "tool_calls": [
                                     {"id": tc.id, "type": "function",
                                      "function": {"name": tc.function.name,
                                                   "arguments": tc.function.arguments}}
                                     for tc in msg.tool_calls]})
                for tc in msg.tool_calls:
                    fn = tc.function.name
                    try:
                        argv = json.loads(tc.function.arguments or "{}")
                    except json.JSONDecodeError:
                        argv = {}
                    trace.append(f"🔧 {fn}({json.dumps(argv, ensure_ascii=False)[:120]})")
                    impl = TOOL_IMPL.get(fn)
                    result = json.dumps({"error": "未知工具"}, ensure_ascii=False) if impl is None else impl(**argv)
                    trace.append("   ↳ " + result[:400].replace("\n", " "))
                    messages.append({"role": "tool", "tool_call_id": tc.id,
                                     "content": str(result)[:3000]})
                continue
            text = (msg.content or "").strip()
            if not text:
                text = "模型未返回文本;请重试或改用 CLI(不受影响)。"
            if trace:
                text += "\n\n<details><summary>工具调用记录</summary>\n\n" + "\n\n".join(trace) + "\n</details>"
            return text
        return "工具循环达到上限,未得到最终回答;请缩小问题范围或使用 CLI。"
    except Exception as e:  # noqa: BLE001
        return (f"⚠️ LLM 调用不可用({type(e).__name__}: {str(e)[:200]})。\n"
                "工作台本身不受影响:CLI(`python -m workbench.cli / workbench.gromacs`)与只读面板照常可用。")


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------
def build_ui():
    import gradio as gr
    from .dashboard import collect_runs, collect_evals, collect_figures

    data = collect_runs()
    evals = collect_evals()

    with gr.Blocks(title="Protein Design Workbench Agent") as ui:
        gr.Markdown("# Protein Design Workbench — 智能体")
        with gr.Tab("介绍"):
            gr.Markdown(INTRO_MD)
        with gr.Tab("智能体"):
            gr.Markdown("**问状态、问结果、要求提交计算**——任务状态一律来自工具返回,不编造;"
                        "提交类操作仅在你明确要求时执行,且都是 workbench 既有确定性流程。")
            chat = gr.Chatbot(height=420, show_label=False)
            box = gr.Textbox(placeholder="例如:列出最近的 MD 任务 / 最新评测 r 是多少 / 查询 20261113_230809 的详情", show_label=False)
            with gr.Row():
                send = gr.Button("发送", variant="primary")
                clear = gr.Button("清空")
            state = gr.State([])

            def respond(msg, hist):
                hist = (hist or [])[-12:]
                reply = answer(msg, hist)
                hist = hist + [{"role": "user", "content": msg}, {"role": "assistant", "content": reply}]
                return hist, hist, ""

            send.click(respond, [box, state], [chat, state, box])
            box.submit(respond, [box, state], [chat, state, box])
            clear.click(lambda: ([], []), None, [chat, state])

        with gr.Tab("任务面板"):
            gr.Dataframe(headers=["run_id", "状态", "蛋白", "突变", "ΔΔG (REU)"],
                         value=data["rosetta"][-40:], interactive=False, wrap=True)
            gr.Dataframe(headers=["run_id", "状态", "体系", "时长档", "ns/day"],
                         value=data["md"], interactive=False, wrap=True)
            gr.Dataframe(headers=["run_id", "状态", "类型", "来源 MD", "TOTAL (kJ/mol)", "帧数"],
                         value=data["mmpbsa"], interactive=False, wrap=True)

        with gr.Tab("评测"):
            for e in evals[-3:]:
                gr.Markdown(f"### {e['dir']}（{e['time']}，匹配样本 {e['n_matched']}）")
                gr.JSON(e["summary"])
            if not evals:
                gr.Markdown("暂无评测报告。")

        with gr.Tab("图表"):
            gr.Gallery(value=collect_figures(), columns=3, height="500px", show_label=False)

        gr.Markdown("---\n*计算筛选 ≠ 实验验证;面板为启动时快照,重启刷新。*")
    return ui


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="workbench-agent")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=7860)
    args = ap.parse_args(argv)
    ui = build_ui()
    ui.launch(server_name=args.host, server_port=args.port, share=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
