"""Rosetta Agent Web 服务：与 GROMACS 平台同架构（FastAPI + SSE + LangGraph 智能体）。

  POST /api/tasks                 上传蛋白(+可选配体)、突变位点，提交三段流水线
  GET  /api/tasks                 历史任务列表（live 标记活动任务）
  GET  /api/tasks/{id}/events     SSE 实时事件
  POST /api/tasks/{id}/message    任务完成后继续对话
  GET  /api/tasks/{id}/files      任务工作区产物
  GET  /api/tasks/{id}/download   打包下载
"""
import json
import os
import re
import sys
import glob
import time
import base64
import shutil
import zipfile
import threading
import datetime
import traceback

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_ROOT)
os.chdir(PROJECT_ROOT)  # rosetta 工具的工作区相对路径以项目目录为基准

# rosetta 工具内部以裸 `python` 调用脚本，必须把 conda env 的 bin 放进 PATH
_ENV_BIN = os.path.expanduser(os.getenv(
    "CONDA_ENV_BIN", "~/.conda/envs/rosetta-agent/bin"))
os.environ["PATH"] = _ENV_BIN + os.pathsep + os.environ.get("PATH", "")

os.environ.setdefault("GRADIO_ANALYTICS_ENABLED", "False")
os.environ.setdefault("HF_HUB_OFFLINE", "1")

from fastapi import FastAPI, UploadFile, Form, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import viz_render  # PyMOL 发表级渲染（5 种样式，见模块说明）
import task_runtime  # 任务 ID/路径校验/位点预检/持久状态（2026-11-20 收敛到公共层）
from rosetta_agent import agent_executor
from rosetta_tools import current_workspace
from langchain_core.messages import HumanMessage, SystemMessage

OUTPUT_DIR = os.path.join(PROJECT_ROOT, "output")
os.makedirs(OUTPUT_DIR, exist_ok=True)
VIZ_DIR = os.path.join(OUTPUT_DIR, "_viz")
os.makedirs(VIZ_DIR, exist_ok=True)

JOBS_DEFAULT = 10
DELAY_DEFAULT = 1.5

SYS_RULE = (
    "你是 Rosetta 计算生物助手。"
    "当用户提交了蛋白+配体+突变位点时，系统会自动按"
    "【参数化→弛豫→突变】三段流水线执行；"
    "若用户仅询问文档或报错，可使用 search_rosetta_docs / read_file 检索；"
    "其余情况直接自然语言回复即可。"
    "【编号铁律】run_saturation_mutagenesis 接收【PDB 晶体学编号】（如 76,73），"
    "工具内部自动转换为 Rosetta 绝对行号；"
    "get_rosetta_numbering 仅供核对（只读），核对后必须传回原始 PDB 编号，"
    "绝对禁止把它的绝对行号输出再传给 run_saturation_mutagenesis，否则会导致双重偏移错位。"
)

app = FastAPI(title="Rosetta Agent 平台")
TASKS = {}
_LOCK = threading.Lock()


@app.middleware("http")
async def no_cache_for_dev(request, call_next):
    """HTML/JS/CSS 每次都走 etag 校验（no-cache=可缓存但必须再验证）。

    平台在活跃开发期，浏览器启发式缓存会拿到陈旧页面；静态库 3Dmol.min.js
    体积大且稳定，允许正常缓存。
    """
    resp = await call_next(request)
    path = request.url.path
    if path == "/" or path.endswith((".html", ".js", ".css")) and "3Dmol-min" not in path:
        resp.headers["Cache-Control"] = "no-cache"
    return resp


def _emit(task_id: str, event: dict):
    with _LOCK:
        TASKS[task_id]["events"].append(event)


def _safe_task_dir(task_id: str) -> str:
    """校验 task_id 并返回任务目录（防路径遍历/符号链接，2026-11-20 收敛到公共层）。

    task_id 由本服务生成，但 /files /download 等接口直接把它拼进文件路径。
    规则（task_runtime.validate_task_dir）：禁止路径分隔符；realpath 后必须仍在
    output/ 内（穿越与软链外指都会被拒）；目录名必须以 task_ 开头。
    """
    try:
        return task_runtime.validate_task_dir(task_id, base=OUTPUT_DIR)
    except ValueError:
        raise HTTPException(404, "任务不存在")


