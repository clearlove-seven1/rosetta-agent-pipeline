"""Rosetta-Agent MCP Server

把 rosetta_tools.py 的 16 个原子工具通过 MCP 协议暴露给 Claude Desktop / Cursor / Trae 等
MCP Client，让任意支持 MCP 的 IDE Agent 直接编排三段流水线：

    create_task → parameterize_ligand → run_cartesian_relax
    → predict_disorder → run_saturation_mutagenesis
    → evaluate_against_skempi / evaluate_against_pdbbind

与 rosetta_agent.py（LangGraph 编排）共享同一套底层工具实现，零逻辑重复：
LangGraph 内部用 TaskAwareToolNode 注入工作区，本 Server 用 _apply_workspace() 注入工作区。

运行方式：
    python rosetta_mcp_server.py                                   # stdio（Claude Desktop / Cursor 本地接入）
    python rosetta_mcp_server.py --transport sse --port 8000      # SSE（远程接入）

Claude Desktop（claude_desktop_config.json）/ Cursor（.cursor/mcp.json）配置示例：
    {
      "mcpServers": {
        "rosetta-agent": {
          "command": "/home/xxx/.conda/envs/rosetta-agent/bin/python",
          "args": ["/abs/path/to/rosetta_mcp_server.py"]
        }
      }
    }

依赖：pip install mcp   （官方 SDK，自带 FastMCP Server 实现）
"""
import os
import sys
import glob
import shutil
import argparse
import datetime
import functools
import inspect
import subprocess

# ==========================================
# 0. 固定工作目录到项目根（Client 启动进程的 cwd 不可控）
# ==========================================
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
os.chdir(PROJECT_ROOT)

# 任务运行时公共层（任务 ID/校验/持久状态）
import task_runtime

from dotenv import load_dotenv

load_dotenv(os.path.join(PROJECT_ROOT, ".env"))  # 显式指向项目根的 .env

# 与 rosetta_agent.py 保持一致：把 Rosetta 二进制目录前置到 PATH
_rosetta_bin = os.getenv("ROSETTA_BIN_DIR", "/opt/software/rosetta/main/source/bin")
os.environ["PATH"] = _rosetta_bin + ":" + os.environ.get("PATH", "")

# ==========================================
# 1. 复用 rosetta_tools 的 16 个原子工具（零逻辑重复）
# ==========================================
from rosetta_tools import (
    read_file,
    check_logs,
    get_rosetta_numbering,
    parameterize_ligand,
    run_cartesian_relax,
    run_saturation_mutagenesis,
    diagnose_and_fix,
    auto_repair,
    evaluate_against_skempi,
    evaluate_against_pdbbind,
    predict_disorder,
    search_rosetta_docs,
    score_structure,
    calculate_sasa,
    analyze_interface,
    run_fixbb_design,
    suggest_plan,
    summarize_results,
    run_tmol_scan,
    current_workspace,
)

from mcp.server.fastmcp import FastMCP

mcp = FastMCP(
    "rosetta-agent",
    instructions=(
        "Rosetta 三段流水线 MCP 工具集。典型编排顺序："
        "create_task 创建任务 → parameterize_ligand 参数化（纯蛋白任务会自动跳过）"
        " → run_cartesian_relax 弛豫（后台异步）"
        " → predict_disorder 检查无序区 → run_saturation_mutagenesis 饱和突变（后台异步）"
        " → evaluate_against_skempi / evaluate_against_pdbbind 实验对照。"
        "扩展分析/设计工具：score_structure 打分、calculate_sasa 总溶剂可及面积、"
        "analyze_interface 多链界面指标（dG_separated/dG_cross/dSASA/packstat）、"
        "run_fixbb_design 固定骨架多点设计（后台，完成后 read_file 读 fixbb_design/design_summary.txt）。"
        "弛豫与突变均为后台提交、立即返回：用 list_tasks 查产物状态、check_logs 查日志、read_file 读结果。"
        "【编号铁律】run_saturation_mutagenesis 只接收 PDB 晶体学编号（如 76,73），"
        "内部自动转换为 Rosetta 绝对行号，切勿把 get_rosetta_numbering 的行号回传（双重偏移）。"
    ),
)

