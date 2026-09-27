"""Rosetta-Agent MCP Server

把 rosetta_tools.py 的 12 个原子工具通过 MCP 协议暴露给 Claude Desktop / Cursor / Trae 等
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
import subprocess

# ==========================================
# 0. 固定工作目录到项目根（Client 启动进程的 cwd 不可控）
# ==========================================
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
os.chdir(PROJECT_ROOT)

from dotenv import load_dotenv

load_dotenv(os.path.join(PROJECT_ROOT, ".env"))  # 显式指向项目根的 .env

# 与 rosetta_agent.py 保持一致：把 Rosetta 二进制目录前置到 PATH
_rosetta_bin = os.getenv("ROSETTA_BIN_DIR", "/opt/software/rosetta/main/source/bin")
os.environ["PATH"] = _rosetta_bin + ":" + os.environ.get("PATH", "")

# ==========================================
# 1. 复用 rosetta_tools 的 12 个原子工具（零逻辑重复）
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
    current_workspace,
)

from mcp.server.fastmcp import FastMCP

mcp = FastMCP(
    "rosetta-agent",
    instructions=(
        "Rosetta 三段流水线 MCP 工具集。典型编排顺序："
        "create_task 创建任务 → parameterize_ligand 参数化 → run_cartesian_relax 弛豫（后台异步）"
        " → predict_disorder 检查无序区 → run_saturation_mutagenesis 饱和突变（后台异步）"
        " → evaluate_against_skempi / evaluate_against_pdbbind 实验对照。"
        "弛豫与突变均为后台提交、立即返回：用 list_tasks 查产物状态、check_logs 查日志、read_file 读结果。"
        "【编号铁律】run_saturation_mutagenesis 只接收 PDB 晶体学编号（如 76,73），"
        "内部自动转换为 Rosetta 绝对行号，切勿把 get_rosetta_numbering 的行号回传（双重偏移）。"
    ),
)

# ==========================================
# 2. 工作区管理：进程级全局 + 每次调用前重注入
# ==========================================
# MCP Server 的每次工具调用运行在独立 async context 中，contextvar 在单次调用内 set
# 未必延续到下一次调用。因此"当前任务"存进程级全局，
# 由 _apply_workspace() 在每次工具执行前重新注入 contextvar。

_ACTIVE_TASK = {"task_id": None}


def _apply_workspace():
    """把 _ACTIVE_TASK 对应的任务目录注入 current_workspace（与 TaskAwareToolNode 同等效果）"""
    task_id = _ACTIVE_TASK["task_id"]
    if task_id:
        current_workspace.set(os.path.join("output", task_id))
    else:
        current_workspace.set(".")


def _wrap_tool(lc_tool):
    """包装 LangChain @tool：执行前注入工作区。

    functools.wraps 保留原函数签名与 docstring，
    MCP 的 input schema / 工具描述即由它自动生成，无需手工维护两套定义。
    """
    fn = lc_tool.func

    @functools.wraps(fn)
    def _wrapped(*args, **kwargs):
        _apply_workspace()
        return fn(*args, **kwargs)

    return _wrapped


# 注册 12 个原子工具
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
]

for _t in _ATOMIC_TOOLS:
    mcp.tool()(_wrap_tool(_t))


# ==========================================
# 3. 管理工具：create_task / list_tasks / set_workspace
# ==========================================
@mcp.tool()
def create_task(protein_pdb_path: str, ligand_path: str = "", task_label: str = "") -> str:
    """【管理工具】创建新任务工作区并设为当前工作区：把蛋白/配体拷贝到
    output/task_<时间戳>_<label>/；未提供配体时自动调 PyMOL 从复合物中分离有机配体。
    创建后即可按 parameterize_ligand → run_cartesian_relax → run_saturation_mutagenesis 编排。

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
    task_id = f"task_{datetime.datetime.now().strftime('%Y%m%d_%H%M')}_{label}"
    workspace_dir = os.path.join(PROJECT_ROOT, "output", task_id)
    os.makedirs(workspace_dir, exist_ok=True)

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
                f"{pymol_bin} -c -d '{pymol_cmds}'",
                shell=True, check=True, capture_output=True,
            )
            if os.path.exists(target_ligand) and os.path.getsize(target_ligand) > 0:
                notes.append("PyMOL 已自动分离配体为 ligand.pdb")
            else:
                notes.append("警告：复合物中未识别到有机配体（纯蛋白体系可跳过参数化）")
        except Exception as e:
            notes.append(f"PyMOL 分离配体失败：{e}")

    _ACTIVE_TASK["task_id"] = task_id
    _apply_workspace()
    return (
        f"【任务已创建】task_id = {task_id}\n"
        f"工作区：output/{task_id}\n"
        + "\n".join(f"  - {n}" for n in notes)
        + "\n下一步：parameterize_ligand → run_cartesian_relax → run_saturation_mutagenesis"
    )


@mcp.tool()
def list_tasks() -> str:
    """【管理工具】列出 output/ 下所有任务目录及各阶段产物状态（参数化/弛豫/突变/汇总/disorder），用于配合 set_workspace 选择要操作的任务。"""
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
            active = "  ← 当前工作区" if task_id == _ACTIVE_TASK["task_id"] else ""
            rows.append(f"{task_id}{active}\n    {status}")
    if not rows:
        return "output/ 下还没有任务。先调用 create_task 创建一个。"
    return f"共 {len(rows)} 个任务（新→旧）：\n" + "\n".join(rows)


@mcp.tool()
def set_workspace(task_id: str) -> str:
    """【管理工具】把后续 12 个原子工具的工作区切换到 output/<task_id>（所有相对路径都以它为基准）。
    参数 task_id 见 list_tasks 输出；传空字符串则重置为项目根目录。"""
    task_id = (task_id or "").strip()
    if task_id and not os.path.isdir(os.path.join(PROJECT_ROOT, "output", task_id)):
        return f"【执行报错】任务目录 output/{task_id} 不存在，请用 list_tasks 查看任务列表。"
    _ACTIVE_TASK["task_id"] = task_id or None
    _apply_workspace()
    if task_id:
        return f"工作区已切换到 output/{task_id}，后续工具将作用于该任务。"
    return "工作区已重置为项目根目录。"


# ==========================================
# 4. 入口：stdio（默认）或 sse / streamable-http
# ==========================================
if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Rosetta-Agent MCP Server：向 Claude Desktop / Cursor 等 MCP 客户端暴露 12 个原子工具 + 3 个管理工具"
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