def _precheck_targets(pdb_path: str, mutation: str):
    """提交前确定性预检：突变位点必须存在于上传结构且为标准氨基酸。

    2026-11-20 起委托 task_runtime.precheck_targets——与 get_rosetta_numbering /
    run_saturation_mutagenesis / run_fixbb_design 共用同一解析实现，
    保证"预检接受的位点"与"执行时映射的位点"一致（旧实现三处规则不一致，
    非标准残基位点会通过预检却在弛豫后才报错）。
    返回 (ok, note)；ok=True 时 note 含逐位点 WT 氨基酸（供 Agent 与用户核对）。
    """
    return task_runtime.precheck_targets(pdb_path, mutation)


def _load_ddg_rows(workspace: str):
    path = os.path.join(workspace, "summary", "ddg_results.txt")
    rows = []
    if not os.path.exists(path):
        return rows
    with open(path, encoding="utf-8") as f:
        for line in f.readlines()[1:]:
            parts = line.strip().split("\t")
            if len(parts) >= 2:
                try:
                    rows.append((parts[0].strip(), float(parts[1])))
                except ValueError:
                    continue
    return rows


def _interpret_ddg(workspace: str):
    """对 ddg_results.txt 做确定性解读：分档统计 + Top 有利/去稳定 + 口径声明。"""
    rows = _load_ddg_rows(workspace)
    if not rows:
        return None
    rows_sorted = sorted(rows, key=lambda r: r[1])
    stable = [r for r in rows if r[1] <= -1.0]
    mild = [r for r in rows if -1.0 < r[1] <= -0.5]
    destab = [r for r in rows if r[1] >= 2.0]
    lines = ["📊 ddG 结果解读（自动汇总；分档阈值仅为展示参考，无实验依据）",
             f"共 {len(rows)} 个突变体：强稳定候选(ddG≤-1.0) {len(stable)} 个、"
             f"轻微有利(-1.0~0) {len(mild)} 个、强去稳定(≥+2.0) {len(destab)} 个。"]
    if rows_sorted:
        lines.append("最有利 Top5（ddG 最负，供筛选参考，非实验结论）：")
        for name, v in rows_sorted[:5]:
            lines.append(f"  {name}: {v:+.3f} REU")
    if destab:
        lines.append("强去稳定 Top3（应避开）：")
        for name, v in sorted(destab, key=lambda r: -r[1])[:3]:
            lines.append(f"  {name}: {v:+.3f} REU")
    lines.append("口径: cartesian_ddg 常规稳定性模式（REU）。计算筛选 ≠ 实验验证；"
                 "点击上方位点可在右侧结构中定位查看。")
    return "\n".join(lines)


def _ddg_chart(workspace: str):
    """生成 ddG 条形图（Top30 最有利优先），保存 summary/ddg_chart.png 并返回 data URL。

    图内文字用英文，避免服务器缺 CJK 字体出现方块。
    """
    rows = _load_ddg_rows(workspace)
    if not rows:
        return None
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    top = sorted(rows, key=lambda r: r[1])[:30]
    names = [r[0] for r in top][::-1]
    vals = [r[1] for r in top][::-1]
    colors = ["#2fbf71" if v < 0 else "#ff7a6e" for v in vals]
    fig, ax = plt.subplots(figsize=(6.2, max(3.0, 0.28 * len(vals) + 1.2)), dpi=140)
    ax.barh(names, vals, color=colors)
    ax.axvline(0, color="#666", lw=0.8)
    ax.set_xlabel("ddG (REU), <0 = stabilizing")
    ax.set_title(f"cartesian_ddG of {len(top)} mutations (most stabilizing first)")
    fig.tight_layout()
    out = os.path.join(workspace, "summary", "ddg_chart.png")
    fig.savefig(out)
    plt.close(fig)
    with open(out, "rb") as f:
        return "data:image/png;base64," + base64.b64encode(f.read()).decode()