# ==========================================
# 2. 工作区管理：按 MCP 会话隔离 + 显式 task_id 优先
# ==========================================
# 2026-11-21 复核修复（取代 11-20 的进程级全局方案）：
#   旧方案缺陷（已用两个真实 MCP 会话交错实测证实）：无参原子工具读取进程级共享
#   _ACTIVE_TASK，SSE/HTTP 模式下客户端 B set_workspace 会劫持客户端 A 的后续调用。
#   新方案：
#   - "当前任务"按 MCP 会话（session_id）隔离：每个工具经 Tool.from_function
#     (context_kwarg='ctx') 注入请求上下文，工具内读取本会话绑定的任务；
#   - 每个原子工具同时保留可选参数 task_id（__signature__ 注入 schema，已实测
#     FastMCP 暴露的 inputSchema 真实包含），单次调用可显式绑定任意任务；
#   - stdio 模式每客户端独立进程，等价天然隔离；无 ctx 的老式调用回退进程级
#     _ACTIVE_TASK（仅 stdio 单会话场景可达）。
#   旧客户端无需调整：create_task → set_workspace → 原子工具 的编排在新语义下
#   于同一会话内行为不变。

_ACTIVE_TASK = {"task_id": None}   # 兼容回退：仅当调用未携带 MCP 会话上下文时使用
_SESSION_TASKS = {}                # 会话隔离核心：session_id -> task_id | None


def _resolve_task_dir(task_id: str):
    """校验 task_id 并返回任务目录绝对路径；非法/不存在返回 None（不抛异常）。"""
    try:
        return task_runtime.validate_task_dir(task_id, base=task_runtime.OUTPUT_BASE)
    except ValueError:
        return None


def _session_key(ctx) -> str | None:
    """从 MCP 请求上下文提取会话标识；拿不到返回 None（回退进程级）。

    SDK 1.29.1 的 Context/ServerSession 没有公开 session_id 属性（已实测），
    但每个客户端会话对应进程内唯一的 ServerSession 实例——同一进程内同时存活
    的会话地址不会复用，故以 id(session) 作为会话键。权衡（如实记录）：
    - 会话断开重连 → 新实例 → 绑定失效，客户端需重新 create_task/set_workspace
      （MCP 会话语义本就如此，且旧的全局共享方案在该场景同样不可靠）；
    - CPython 地址复用发生在旧对象被 GC 之后，极小概率让新会话继承旧绑定，
      等价于旧行为，不会导致"同时活动的两个会话互相串任务"（隔离目标场景）；
    - 条目数 = 历史会话总数，每条几十字节，可忽略；不做清理。
    """
    if ctx is None:
        return None
    sess = getattr(ctx, "session", None)
    if sess is not None:
        return f"sess:{id(sess)}"
    return None


def _current_task_id(ctx=None):
    key = _session_key(ctx)
    if key is not None:
        return _SESSION_TASKS.get(key)
    return _ACTIVE_TASK["task_id"]


def _set_current_task_id(task_id, ctx=None):
    """绑定"当前任务"：有会话上下文 → 只写本会话；无 → 写进程级（stdio 兼容）。"""
    task_id = task_id or None
    key = _session_key(ctx)
    if key is not None:
        _SESSION_TASKS[key] = task_id
    _ACTIVE_TASK["task_id"] = task_id  # 展示层标记（list_tasks "← 当前工作区"）


def _apply_workspace(ctx=None):
    """把本会话（或显式）任务目录注入 current_workspace（与 TaskAwareToolNode 同等效果）"""
    task_id = _current_task_id(ctx)
    if task_id:
        task_dir = _resolve_task_dir(task_id)
        current_workspace.set(task_dir if task_dir else ".")
    else:
        current_workspace.set(".")