def _consume(task_id: str, messages: list):
    # ContextVar 不会跨线程继承：必须在 worker 线程内重新设置工作区，
    # 否则首个 TaskAwareToolNode 之前任何直接读 current_workspace 的代码都拿到默认值 "."
    current_workspace.set(os.path.join("output", task_id))
    task_dir = os.path.join(OUTPUT_DIR, task_id)
    n_tool_errors = 0
    last_error = ""
    try:
        task_runtime.state_write(task_dir, status="running", stage="conversation",
                                 status_note="Agent 流执行中")
        config = {"configurable": {"thread_id": task_id}, "recursion_limit": 200}
        for event in agent_executor.stream({"messages": messages}, config,
                                           stream_mode="updates"):
            for _node, node_state in event.items():
                if "messages" not in node_state:
                    continue
                msg = node_state["messages"][-1]
                mtype = getattr(msg, "type", "")
                if mtype == "ai":
                    if getattr(msg, "tool_calls", None):
                        for tc in msg.tool_calls:
                            _emit(task_id, {"type": "tool_start", "name": tc["name"]})
                    elif msg.content:
                        _emit(task_id, {"type": "agent", "text": str(msg.content)})
                elif mtype == "tool":
                    content = str(msg.content)
                    bad = ("【执行报错】" in content) or ("【系统异常】" in content) or ("❌" in content)
                    if bad:
                        n_tool_errors += 1
                        last_error = content[:500]
                    _emit(task_id, {"type": "tool_end", "name": getattr(msg, "name", "?"),
                                    "ok": not bad, "detail": content[:500]})
                elif mtype == "human" and getattr(msg, "name", "") in (
                        "ProgressReporter", "BindingDirector", "DisorderDirector"):
                    # 确定性节点的进度/对照/disorder 播报：作为 agent 文本事件转发，
                    # 否则前端在长时间后台计算期间收不到任何进度
                    _emit(task_id, {"type": "agent", "text": str(msg.content)})
    except Exception as e:
        _emit(task_id, {"type": "done", "ok": False, "error": f"{type(e).__name__}: {e}",
                        "outcome": "failed"})
        traceback.print_exc()
        with _LOCK:
            TASKS[task_id]["done"] = True
        task_runtime.settle_task_after_stream(
            task_dir, had_error=True, error_type=type(e).__name__,
            error_detail=f"{type(e).__name__}: {e}")
        return
    # P1 状态收口：区分 完成/失败/后台仍在运行（汇总缺失+runner 存活），
    # 不再把"流结束"一律当成功。outcome 附加进 done 事件（前端忽略未知字段）。
    outcome = task_runtime.settle_task_after_stream(
        task_dir, had_error=n_tool_errors > 0, error_type="ToolError",
        error_detail=last_error)
    _emit(task_id, {"type": "done", "ok": True, "outcome": outcome})
    with _LOCK:
        TASKS[task_id]["done"] = True


# ==========================================
# 流水线进度监控：确定性读取任务目录（不依赖 LLM 播报），变化才推 SSE
# ==========================================
def _progress_snapshot(workspace: str) -> dict:
    """从文件系统推断三段流水线各自的进度（pct/state/detail）。"""
    steps = {
        "p1": {"label": "配体参数化", "pct": 0, "state": "wait", "detail": "未开始"},
        "p2": {"label": "复合物弛豫", "pct": 0, "state": "wait", "detail": "未开始"},
        "p3": {"label": "饱和突变", "pct": 0, "state": "wait", "detail": "未开始"},
    }

    param_dir = os.path.join(workspace, "parameterize_ligand")
    if os.path.isdir(param_dir):
        params = glob.glob(os.path.join(param_dir, "*.params"))
        if params:
            steps["p1"] = {"label": "配体参数化", "pct": 100, "state": "done",
                           "detail": f"已生成 {os.path.basename(params[0])}"}
        else:
            steps["p1"] = {"label": "配体参数化", "pct": 40, "state": "run",
                           "detail": "PyMOL 参数化进行中…"}

    relax_dir = os.path.join(workspace, "cartesian_relax")
    runner = os.path.join(relax_dir, "run_relax_parallel.py")
    if os.path.exists(runner):
        nstruct = 3
        try:
            with open(runner, encoding="utf-8") as f:
                m = re.search(r"range\(1, (\d+) \+ 1\)", f.read())
            if m:
                nstruct = int(m.group(1))
        except Exception:
            pass
        if os.path.exists(os.path.join(relax_dir, "score.sc")):
            steps["p2"] = {"label": "复合物弛豫", "pct": 100, "state": "done",
                           "detail": f"{nstruct} 个构象已完成，已合并择优"}
        else:
            done_pdbs = len(glob.glob(os.path.join(relax_dir, "cart_relax_*_0001.pdb")))
            pct = min(95, max(8, int(done_pdbs / max(nstruct, 1) * 100)))
            steps["p2"] = {"label": "复合物弛豫", "pct": pct, "state": "run",
                           "detail": f"已完成 {done_pdbs}/{nstruct} 个构象"}

    tasks_file = os.path.join(workspace, "saturation_mutagenesis", "rosetta_tasks.txt")
    if os.path.exists(tasks_file):
        try:
            with open(tasks_file, encoding="utf-8") as f:
                total = sum(1 for l in f
                            if l.strip() and not l.strip().startswith("#")
                            and "cartesian_ddg" in l)
        except Exception:
            total = 0
        ddg = len(glob.glob(os.path.join(workspace, "saturation_mutagenesis", "*.ddg")))
        summary = os.path.join(workspace, "summary", "ddg_results.txt")
        if os.path.exists(summary):
            steps["p3"] = {"label": "饱和突变", "pct": 100, "state": "done",
                           "detail": f"{total} 个计算全部完成"}
        elif total > 0:
            pct = min(95, max(8, int(ddg / total * 100)))
            steps["p3"] = {"label": "饱和突变", "pct": pct, "state": "run",
                           "detail": f"{ddg}/{total} 个突变计算"}
        else:
            steps["p3"] = {"label": "饱和突变", "pct": 10, "state": "run", "detail": "任务展开中…"}
    return steps


def _progress_monitor(task_id: str):
    """后台线程：每 5s 对比一次快照，变化才 emit（SSE 事件 type=progress）。"""
    workspace = os.path.join(OUTPUT_DIR, task_id)
    last = None
    while True:
        with _LOCK:
            task = TASKS.get(task_id)
            done = bool(task and task["done"])
        if done:
            break
        try:
            snap = _progress_snapshot(workspace)
            if snap != last:
                _emit(task_id, {"type": "progress", "steps": snap})
                last = snap
        except Exception:
            traceback.print_exc()
        time.sleep(5)


def _build_prompt(ligand_name: str, mutation: str,
                  params: str, msg: str) -> str:
    """复刻 rosetta1 run_cli 的提示词组装：蛋白 + 配体(自动分离) + 突变位点红线规则。"""
    parts = ["蛋白信息: protein.pdb"]
    if ligand_name:
        parts.append(
            f"【系统强制规则】配体已自动保存为 {ligand_name}。"
            f"第一步必须调用 parameterize_ligand(ligand_path='{ligand_name}')。"
        )
    if mutation.strip():
        mut = mutation.replace("，", ",")
        parts.append(
            f"目标突变位点: {mut}。\n"
            "【红线警告】：\n"
            "1. 你必须严格按顺序、不间断地生成工具调用：parameterize_ligand → run_cartesian_relax → run_saturation_mutagenesis。\n"
            "2. 除非上一步明确返回了【执行报错】，否则绝对禁止在弛豫成功前输出纯文本！\n"
            "3. 传给 run_saturation_mutagenesis 的必须是【PDB 晶体学编号】（如 76,73），"
            "工具内部自动转换为 Rosetta 绝对行号；绝对禁止把 get_rosetta_numbering 返回的绝对行号再传给它，否则双重偏移错位。\n"
            "4. 如果某一步报错，请调用 diagnose_and_fix 获取修复建议后重试。"
        )
    if params.strip():
        parts.append(f"其他参数: {params}")
    if msg.strip():
        parts.append(f"指令: {msg}")
    return " | ".join(parts)