def _wrap_tool(lc_tool):
    """包装 LangChain @tool：执行前注入工作区，并为每次调用提供显式 task_id。

    functools.wraps 保留原函数签名与 docstring；再用 __signature__ 追加一个
    keyword-only 的可选参数 task_id（默认空串）——MCP 的 input schema 由该签名
    自动生成，客户端可按调用传 task_id 实现任务绑定，旧客户端不传则作用于
    本会话的"当前任务"（会话隔离，见 _SESSION_TASKS）。
    ctx 参数不进 schema（func_metadata skip_names），由 SDK 在调用时注入。
    """
    fn = lc_tool.func
    orig_sig = inspect.signature(fn)
    new_sig = orig_sig.replace(parameters=list(orig_sig.parameters.values()) + [
        inspect.Parameter("task_id", inspect.Parameter.KEYWORD_ONLY, default="",
                          annotation=str),
    ])

    @functools.wraps(fn)
    def _wrapped(*args, task_id: str = "", ctx=None, **kwargs):
        if task_id:
            task_dir = _resolve_task_dir(task_id)
            if not task_dir:
                return (f"【执行报错】task_id '{task_id}' 非法或任务目录不存在，"
                        "请先用 list_tasks 查看任务列表。")
            current_workspace.set(task_dir)
            return fn(*args, **kwargs)
        _apply_workspace(ctx)
        return fn(*args, **kwargs)

    _wrapped.__signature__ = new_sig
    return _wrapped


def _register_tool(fn, name: str, description: str):
    """显式注册：Tool.from_function(context_kwarg='ctx') 让 SDK 把请求上下文注入
    fn 的 ctx 参数，并从 schema 中排除。依赖私有 _tools 仅作登记，失败时回退
    装饰器注册（退化为进程级共享，并在 stderr 明示）。"""
    try:
        from mcp.server.fastmcp.tools.base import Tool
        tool = Tool.from_function(fn, name=name, description=description,
                                  context_kwarg="ctx")
        mcp._tool_manager._tools[tool.name] = tool
    except Exception as e:  # pragma: no cover
        print(f"[rosetta_mcp] 工具 {name} 显式注册失败({e})，回退装饰器注册"
              "（无按会话隔离，建议传显式 task_id）", file=sys.stderr)
        mcp.tool()(fn)


# 注册原子工具（v0.2: 12 原子 + 4 扩展分析/设计 = 16 个）
_ATOMIC_TOOLS = [
    read_file,
    check_logs,
    get_rosetta_numbering,
    parameterize_ligand,
    run_cartesian_relax,
    run_saturation_mutagenesis,
    diagnose_and_fix,
    auto_repair,
    evaluate_against_skempi,
    evaluate_against_pdbbind,
    predict_disorder,
    search_rosetta_docs,
    score_structure,
    calculate_sasa,
    analyze_interface,
    run_fixbb_design,
    suggest_plan,
    summarize_results,
    run_tmol_scan,
]

for _t in _ATOMIC_TOOLS:
    _register_tool(_wrap_tool(_t), _t.name, _t.description)