@app.post("/api/tasks")
async def create_task(file: UploadFile,
                      ligand: UploadFile = None,
                      mutation: str = Form(...),
                      params: str = Form(""),
                      msg: str = Form(""),
                      engine: str = Form("rosetta")):
    if not file.filename or not file.filename.lower().endswith(".pdb"):
        raise HTTPException(400, "请上传 .pdb 蛋白结构文件")
    # 任务 ID：标签白名单清洗 + 时间戳含随机后缀 + 独占建目录（2026-11-20）。
    # 旧行为：秒级时间戳+未清洗文件名，同秒重复提交会共用目录互相覆盖。
    protein_id = os.path.splitext(os.path.basename(file.filename))[0][:40] or "protein"
    task_id, workspace_dir = task_runtime.create_task_dir(protein_id, base=OUTPUT_DIR)

    with open(os.path.join(workspace_dir, "protein.pdb"), "wb") as f:
        shutil.copyfileobj(file.file, f)

    if engine not in ("rosetta", "tmol"):
        raise HTTPException(400, "engine 仅支持 rosetta 或 tmol")
    task_runtime.state_write(workspace_dir, status="created", stage="created",
                             manifest={"engine": engine},
                             input_summary={"protein": os.path.basename(file.filename)[:80],
                                            "mutation": str(mutation)[:200],
                                            "ligand": bool(ligand and ligand.filename),
                                            "engine": engine,
                                            "params": str(params)[:120],
                                            "msg": str(msg)[:120],
                                            "entry": "fastapi_7779"})

    # 提交前确定性预检：位点不存在/格式错立即拒绝，不烧弛豫算力
    precheck_note = ""
    if mutation.strip():
        ok, precheck_note = _precheck_targets(os.path.join(workspace_dir, "protein.pdb"), mutation)
        if not ok:
            task_runtime.state_write(workspace_dir, status="failed", stage="precheck",
                                     error_type="PrecheckFailed",
                                     error_detail=str(precheck_note)[:500])
            raise HTTPException(400, precheck_note)

    ligand_name = None
    if ligand is not None and ligand.filename:
        ext = os.path.splitext(ligand.filename)[1].lower()
        if ext not in (".pdb", ".mol2"):
            raise HTTPException(400, "配体仅支持 .pdb / .mol2")
        ligand_name = f"ligand{ext}"
        with open(os.path.join(workspace_dir, ligand_name), "wb") as f:
            shutil.copyfileobj(ligand.file, f)
    else:
        # 未上传配体时尝试 PyMOL 自动分离（与 CLI 逻辑一致）
        import subprocess
        pymol_bin = os.getenv("PYMOL_BIN", "/usr/local/bin/pymol")
        target_protein = os.path.join(workspace_dir, "protein.pdb")
        target_ligand = os.path.join(workspace_dir, "ligand.pdb")
        try:
            subprocess.run(
                [pymol_bin, "-c", "-d",
                 f"load {target_protein}, complex; save {target_ligand}, complex and organic"],
                check=True, capture_output=True, timeout=120)
            if os.path.exists(target_ligand) and os.path.getsize(target_ligand) > 0:
                # PyMOL 对空 selection 也会写出仅含 END 的 4 字节文件，
                # 必须确认真的分离到 ATOM/HETATM 原子行才算有配体，
                # 否则纯蛋白任务会被带进配体参数化分支而失败
                with open(target_ligand, errors="ignore") as _lf:
                    _has_atoms = any(
                        _l.startswith(("ATOM", "HETATM")) for _l in _lf)
                if _has_atoms:
                    ligand_name = "ligand.pdb"
                else:
                    # 空 selection 的假配体文件会骗过工具侧 has_ligand 探测，直接删除
                    try:
                        os.remove(target_ligand)
                    except OSError:
                        pass
        except Exception:
            pass  # 无配体任务则继续

    with _LOCK:
        TASKS[task_id] = {"events": [], "done": False}

    # 工作区在 _consume（worker 线程）内设置：ContextVar 不跨线程继承，
    # 在请求线程 set 对 worker 线程无效
    prompt = _build_prompt(ligand_name, mutation, params, msg)
    if precheck_note:
        prompt += f" | {precheck_note}"
    threading.Thread(target=_consume, args=(task_id, [
        SystemMessage(content=SYS_RULE),
        HumanMessage(content=prompt),
    ]), daemon=True).start()
    threading.Thread(target=_progress_monitor, args=(task_id,), daemon=True).start()
    return {"task_id": task_id, "prompt": prompt}


class MessageIn(BaseModel):
    message: str


@app.post("/api/tasks/{task_id}/message")
async def task_message(task_id: str, body: MessageIn):
    with _LOCK:
        task = TASKS.get(task_id)
    task_dir = os.path.join(OUTPUT_DIR, task_id)
    restarted = False
    if task is None:
        # 服务重启后内存事件流丢失；任务目录与 task_status.json 仍在时可续对话。
        # 诚实边界：LangGraph 检查点是内存态，重启后对话历史不可恢复——本次续聊是全新上下文。
        if task_runtime.validate_task_dir(task_id, base=OUTPUT_DIR) and \
                task_runtime.state_read(task_dir):
            st = task_runtime.state_effective(task_dir)["status"]
            if st == "running":
                raise HTTPException(409, "后台计算仍在运行，完成后才能继续对话")
            restarted = True
        else:
            raise HTTPException(404, "任务不存在（且没有可查询的历史状态文件）")
    else:
        if not task["done"]:
            raise HTTPException(409, "任务仍在运行中，完成后才能继续对话")
    with _LOCK:
        TASKS[task_id] = {"events": [], "done": False}
    if restarted:
        _emit(task_id, {"type": "agent", "text":
             "【提示】服务在此任务提交后重启过：历史对话与后台事件的内存记录已丢失，"
             "本条续聊在全新上下文中进行；任务产物与 task_status.json 仍可查询。"})
    threading.Thread(target=_consume, args=(task_id, [HumanMessage(content=body.message)]),
                     daemon=True).start()
    threading.Thread(target=_progress_monitor, args=(task_id,), daemon=True).start()
    return {"task_id": task_id, "status": "continued",
            "reconnected_after_restart": restarted}