# ==========================================
# 3. 管理工具：create_task / list_tasks / set_workspace
#    （普通函数 + 显式注册，ctx 由 SDK 注入、不进 schema；会话内绑定"当前任务"）
# ==========================================
def create_task(protein_pdb_path: str, ligand_path: str = "", task_label: str = "",
                engine: str = "rosetta", ctx=None) -> str:
    """【管理工具】创建新任务工作区并设为本会话当前工作区：把蛋白/配体拷贝到
    output/task_<时间戳+随机后缀>_<label>/（同标签同时间重复创建不会再互相覆盖）；
    未提供配体时自动调 PyMOL 从复合物中分离有机配体。
    创建后即可按 parameterize_ligand → run_cartesian_relax → run_saturation_mutagenesis 编排；
    后续原子工具可显式传 task_id 绑定本任务（多任务交错时推荐），不传则作用于本会话当前工作区。

    Args:
        protein_pdb_path: 受体蛋白/复合物 PDB 路径（绝对路径，或相对项目根的路径）
        ligand_path: 可选配体 (.pdb/.mol2) 路径；留空则用 PyMOL 自动分离
        task_label: 任务名后缀。建议填真实 4 字符 PDB 代码（如 1MC9）——
            任务目录名以 PDB 代码结尾才能触发 SKEMPI / PDBbind 实验对照评估
    """
    protein_pdb_path = (protein_pdb_path or "").strip()
    if not os.path.isabs(protein_pdb_path):
        protein_pdb_path = os.path.join(PROJECT_ROOT, protein_pdb_path)
    if not protein_pdb_path or not os.path.exists(protein_pdb_path):
        return f"【执行报错】未找到蛋白文件：{protein_pdb_path}"

    label = (task_label or "").strip() or os.path.splitext(os.path.basename(protein_pdb_path))[0]
    # P0：标签白名单清洗 + 独占建目录（旧实现为分钟级时间戳+原始标签，可被 ../../ 注入）
    task_id, workspace_dir = task_runtime.create_task_dir(label)
    if engine not in ("rosetta", "tmol"):
        return "【执行报错】engine 仅支持 rosetta 或 tmol。"
    task_runtime.state_write(workspace_dir, status="created", stage="created",
                             manifest={"engine": engine},
                             input_summary={"protein": os.path.basename(protein_pdb_path)[:80],
                                            "ligand": os.path.basename(ligand_path or "")[:80],
                                            "engine": engine,
                                            "entry": "mcp"})

    target_protein = os.path.join(workspace_dir, "protein.pdb")
    if os.path.abspath(protein_pdb_path) != os.path.abspath(target_protein):
        shutil.copy(protein_pdb_path, target_protein)
    notes = ["蛋白已保存为 protein.pdb"]

    ligand_path = (ligand_path or "").strip()
    if ligand_path:
        if not os.path.isabs(ligand_path):
            ligand_path = os.path.join(PROJECT_ROOT, ligand_path)
        if not os.path.exists(ligand_path):
            return f"【执行报错】未找到配体文件：{ligand_path}"
        ext = os.path.splitext(ligand_path)[1].lower()
        if ext not in (".pdb", ".mol2"):
            ext = ".pdb"
        shutil.copy(ligand_path, os.path.join(workspace_dir, f"ligand{ext}"))
        notes.append(f"配体已保存为 ligand{ext}")
    else:
        # 与 rosetta_agent.run_cli 同一策略：仅上传复合物时自动分离有机配体
        pymol_bin = os.getenv("PYMOL_BIN", "/usr/local/bin/pymol")
        target_ligand = os.path.join(workspace_dir, "ligand.pdb")
        pymol_cmds = f"load {target_protein}, complex; save {target_ligand}, complex and organic"
        try:
            subprocess.run(
                [pymol_bin, "-c", "-d", pymol_cmds],
                check=True, capture_output=True, timeout=120,
            )
            if os.path.exists(target_ligand) and os.path.getsize(target_ligand) > 0:
                notes.append("PyMOL 已自动分离配体为 ligand.pdb")
            else:
                notes.append("警告：复合物中未识别到有机配体（纯蛋白体系可跳过参数化）")
        except Exception as e:
            notes.append(f"PyMOL 分离配体失败：{e}")

    _set_current_task_id(task_id, ctx)
    _apply_workspace(ctx)
    return (
        f"【任务已创建】task_id = {task_id}\n"
        f"工作区：output/{task_id}\n"
        + "\n".join(f"  - {n}" for n in notes)
        + "\n下一步：parameterize_ligand → run_cartesian_relax → run_saturation_mutagenesis"
        + "\n（多任务交错时，建议给每个原子工具显式传 task_id 绑定本任务）"
    )


def list_tasks(ctx=None) -> str:
    """【管理工具】列出 output/ 下所有任务目录及各阶段产物状态（参数化/弛豫/突变/汇总/disorder），
    并附 task_status.json 的生效状态（completed/failed/running/interrupted，重启后仍可查询），
    用于配合 set_workspace 选择要操作的任务。"← 当前工作区"按本会话绑定标记。"""
    base = os.path.join(PROJECT_ROOT, "output")
    rows = []
    if os.path.isdir(base):
        for task_id in sorted(os.listdir(base), reverse=True):
            task_dir = os.path.join(base, task_id)
            if not os.path.isdir(task_dir):
                continue

            def _has(*rels):
                return any(os.path.exists(os.path.join(task_dir, r)) for r in rels)

            def _count(*rels):
                return sum(len(glob.glob(os.path.join(task_dir, r))) for r in rels)

            # 主命名 saturation_mutagenesis/summary；同时防御性探测早期文档中的 tool4/tool5 写法
            status = (
                f"params={'Y' if _has('parameterize_ligand/*.params') else '-'} | "
                f"relax={'Y' if _has('cartesian_relax/score.sc') else '-'} | "
                f"ddg={_count('saturation_mutagenesis/*.ddg', 'tool4_mut_results/*.ddg')} | "
                f"summary={'Y' if _has('summary/ddg_results.txt', 'tool5_summary/ddg_results.txt') else '-'} | "
                f"disorder={'Y' if _has('disorder/disorder_scores.csv') else '-'}"
            )
            # P1：持久状态（无状态文件的旧任务显示 status=-）
            eff = task_runtime.state_effective(task_dir)
            st = eff["status"] if eff["state"] else "-"
            active = "  ← 本会话当前工作区" if task_id == _current_task_id(ctx) else ""
            rows.append(f"{task_id}{active}\n    {status} | status={st}"
                        + (f"（{eff['note']}）" if eff.get("note") else ""))
    if not rows:
        return "output/ 下还没有任务。先调用 create_task 创建一个。"
    return f"共 {len(rows)} 个任务（新→旧）：\n" + "\n".join(rows)


def set_workspace(task_id: str, ctx=None) -> str:
    """【管理工具】把后续原子工具的工作区切换到 output/<task_id>（所有相对路径都以它为基准）。
    切换只影响【本会话】：其他 MCP 客户端会话的工作区不受影响。
    参数 task_id 见 list_tasks 输出；传空字符串则重置为项目根目录。
    注意：多任务交错执行时更安全的做法是给每个原子工具显式传 task_id。"""
    task_id = (task_id or "").strip()
    if task_id:
        task_dir = _resolve_task_dir(task_id)
        if not task_dir:
            return (f"【执行报错】任务目录 output/{task_id} 不存在或非法"
                    "（禁止路径分隔符与穿越），请用 list_tasks 查看任务列表。")
        _set_current_task_id(task_id, ctx)
        _apply_workspace(ctx)
        return f"工作区已切换到 output/{task_id}（仅本会话），后续工具将作用于该任务。"
    _set_current_task_id(None, ctx)
    _apply_workspace(ctx)
    return "本会话工作区已重置为项目根目录。"


# 管理工具同样显式注册（ctx 注入 + schema 排除）
_register_tool(create_task, "create_task", create_task.__doc__)
_register_tool(list_tasks, "list_tasks", list_tasks.__doc__)
_register_tool(set_workspace, "set_workspace", set_workspace.__doc__)


# ==========================================
# 4. 入口：stdio（默认）或 sse / streamable-http
# ==========================================
if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Rosetta-Agent MCP Server：向 Claude Desktop / Cursor 等 MCP 客户端暴露 16 个原子工具 + 3 个管理工具"
    )
    parser.add_argument(
        "--transport", choices=["stdio", "sse", "streamable-http"], default="stdio",
        help="传输协议：stdio（Claude Desktop / Cursor 默认） / sse / streamable-http",
    )
    parser.add_argument("--host", default="0.0.0.0", help="网络模式的监听地址")
    parser.add_argument("--port", type=int, default=8000, help="网络模式的监听端口")
    args = parser.parse_args()

    if args.transport == "stdio":
        mcp.run()  # stdio：stdout 是协议通道，此处严禁任何 print 到 stdout
    else:
        mcp.settings.host = args.host
        mcp.settings.port = args.port
        path = "/sse" if args.transport == "sse" else "/mcp"
        print(
            f"Rosetta-Agent MCP Server ({args.transport}) → http://{args.host}:{args.port}{path}",
            file=sys.stderr,
        )
        mcp.run(transport=args.transport)