# ---------- API: 主机资源状态(页面指示牌) ----------
@app.get("/api/host/status")
def host_status():
    import os as _os, subprocess as _sp
    try:
        r = _sp.run(["nvidia-smi", "--query-gpu=index,utilization.gpu,memory.used,memory.total,name",
                     "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=8)
        gpus = []
        for line in r.stdout.splitlines():
            p = [x.strip() for x in line.split(",")]
            if len(p) >= 5:
                try:
                    gpus.append({"idx": int(p[0] or 0), "util": float(p[1] or 0),
                                 "mem_used": float(p[2] or 0), "mem_total": float(p[3] or 0),
                                 "name": p[4]})
                except ValueError:
                    pass
    except Exception:
        gpus = []
    try:
        l1 = _os.getloadavg()[0]
    except Exception:
        l1 = 0.0
    cores = _os.cpu_count() or 1
    return {"gpus": gpus,
            "cpu": {"load1": round(l1, 2), "cores": cores,
                    "percent": round(min(l1 / cores * 100.0, 100.0), 1)}}

@app.get("/api/tasks")
async def list_tasks():
    with _LOCK:
        live_ids = set(TASKS.keys())
    items = []
    if os.path.isdir(OUTPUT_DIR):
        for name in sorted(os.listdir(OUTPUT_DIR), reverse=True):
            p = os.path.join(OUTPUT_DIR, name)
            if os.path.isdir(p) and name.startswith("task_"):
                # 汇总目录名以 rosetta_tools.run_saturation_mutagenesis 实际创建的 "summary" 为准
                # （早期文档里的 tool5_summary 从未存在过，曾导致前端永远显示未完成）
                summary = os.path.join(p, "summary", "ddg_results.txt")
                # P1：持久状态（重启后仍可查询）；running 状态结合 runner 进程与产物判定
                eff = task_runtime.state_effective(p)
                items.append({"task_id": name,
                              "final_pdb": os.path.exists(summary),
                              "live": name in live_ids,
                              "status": eff["status"],
                              "status_note": eff.get("note", "")})
    return {"tasks": items[:50]}


@app.get("/api/tasks/{task_id}/status")
async def task_status(task_id: str):
    """任务持久状态查询（重启后仍可用）：读取任务目录内 task_status.json 并解释生效状态。"""
    try:
        task_dir = task_runtime.validate_task_dir(task_id, base=OUTPUT_DIR)
    except ValueError:
        raise HTTPException(404, "任务不存在")
    eff = task_runtime.state_effective(task_dir)
    if not eff["state"]:
        raise HTTPException(404, "该任务没有 task_status.json（历史旧任务或状态文件缺失）")
    return {"task_id": task_id, "status": eff["status"], "note": eff.get("note", ""),
            "state": eff["state"]}


@app.get("/api/tasks/{task_id}/events")
async def task_events(task_id: str):
    with _LOCK:
        task = TASKS.get(task_id)
    if task is None:
        # SSE 只回放本进程内存中的事件；重启后改查 /api/tasks/{id}/status（持久状态）
        # 与任务目录产物。
        has_state = False
        try:
            has_state = bool(task_runtime.state_read(
                task_runtime.validate_task_dir(task_id, base=OUTPUT_DIR)))
        except (ValueError, OSError):
            pass
        msg = ("任务不存在（服务重启后历史事件不可回放）" if not has_state
               else "任务不在运行中（服务重启过，事件流不可回放；状态请查 /api/tasks/{id}/status）")
        raise HTTPException(404, msg)

    async def gen():
        import asyncio
        idx = 0
        while True:
            with _LOCK:
                events = list(TASKS[task_id]["events"])
                done = TASKS[task_id]["done"]
            while idx < len(events):
                yield f"data: {json.dumps(events[idx], ensure_ascii=False)}\n\n"
                idx += 1
            if done and idx >= len(events):
                yield "data: {\"type\": \"stream_end\"}\n\n"
                return
            await asyncio.sleep(0.6)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/tasks/{task_id}/files")
async def task_files(task_id: str):
    workspace = _safe_task_dir(task_id)
    items = []
    for root, _dirs, files in os.walk(workspace):
        for name in files:
            p = os.path.join(root, name)
            rel = os.path.relpath(p, workspace)
            items.append({"name": rel, "size_kb": round(os.path.getsize(p) / 1024, 1)})
    items.sort(key=lambda x: x["name"])
    return {"files": items[:200]}


_ARCH_EXTS = {".pdb", ".mol2", ".params", ".ddg", ".log", ".sc", ".txt", ".md", ".json"}

@app.get("/api/tasks/{task_id}/download")
async def task_download(task_id: str):
    workspace = _safe_task_dir(task_id)
    zip_path = os.path.join(OUTPUT_DIR, f"{task_id}_results.zip")
    try:
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for root, _dirs, files in os.walk(workspace):
                for name in files:
                    if os.path.splitext(name)[1].lower() in _ARCH_EXTS:
                        p = os.path.join(root, name)
                        zf.write(p, os.path.relpath(p, OUTPUT_DIR))
    except Exception as e:
        raise HTTPException(500, f"打包失败: {e}")
    if not os.path.exists(zip_path):
        raise HTTPException(404, "没有可打包的产物")
    return FileResponse(zip_path, filename=os.path.basename(zip_path),
                        media_type="application/zip")


@app.get("/api/tasks/{task_id}/interpretation")
async def task_interpretation(task_id: str):
    """ddG 结果解读：确定性汇总分档 + Top 位点 + matplotlib 条形图（data URL）。

    供前端在任务完成时拉取展示；纯读接口，不触发任何计算（图仅首次生成后复用）。
    """
    workspace = _safe_task_dir(task_id)
    text = _interpret_ddg(workspace)
    if not text:
        raise HTTPException(404, "该任务还没有 ddg_results.txt 汇总（突变计算未完成或未运行）")
    chart = None
    try:
        chart = _ddg_chart(workspace)
    except Exception:
        chart = None  # 图表失败不影响文字解读
    return {"ok": True, "text": text, "chart": chart}


@app.get("/api/structure")
async def task_structure(task_id: str):
    """返回任务结构的 PDB 文本（供前端 3Dmol WebGL 交互式查看器加载）。

    结构优先级与 /api/viz/render 一致：弛豫择优 wt_relaxed.pdb → 清洗复合物 → 原始 protein.pdb。
    """
    workspace = _safe_task_dir(task_id)
    for cand in ("cartesian_relax/wt_relaxed.pdb",
                 "cartesian_relax/complex_rosetta_ready.pdb",
                 "protein.pdb"):
        p = os.path.join(workspace, cand)
        if os.path.exists(p):
            return FileResponse(p, media_type="text/plain", filename=os.path.basename(p))
    raise HTTPException(404, "该任务下没有可用的 PDB（protein.pdb / wt_relaxed.pdb）")


# ---------------- 交互式 3D 查看器：项目内 PDB 文件服务 ----------------

def _safe_pdb_path(rel: str) -> str:
    """把相对路径限定在项目根内的 .pdb 文件上（查看器/发表图共用）。

    realpath 后必须仍在项目根内，后缀必须 .pdb，防止 ../ 遍历。
    """
    root = os.path.realpath(PROJECT_ROOT)
    target = os.path.realpath(os.path.join(root, rel))
    if target != root and not target.startswith(root + os.sep):
        raise HTTPException(404, "非法路径")
    if not target.lower().endswith(".pdb") or not os.path.isfile(target):
        raise HTTPException(404, "PDB 文件不存在")
    if os.path.getsize(target) > 300 * 1024 * 1024:
        raise HTTPException(400, "文件过大（>300MB），查看器不支持")
    return target


def _scan_viewer_pdbs():
    """扫描可查看的 PDB：inputs/pdbs 全量 + workspaces 每任务代表结构。

    workspaces 里突变产物成百上千，只列代表性文件避免下拉爆炸。
    """
    items = {}
    inputs_dir = os.path.join(PROJECT_ROOT, "inputs", "pdbs")
    if os.path.isdir(inputs_dir):
        for f in sorted(os.listdir(inputs_dir)):
            p = os.path.join(inputs_dir, f)
            if f.lower().endswith(".pdb") and os.path.isfile(p):
                items[os.path.relpath(p, PROJECT_ROOT)] = os.path.getsize(p)
    ws_root = os.path.join(PROJECT_ROOT, "workspaces")
    if os.path.isdir(ws_root):
        for d in sorted(os.listdir(ws_root)):
            dp = os.path.join(ws_root, d)
            if not os.path.isdir(dp):
                continue
            for rel in ("protein.pdb", "ligand.pdb", "complex_rosetta_ready.pdb",
                        os.path.join("cartesian_relax", "wt_relaxed.pdb")):
                p = os.path.join(dp, rel)
                if os.path.isfile(p):
                    items[os.path.relpath(p, PROJECT_ROOT)] = os.path.getsize(p)
    return [{"path": k, "size": v} for k, v in sorted(items.items())]


@app.get("/api/viewer/files")
async def viewer_files():
    """项目内可加载到交互式查看器的 PDB 清单。"""
    return {"files": _scan_viewer_pdbs()}


@app.get("/api/viewer/pdb")
async def viewer_pdb(path: str):
    """按项目内相对路径返回 PDB 文本（供交互式 3D 查看器加载任意结构）。"""
    return FileResponse(_safe_pdb_path(path), media_type="text/plain",
                        filename=os.path.basename(path))


@app.get("/api/system")
async def system_info():
    """服务器资源信息：给前端"最高可用并发"提示用。"""
    return {
        "cpu_cores": os.cpu_count() or 0,
        "pymol_bin": viz_render.PYMOL_BIN,
        "pymol_styles": {k: viz_render.STYLE_NAMES[k] for k in viz_render.STYLE_NAMES},
        "jobs_default": JOBS_DEFAULT,
        "delay_default": DELAY_DEFAULT,
        "delay_explain": ("延迟 = 每提交一个单突变计算后的等待秒数（错峰启动）："
                          "位点数 ×19 个 Rosetta 进程不会瞬时同时拉起，避免挤爆内存/IO。"),
    }


_VIZ_KEEP = 12  # _viz/ 目录最多保留的文件数（自动清理旧的）


def _viz_cleanup():
    try:
        files = [os.path.join(VIZ_DIR, f) for f in os.listdir(VIZ_DIR)]
        files = [f for f in files if os.path.isfile(f)]
        files.sort(key=lambda p: os.path.getmtime(p), reverse=True)
        for p in files[_VIZ_KEEP:]:
            os.remove(p)
    except Exception:
        pass


@app.post("/api/viz/render")
async def viz_render_api(file: UploadFile = None,
                         task_id: str = Form(""),
                         pdb_path: str = Form(""),
                         chain: str = Form(""),
                         resi: str = Form(""),
                         style: str = Form("soft"),
                         mode: str = Form("fast"),
                         rep: str = Form(""),
                         color: str = Form(""),
                         width: int = Form(900),
                         height: int = Form(640)):
    """PyMOL 结构渲染：上传 PDB 或引用已有任务。

    color 非空（spectrum/chain/ssPyMOL/teal）时按 rep×color 动态渲染，
    与交互 3D 面板联动；否则回退到 5 种具名发表级样式。
    mode=fast 秒级预览；mode=ray 光追发表级（大复合物可达数十秒）。
    返回 {"ok", "image"(data URL), "elapsed_s"} 或 {"ok": False, "error"}。
    """
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    src_pdb = None
    if file is not None and file.filename:
        if not file.filename.lower().endswith(".pdb"):
            raise HTTPException(400, "可视化仅支持 .pdb 文件")
        name = re.sub(r"[^A-Za-z0-9._-]", "_", os.path.basename(file.filename))[:60]
        src_pdb = os.path.join(VIZ_DIR, f"{stamp}_{name}")
        with open(src_pdb, "wb") as f:
            shutil.copyfileobj(file.file, f)
    elif pdb_path:
        # 项目内相对路径（交互查看器「项目 PDB」下拉与发表图联动）
        src_pdb = _safe_pdb_path(pdb_path)
    elif task_id:
        workspace = _safe_task_dir(task_id)
        # 优先弛豫后结构（与突变计算同一坐标系），否则用上传的原始结构
        for cand in ("cartesian_relax/wt_relaxed.pdb", "protein.pdb"):
            p = os.path.join(workspace, cand)
            if os.path.exists(p):
                src_pdb = p
                break
        if not src_pdb:
            raise HTTPException(404, "该任务下没有可渲染的 PDB（protein.pdb / wt_relaxed.pdb）")
    else:
        raise HTTPException(400, "请上传 PDB 文件或指定 task_id / pdb_path")

    res = viz_render.render(src_pdb, os.path.join(VIZ_DIR, f"{stamp}_view.png"),
                            style=style, chain=chain, resi=resi,
                            width=width, height=height, ray=(mode == "ray"),
                            rep=rep, color=color)
    _viz_cleanup()
    if not res.get("ok"):
        return {"ok": False, "error": res.get("error", "渲染失败")}
    return {
        "ok": True,
        "image": "data:image/png;base64," + res["image_b64"],
        "elapsed_s": res["elapsed_s"],
        "style": style,
        "site": f"{chain}_{resi}" if (chain and resi) else "",
    }


@app.get("/")
async def index():
    # no-store：首页 HTML 不许缓存，避免浏览器拿着旧 HTML 引用旧版本 app.js/style.css
    return FileResponse(os.path.join(PROJECT_ROOT, "static", "index.html"),
                        headers={"Cache-Control": "no-store"})


app.mount("/static", StaticFiles(directory=os.path.join(PROJECT_ROOT, "static")), name="static")


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("ROSETTA_PORT", "7779"))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning")
