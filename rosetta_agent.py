import os
import glob
import shutil
import zipfile
import datetime
import traceback
import time
from dotenv import load_dotenv
import uuid

from rosetta_tools import current_workspace, get_workspace_path

# .env 配置文件
load_dotenv()
rosetta_bin = os.getenv("ROSETTA_BIN_DIR", "/opt/software/rosetta/main/source/bin")
os.environ["PATH"] = rosetta_bin + ":" + os.environ.get("PATH", "")

from typing import Annotated, Literal, TypedDict, Callable

from langchain_openai import ChatOpenAI
from langchain_core.messages import SystemMessage, HumanMessage, AIMessage, ToolMessage
from langgraph.graph import StateGraph, START, END
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode
from langgraph.checkpoint.memory import MemorySaver
import gradio as gr

from rosetta_tools import (
    read_file,
    get_rosetta_numbering,
    parameterize_ligand,
    run_cartesian_relax,
    run_saturation_mutagenesis,
    check_logs,
    diagnose_and_fix,
    auto_repair,
    search_rosetta_docs,
    evaluate_against_skempi,
    evaluate_against_pdbbind,
    predict_disorder,
    predict_disorder_struct,
    DISORDER_THRESHOLD,
)

# ==========================================
# 1. 基础配置与模型初始化
# ==========================================
api_key = os.getenv("LLM_API_KEY")
base_url = os.getenv("LLM_BASE_URL", "https://api.shubiaobiao.cn/v1")

if not api_key:
    raise ValueError("没有找到 LLM_API_KEY，请检查 .env 文件")

llm = ChatOpenAI(
    model=os.getenv("LLM_MODEL", "grok-4-1-fast-non-reasoning"),
    api_key=api_key,
    base_url=base_url,
    temperature=0,
)

# ==========================================
# 2. 工具分组：把"绝对禁止调用"从文本约束变成 schema 层的物理约束
# ==========================================
# 主图 reply 节点可用的通用检索/读取工具
GENERAL_TOOLS = [search_rosetta_docs, read_file]

# 子图 repair 节点专用的诊断/修复工具
REPAIR_TOOLS = [check_logs, diagnose_and_fix, auto_repair]

# 流水线三段式：每个 step 只能 bind_tools 自己那一步的工具
STEP1_TOOLS = [parameterize_ligand]
STEP2_TOOLS = [run_cartesian_relax]
# step3 允许 LLM 在提交前先核对映射（只读），但只能选用这两个之一
STEP3_TOOLS = [run_saturation_mutagenesis, get_rosetta_numbering]

MAX_RETRIES = 2  # 单个 step 最多重试次数


# ==========================================
# 3. 任务感知工具节点（保持原有线程隔离行为）
# ==========================================
MAX_TOOL_RESULT_CHARS = 16000  # tool 结果超过这个长度会被首尾截断


def _truncate_tool_result(content: str, max_chars: int) -> str:
    """超过 max_chars 时保留首尾各一半，中间用省略号标记"""
    if len(content) <= max_chars:
        return content
    half = max_chars // 2
    return f"{content[:half]}\n\n... [已截断 {len(content) - max_chars} 字符] ...\n\n{content[-half:]}"


class TaskAwareToolNode(ToolNode):
    """继承 ToolNode：拦截执行，强行读取 thread_id（即 task_id）并注入工作区上下文；
    同时对超长 tool 结果做首尾截断，避免炸 context window"""

    def invoke(self, input, config=None, **kwargs):
        if config and "configurable" in config:
            task_id = config["configurable"].get("thread_id", ".")
            target_dir = os.path.join("output", task_id)
            current_workspace.set(target_dir)
        result = super().invoke(input, config=config, **kwargs)
        self._truncate_messages(result)
        return result

    async def ainvoke(self, input, config=None, **kwargs):
        if config and "configurable" in config:
            task_id = config["configurable"].get("thread_id", ".")
            target_dir = os.path.join("output", task_id)
            current_workspace.set(target_dir)
        result = await super().ainvoke(input, config=config, **kwargs)
        self._truncate_messages(result)
        return result

    @staticmethod
    def _truncate_messages(result):
        """对 ToolMessage.content 超过阈值的做首尾截断，中间用省略号标记"""
        for m in result.get("messages", []):
            if isinstance(m, ToolMessage):
                content = str(m.content)
                if len(content) > MAX_TOOL_RESULT_CHARS:
                    m.content = _truncate_tool_result(content, MAX_TOOL_RESULT_CHARS)


# ==========================================
# 4. 主图状态定义：把"顺序/权限/恢复/收尾"职责编码到状态字段里
# ==========================================
class AgentState(TypedDict):
    """主图与子图共享的状态

    - messages         : 对话历史（add_messages 自动合并）
    - intent           : router 节点分类结果：pipeline / query / chat
    - pipeline_step    : 当前所处的流水线阶段（0=未启动, 1=parameterize, 2=relax, 3=mutagenesis, 4=轮询进度）
    - failed_step      : 最近一次失败的步骤编号，供 repair 回路决定回到哪一步
    - retry_count      : 当前失败步骤已重试次数，达到 MAX_RETRIES 强制退出
    - progress_polls   : progress_poll 节点自循环计数（达到 MAX_PROGRESS_POLLS 强制退出）
    - progress_done    : progress_poll 检测到后台任务完成的标记
    - disordered_sites : ESM-2 disorder ≥ 阈值 的位点（["A_76", ...]），供 step3 警告注入
    - disorder_scores  : 全量 disorder 分数 {chain: {resnum: score}}，落盘 CSV 同源
    """
    messages: Annotated[list, add_messages]
    intent: str
    pipeline_step: int
    failed_step: int
    retry_count: int
    progress_polls: int
    progress_done: bool
    disordered_sites: list
    disorder_scores: dict


# ==========================================
# 5. 公共辅助函数
# ==========================================
def _is_tool_error(msg) -> bool:
    """统一判定某条 ToolMessage 是否为报错"""
    if not isinstance(msg, ToolMessage):
        return False
    content = str(msg.content)
    return ("【执行报错】" in content) or ("【系统异常】" in content) or ("【执行失败】" in content)


def _llm_has_tool_call(state: AgentState) -> Literal["tool", "__next__"]:
    """通用 LLM 节点出口：必须产生 tool_call，否则视为异常直接退出"""
    last = state["messages"][-1]
    if isinstance(last, AIMessage) and last.tool_calls:
        return "tool"
    return "__next__"


# ==========================================
# 6. 节点工厂：每个 step 的 LLM / Tool 节点都由 schema 层硬约束权限
# ==========================================
def _make_step_llm_node(step_num: int, system_prompt: str, bound_tools):
    """工厂：生成 stepX_llm 节点

    该 LLM 已 bind_tools(bound_tools)，只能调用本步骤允许的工具；
    这是 schema 层的物理约束，不再依赖 prompt 中的"绝对禁止"文本。
    """
    step_llm = llm.bind_tools(bound_tools)

    def _node(state: AgentState):
        # 进入该步骤时记录 pipeline_step，便于 UI / 调试观察
        response = step_llm.invoke(
            [SystemMessage(content=system_prompt)] + list(state["messages"])
        )
        return {"messages": [response], "pipeline_step": step_num}

    _node.__name__ = f"step{step_num}_llm_node"
    return _node


def _make_step_tool_node(step_num: int, bound_tools):
    """工厂：生成 stepX_tool 节点

    工具执行后：
      - 成功：不修改 pipeline_step（由下一个 step_llm 写入）
      - 失败：写入 failed_step 与 retry_count，供后续 repair 回路路由使用
    """
    inner = TaskAwareToolNode(bound_tools)

    def _node(state: AgentState, config=None, **kwargs):
        result = inner.invoke(state, config=config, **kwargs)
        new_msgs = result.get("messages", [])
        update = {"messages": new_msgs}
        last = new_msgs[-1] if new_msgs else None
        if last and _is_tool_error(last):
            update["failed_step"] = step_num
            update["retry_count"] = state.get("retry_count", 0) + 1
        return update

    _node.__name__ = f"step{step_num}_tool_node"
    return _node


def _post_tool_route(next_node: str) -> Callable[[AgentState], str]:
    """工厂：生成 step_tool 节点出口的条件边

    add_conditional_edges 在每个 tool 出口触发，成功 -> next_node，失败 -> repair
    """

    def _route(state: AgentState):
        last = state["messages"][-1]
        if _is_tool_error(last):
            # 错误交给 repair 分支（取代原 force_fix prompt 注入）
            return "repair"
        return next_node

    return _route


# 进度轮询参数：MAX_PROGRESS_POLLS * PROGRESS_INTERVAL = 30 分钟（默认）
MAX_PROGRESS_POLLS = 60
PROGRESS_INTERVAL = 30  # 秒


def progress_poll_node(state: AgentState, config=None, **kwargs):
    """进度轮询节点：纯确定性，不调 LLM，读目录查后台任务状态

    - 完成后 ddg_results.txt 会被 auto_clean.py 写入 → 返回 __end__
    - 中途持续轮询 .ddg 数量，PROGRESS_INTERVAL 秒一次
    - 达到 MAX_PROGRESS_POLLS 上限强制退出（让用户去 fetch 按钮手动下载）
    """
    if config and "configurable" in config:
        task_id = config["configurable"].get("thread_id", ".")
        workspace = os.path.join("output", task_id)
    else:
        workspace = current_workspace.get()

    mut_dir = os.path.join(workspace, "saturation_mutagenesis")
    sum_dir = os.path.join(workspace, "summary")

    ddg_count = len(glob.glob(f"{mut_dir}/*.ddg")) if os.path.exists(mut_dir) else 0
    summary_file = os.path.join(sum_dir, "ddg_results.txt")
    summary_exists = os.path.exists(summary_file)

    polls = state.get("progress_polls", 0) + 1

    if summary_exists:
        progress_text = (
            f"✅ 后台计算全部完成！已生成 ddg_results.txt，"
            f"共 {ddg_count} 个 .ddg 文件，可下载结果包"
        )
        done = True
    elif ddg_count > 0:
        progress_text = f"⏳ 后台计算中… 第 {polls} 次轮询，已完成 {ddg_count} 个 .ddg"
        done = False
    else:
        progress_text = f"⏳ 后台计算准备中… 第 {polls} 次轮询"
        done = False

    time.sleep(PROGRESS_INTERVAL)

    msg = HumanMessage(
        content=progress_text,
        name="ProgressReporter",
        id="progress_poll_status",  # 固定 id：add_messages reducer 自动覆盖，避免 60 条轮询消息累积
    )

    return {
        "messages": [msg],
        "progress_polls": polls,
        "progress_done": done,
        "pipeline_step": 4,
    }


def progress_poll_route(state: AgentState) -> Literal["progress_poll", "__end__"]:
    """progress_poll 节点出口：自循环直到完成或超限"""
    if state.get("progress_done"):
        return "__end__"
    if state.get("progress_polls", 0) >= MAX_PROGRESS_POLLS:
        return "__end__"
    return "progress_poll"


# ==========================================
# 7. 流水线子图：参数化→弛豫→突变，三段硬编码顺序
# ==========================================
STEP1_SYSTEM = (
    "你是【参数化阶段】执行 Agent。"
    "任务：调用 parameterize_ligand 处理配体文件（ligand.pdb 或 ligand.mol2）。"
    "权限：你只能调用系统为你绑定的工具；调用完成后请直接结束本轮。"
    "若工具返回【执行报错】，系统会自动进入修复分支，不要自行尝试别的工具。"
)
STEP2_SYSTEM = (
    "你是【弛豫阶段】执行 Agent。"
    "任务：调用 run_cartesian_relax 对清洗后的复合物结构进行 Cartesian 弛豫。"
    "参数：本步骤固定 nstruct=3；严禁读取或使用用户参数中的「并发数/延迟」字段——那是给 STEP3 饱和突变阶段用的，本阶段一律不参考。"
    "权限：你只能调用系统为你绑定的工具；调用完成后请直接结束本轮。"
    "若工具返回【执行报错】，系统会自动进入修复分支。"
)
STEP3_SYSTEM = (
    "你是【饱和突变阶段】执行 Agent。"
    "任务：调用 run_saturation_mutagenesis 提交高通量突变任务。"
    "【编号铁律】必须把用户原始输入的【PDB 晶体学编号】原样传入，工具内部会自动转换为 Rosetta 绝对行号。"
    "权限：你只能调用 run_saturation_mutagenesis 或 get_rosetta_numbering；"
    "get_rosetta_numbering 只用于核对映射（只读），核对后仍必须把【原始 PDB 编号】传给 run_saturation_mutagenesis，"
    "绝对禁止把它的绝对行号输出再传给 run_saturation_mutagenesis，否则会导致双重偏移错位！"
    "调用完成后请直接结束本轮。"
)


def build_pipeline_subgraph():
    """构建"参数化→弛豫→突变"三段式子图

    图拓扑（核心职责：把"顺序/权限/恢复"全部从 prompt 移到图结构）：

        START ──► step1_llm ──(tool_call)──► step1_tool ──(成功)──► step2_llm ──(tool_call)──► step2_tool ──(成功)──► step3_llm ──(tool_call)──► step3_tool ──(成功)──► END
                    │                            │                            │                            │                            │
                    │(无tool_call→异常退出)      │(失败→add_conditional_edges)│(失败→repair)              │(失败→repair)              │(失败→repair)
                    ▼                            ▼                            ▼                            ▼                            ▼
                  END                          repair ◄─────────────────── repair ◄────────────────── repair ◄──────────────────────
                                                  │                              │
                                              (tool_call)                    (无tool_call→放弃)
                                                  ▼                              ▼
                                              repair_tools                    END
                                                  │
                                              (按 failed_step 路由回对应 stepX_llm 重试)
                                                  │
                                            (retry_count ≥ MAX_RETRIES → END)
    """
    sg = StateGraph(AgentState)

    # ----- Step 1：参数化 -----
    sg.add_node("step1_llm", _make_step_llm_node(1, STEP1_SYSTEM, STEP1_TOOLS))
    sg.add_node("step1_tool", _make_step_tool_node(1, STEP1_TOOLS))
    # ----- Step 2：弛豫 -----
    sg.add_node("step2_llm", _make_step_llm_node(2, STEP2_SYSTEM, STEP2_TOOLS))
    sg.add_node("step2_tool", _make_step_tool_node(2, STEP2_TOOLS))
    # ----- Step 3：饱和突变（允许先核对编号）-----
    sg.add_node("step3_llm", _make_step_llm_node(3, STEP3_SYSTEM, STEP3_TOOLS))
    sg.add_node("step3_tool", _make_step_tool_node(3, STEP3_TOOLS))

    # ----- Repair 分支 -----
    repair_llm = llm.bind_tools(REPAIR_TOOLS)

    def repair_node(state: AgentState):
        """修复节点：诊断当前失败原因，但不决定重试目标（由 add_conditional_edges 决定）"""
        sys_prompt = (
            "你是【自动修复 Agent】。当前流水线某一步刚报错，你的任务是："
            "调用合适的诊断/修复工具（check_logs / diagnose_and_fix / auto_repair）定位问题，然后直接给出修复建议。"
            "严禁重复触发同一报错工具；如确实无法自动恢复，请明确告知放弃。"
        )
        response = repair_llm.invoke(
            [SystemMessage(content=sys_prompt)] + list(state["messages"])
        )
        return {"messages": [response]}

    repair_node.__name__ = "repair_node"

    def repair_route(state: AgentState) -> Literal["repair_tools", "__end__"]:
        last = state["messages"][-1]
        if isinstance(last, AIMessage) and last.tool_calls:
            return "repair_tools"
        return "__end__"

    def repair_back_route(state: AgentState):
        """repair_tools 出口：retry_count 超限 → 放弃；否则回到失败的那一步"""
        if state.get("retry_count", 0) > MAX_RETRIES:
            return "__end__"
        return {
            1: "step1_llm",
            2: "step2_llm",
            3: "step3_llm",
        }.get(state.get("failed_step", 1), "__end__")

    sg.add_node("repair", repair_node)
    sg.add_node("repair_tools", TaskAwareToolNode(REPAIR_TOOLS))

    # ===== 连边：把"顺序"显式写进图拓扑 =====
    sg.add_edge(START, "step1_llm")

    # step1 闭环
    sg.add_conditional_edges(
        "step1_llm",
        _llm_has_tool_call,
        {"tool": "step1_tool", "__next__": "__end__"},
    )
    sg.add_conditional_edges(
        "step1_tool",
        _post_tool_route("step2_llm"),
        {"step2_llm": "step2_llm", "repair": "repair"},
    )

    # step2 闭环
    sg.add_conditional_edges(
        "step2_llm",
        _llm_has_tool_call,
        {"tool": "step2_tool", "__next__": "__end__"},
    )
    sg.add_conditional_edges(
        "step2_tool",
        _post_tool_route("disorder_check"),
        {"disorder_check": "disorder_check", "repair": "repair"},
    )

    # disorder_check 节点：ESM-2 给每个残基打 disorder 分，结果写到 state
    def disorder_check_node(state: AgentState, config=None, **kwargs):
        """确定性节点：调 predict_disorder.func()（人读报告）+ predict_disorder_struct()（机器读 + 落盘 CSV）

        落盘位置：<workspace>/disorder/disorder_scores.csv
        写回 state：disordered_sites (list)、disorder_scores (dict)
        """
        try:
            report = predict_disorder.func()
            scores = predict_disorder_struct()
        except Exception as e:
            report = f"【disorder 检测】执行出错: {e}"
            scores = {}

        sites = [
            f"{c}_{r}"
            for c, d in scores.items()
            for r, s in d.items()
            if s >= DISORDER_THRESHOLD
        ]
        msg = HumanMessage(content=f"🧬 {report}", name="DisorderDirector")
        return {
            "messages": [msg],
            "disordered_sites": sites,
            "disorder_scores": scores,
        }

    disorder_check_node.__name__ = "disorder_check_node"
    sg.add_node("disorder_check", disorder_check_node)
    sg.add_edge("disorder_check", "step3_llm")  # disorder 后直接进 step3_llm

    # step3 闭环
    sg.add_conditional_edges(
        "step3_llm",
        _llm_has_tool_call,
        {"tool": "step3_tool", "__next__": "__end__"},
    )
    sg.add_conditional_edges(
        "step3_tool",
        _post_tool_route("progress_poll"),
        {"progress_poll": "progress_poll", "repair": "repair"},
    )

    # 进度轮询闭环：自循环直到 ddg_results.txt 出现或超过 MAX_PROGRESS_POLLS
    sg.add_node("progress_poll", progress_poll_node)
    sg.add_conditional_edges(
        "progress_poll",
        progress_poll_route,
        {"progress_poll": "progress_poll", "__end__": "__end__"},
    )

    # repair 闭环（取代原 force_fix prompt 注入）
    sg.add_conditional_edges(
        "repair",
        repair_route,
        {"repair_tools": "repair_tools", "__end__": "__end__"},
    )
    sg.add_conditional_edges(
        "repair_tools",
        repair_back_route,
        {
            "step1_llm": "step1_llm",
            "step2_llm": "step2_llm",
            "step3_llm": "step3_llm",
            "__end__": "__end__",
        },
    )

    return sg.compile()


# ==========================================
# 8. 主图节点：router / reply / summarize
# ==========================================
INTENT_SYSTEM = (
    "你是任务意图路由器。根据用户最新输入，只回复以下三种之一，不要输出其他内容：\n"
    "  pipeline —— 用户提供了蛋白+配体并明确要求执行 Rosetta 流水线（出现突变位点、复合物等关键词）\n"
    "  query    —— 用户在询问 Rosetta 文档、参数、报错含义（属于知识检索）\n"
    "  chat     —— 问候、寒暄或与本系统无关的话题"
)


def router_node(state: AgentState) -> AgentState:
    """router 节点：意图分类（pipeline / query / chat）"""
    if state.get("intent"):
        return {}

    msgs = state["messages"]
    user_msgs = [m.content for m in msgs if isinstance(m, HumanMessage)]
    last_user = user_msgs[-1] if user_msgs else ""

    # 启发式快速判定：系统提示里带"突变位点/系统强制规则"等关键字直接走 pipeline
    pipeline_markers = ["突变位点", "目标突变位点", "【系统强制规则】", "【系统强制批量流水线】"]
    if any(m in str(last_user) for m in pipeline_markers):
        return {"intent": "pipeline"}

    # 其余走 LLM 分类
    try:
        resp = llm.invoke([
            SystemMessage(content=INTENT_SYSTEM),
            HumanMessage(content=str(last_user)),
        ])
        intent = (resp.content or "").strip().lower()
    except Exception:
        intent = "chat"

    if intent not in ("pipeline", "query", "chat"):
        intent = "chat"
    return {"intent": intent}


def main_route_after_router(state: AgentState) -> Literal["pipeline", "reply"]:
    """router 后的主图分支：pipeline 走子图，其余走 reply"""
    return "pipeline" if state.get("intent") == "pipeline" else "reply"


REPLY_SYSTEM = (
    "你是 Rosetta 知识助手。"
    "若用户询问文档或报错，可调用 search_rosetta_docs / read_file 检索；"
    "若属于普通寒暄，直接用自然语言回复即可。"
    "查询完毕后请直接给出最终答复，不要无限循环调用工具。"
)

reply_llm = llm.bind_tools(GENERAL_TOOLS)


def reply_node(state: AgentState):
    response = reply_llm.invoke([
        SystemMessage(content=REPLY_SYSTEM),
        *state["messages"],
    ])
    return {"messages": [response]}


def reply_route(state: AgentState) -> Literal["reply_tools", "summarize"]:
    last = state["messages"][-1]
    if isinstance(last, AIMessage) and last.tool_calls:
        return "reply_tools"
    return "summarize"


reply_tools_node = TaskAwareToolNode(GENERAL_TOOLS)


# ==========================================
# 9. summarize 节点：统一收尾（取代原"自由文本总结"依赖 prompt）
# ==========================================
SUMMARIZE_SYSTEM = (
    "你是【统一收尾 Agent】。基于历史消息向用户输出最终答复：\n"
    "  - 若执行了完整流水线：依次说明参数化→弛豫→突变三步的结果，并提示后台计算仍在进行。\n"
    "  - 若属于查询/对话：用一两句话总结答案。\n"
    "回复必须使用中文，简明扼要，禁止再调用任何工具，禁止追问。"
)


def summarize_node(state: AgentState):
    """summarize 节点：bind_tools([]) 在 schema 层物理上禁止再调用工具"""
    summarize_llm = llm.bind_tools([])
    response = summarize_llm.invoke([
        SystemMessage(content=SUMMARIZE_SYSTEM),
        *state["messages"],
    ])
    return {"messages": [response]}


# ==========================================
# 9.5 skempi_eval 节点：拿 SKEMPI 2.0 实验数据评估本批次预测
# ==========================================
def skempi_eval_node(state: AgentState, config=None, **kwargs):
    """实验对照节点（确定性，不调 LLM）：SKEMPI 蛋白-蛋白 + PDBbind 蛋白-小分子

    1. 从 config.thread_id 解析 PDB ID
    2. 调 evaluate_against_skempi.func(pdb_id, ddg_path) → 蛋白-蛋白 ΔΔG vs 实验 ΔΔG Pearson
    3. 调 evaluate_against_pdbbind.func(pdb_ids, score_paths) → 蛋白-小分子 Rosetta score vs 实验 Kd Spearman
    4. 把两份报告合并为 HumanMessage(name="BindingDirector") 追加，并落盘 summary/binding_eval_report.txt
    """
    if not config or "configurable" not in config:
        msg = HumanMessage(content="【实验对照】无 task_id，跳过评估", name="BindingDirector")
        return {"messages": [msg]}

    task_id = config["configurable"].get("thread_id", "")
    parts = task_id.split("_")
    protein_id = "_".join(parts[3:]) if len(parts) > 3 else ""
    if not protein_id:
        msg = HumanMessage(content="【实验对照】未从 task_id 解析到 PDB ID，跳过", name="BindingDirector")
        return {"messages": [msg]}

    workspace = os.path.join("output", task_id)
    ddg_path = os.path.join(workspace, "summary", "ddg_results.txt")
    sc_path = os.path.join(workspace, "cartesian_relax", "score.sc")

    reports = []

    # 1. SKEMPI（蛋白-蛋白 ΔΔG）
    if os.path.exists(ddg_path):
        try:
            skempi_report = evaluate_against_skempi.func(pdb_id=protein_id, ddg_results_path=ddg_path)
            reports.append(skempi_report)
        except Exception as e:
            reports.append(f"【蛋白-蛋白对照】执行出错: {e}")
    else:
        reports.append(f"【蛋白-蛋白对照】{ddg_path} 不存在，跳过 SKEMPI 评估（未跑完饱和突变？）")

    # 2. PDBbind（蛋白-小分子 Kd）
    if os.path.exists(sc_path):
        try:
            pdbbind_report = evaluate_against_pdbbind.func(pdb_ids=protein_id, score_paths=sc_path)
            reports.append(pdbbind_report)
        except Exception as e:
            reports.append(f"【蛋白-小分子对照】执行出错: {e}")
    else:
        reports.append(f"【蛋白-小分子对照】{sc_path} 不存在，跳过 PDBbind 评估（未跑完弛豫？）")

    combined = "\n\n" + "─" * 60 + "\n\n".join(reports)

    # 报告落盘到 summary/binding_eval_report.txt（随结果 zip 一起交付）
    try:
        report_dir = os.path.join(workspace, "summary")
        os.makedirs(report_dir, exist_ok=True)
        with open(os.path.join(report_dir, "binding_eval_report.txt"), "w", encoding="utf-8") as f:
            f.write(combined.strip())
    except Exception:
        pass  # 落盘失败不影响主流程

    msg = HumanMessage(content=f"📊 {combined}", name="BindingDirector")
    return {"messages": [msg]}


# ==========================================
# 10. 构建与编译主图
# ==========================================
workflow = StateGraph(AgentState)

workflow.add_node("router", router_node)
workflow.add_node("pipeline", build_pipeline_subgraph())
workflow.add_node("reply", reply_node)
workflow.add_node("reply_tools", reply_tools_node)
workflow.add_node("skempi_eval", skempi_eval_node)
workflow.add_node("summarize", summarize_node)

workflow.add_edge(START, "router")
workflow.add_conditional_edges(
    "router",
    main_route_after_router,
    {"pipeline": "pipeline", "reply": "reply"},
)
# 流水线子图出口（无论正常完成还是失败放弃）都进入 SKEMPI 实验对照，再统一收尾
workflow.add_edge("pipeline", "skempi_eval")
workflow.add_edge("skempi_eval", "summarize")
# reply 子图：reply_llm -> [reply_tools | summarize]
workflow.add_conditional_edges(
    "reply",
    reply_route,
    {"reply_tools": "reply_tools", "summarize": "summarize"},
)
workflow.add_edge("reply_tools", "reply")
workflow.add_edge("summarize", END)


memory = MemorySaver()
agent_executor = workflow.compile(checkpointer=memory)


# ==========================================
# 11. 后台打包处理逻辑
# ==========================================
def package_results(task_id):
    if not task_id:
        return None, "❌ 找不到任务目标，请先提交执行一次突变任务。"

    task_dir = os.path.join("output", task_id)
    summary_file = os.path.join(task_dir, "summary", "ddg_results.txt")
    mut_dir = os.path.join(task_dir, "saturation_mutagenesis")

    if not os.path.exists(summary_file) and not os.path.exists(mut_dir):
        return None, f" 未在 {task_dir} 下找到计算结果文件。"

    if not os.path.exists(summary_file):
        ddg_files = glob.glob(f"{mut_dir}/*.ddg") if os.path.exists(mut_dir) else []
        return None, f"⏳ {task_id} 后台计算仍在进行中 (当前已生成 {len(ddg_files)} 个部分突变结果)..."

    try:
        zip_filename = f"{task_id}_results.zip"
        with zipfile.ZipFile(zip_filename, "w", zipfile.ZIP_DEFLATED) as zipf:
            for folder_name in ["summary", "saturation_mutagenesis", "disorder"]:
                target_folder = os.path.join(task_dir, folder_name)
                if os.path.exists(target_folder):
                    for root, _, files in os.walk(target_folder):
                        for file in files:
                            file_path = os.path.join(root, file)
                            arcname = os.path.relpath(file_path, "output")
                            zipf.write(file_path, arcname)
        return zip_filename, f"✅ 打包完成：{zip_filename}，请点击下载。"
    except Exception as e:
        return None, f"❌ 打包失败: {str(e)}"


# ==========================================
# 12. UI 主题
# ==========================================
custom_theme = gr.themes.Soft(
    primary_hue=gr.themes.colors.blue,
    secondary_hue=gr.themes.colors.indigo,
    neutral_hue=gr.themes.colors.slate,
).set(
    body_background_fill="*neutral_50",
    block_background_fill="white",
    block_border_width="0px",
    block_shadow="*shadow_drop_lg",
    button_primary_background_fill="*primary_600",
    button_primary_background_fill_hover="*primary_700",
    button_primary_text_color="white",
)


# ==========================================
# 13. 任务提交逻辑：sys_rule 大幅精简（顺序/禁令已下沉到图结构）
# ==========================================
def submit_task(protein_file, ligand_file, mutation_mode, mutation, params, user_msg, history, error_box, current_task_state):
    mutation = mutation or ""
    params = params or ""
    user_msg = user_msg or ""
    history = history or []
    error_box = error_box or ""

    try:
        protein_id = "unknown"
        if protein_file is not None:
            file_path = getattr(protein_file, "filepath", getattr(protein_file, "name", str(protein_file)))
            protein_id = os.path.splitext(os.path.basename(file_path))[0]

        task_id = f"task_{datetime.datetime.now().strftime('%Y%m%d_%H%M')}_{protein_id}"
        workspace_dir = os.path.join("output", task_id)
        current_workspace.set(workspace_dir)
        os.makedirs(workspace_dir, exist_ok=True)

        prompt_parts = []

        if protein_file is not None:
            file_path = getattr(protein_file, "filepath", getattr(protein_file, "name", str(protein_file)))
            target_protein = os.path.join(workspace_dir, "protein.pdb")
            shutil.copy(file_path, target_protein)
            prompt_parts.append("蛋白信息: protein.pdb")

        # 仅上传复合物时自动调 PyMOL 分离配体
        if ligand_file is None and protein_file is not None:
            import subprocess
            target_ligand_name = "ligand.pdb"
            target_ligand = os.path.join(workspace_dir, target_ligand_name)
            pymol_bin = os.getenv("PYMOL_BIN", "/usr/local/bin/pymol")
            pymol_cmds = f"load {target_protein}, complex; save {target_ligand}, complex and organic"
            cmd = f"{pymol_bin} -c -d '{pymol_cmds}'"
            try:
                subprocess.run(cmd, shell=True, check=True, capture_output=True)
                if os.path.exists(target_ligand) and os.path.getsize(target_ligand) > 0:
                    prompt_parts.append(f"【系统强制规则】配体已自动保存为 {target_ligand_name}。")
            except Exception as e:
                print(f"❌ PyMOL 提取配体失败: {str(e)}")
        elif ligand_file is not None:
            lig_path = getattr(ligand_file, "filepath", getattr(ligand_file, "name", str(ligand_file)))
            ext = os.path.splitext(lig_path)[1].lower()
            if ext not in [".pdb", ".mol2"]:
                ext = ".pdb"
            target_ligand = os.path.join(workspace_dir, f"ligand{ext}")
            shutil.copy(lig_path, target_ligand)
            prompt_parts.append(f"【系统强制规则】配体已保存。")

        if mutation.strip():
            if mutation_mode == "多位点连续突变 (批量)":
                prompt_parts.append(f"【系统强制批量流水线】当前为多位点任务，目标位点：[{mutation}]。")
            else:
                prompt_parts.append(f"突变位点: {mutation}")

        if params.strip():
            prompt_parts.append(f"其他参数: {params}")
        if user_msg.strip():
            prompt_parts.append(f"指令: {user_msg}")

        final_prompt = " | ".join(prompt_parts)
        if not final_prompt:
            yield history, "", error_box
            return

        config = {"configurable": {"thread_id": task_id}}

        history.append({"role": "user", "content": final_prompt})
        history.append({"role": "assistant", "content": "⏳ 任务已提交，底层执行中..."})
        yield history, "", error_box, task_id

        # sys_rule 大幅精简：顺序/权限/恢复已由图结构强制，无需文本约束
        sys_rule = (
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

        for event in agent_executor.stream({
            "messages": [
                SystemMessage(content=sys_rule),
                HumanMessage(content=final_prompt),
            ]
        }, config, stream_mode="updates"):
            for node_name, node_state in event.items():
                if "messages" in node_state:
                    last_msg = node_state["messages"][-1]
                    if last_msg.type == "tool":
                        content = str(last_msg.content)
                        if "【执行报错】" in content or "【系统异常】" in content or "【执行失败】" in content:
                            error_box = f"❌ 底层报错 ({last_msg.name}):\n{content}"
                        else:
                            history[-1]["content"] += f"\n\n✅ 执行成功: {last_msg.name}"
                        yield history, "", error_box, task_id
                    elif last_msg.type == "ai" and last_msg.content:
                        history[-1]["content"] += f"\n\n🤖 Agent: {last_msg.content}"
                        yield history, "", error_box, task_id
                    elif last_msg.type == "human" and getattr(last_msg, "name", "") == "ProgressReporter":
                        # 进度轮询节点播报：增量追加到历史，避免刷屏
                        history[-1]["content"] += f"\n\n{last_msg.content}"
                        yield history, "", error_box, task_id
                    elif last_msg.type == "human" and getattr(last_msg, "name", "") == "BindingDirector":
                        # 实验对照报告（SKEMPI + PDBbind 合并）：单独一段展示
                        history[-1]["content"] += f"\n\n{last_msg.content}"
                        yield history, "", error_box, task_id
                    elif last_msg.type == "human" and getattr(last_msg, "name", "") == "DisorderDirector":
                        # ESM-2 disorder 检测报告：单独一段展示
                        history[-1]["content"] += f"\n\n{last_msg.content}"
                        yield history, "", error_box, task_id

    except Exception as e:
        error_detail = traceback.format_exc()
        error_box = f"❌ 致命崩溃: {str(e)}\n\n完整堆栈:\n{error_detail}"
        yield history, "", error_box, task_id

    return history, "", error_box, task_id


def run_cli(protein_file, ligand_file, mutation, params, user_msg):
    """CLI 模式执行器"""
    print(f"\n 开始构建底层计算任务...")

    protein_id = "unknown"
    if protein_file:
        protein_id = os.path.splitext(os.path.basename(protein_file))[0]

    task_id = f"task_{datetime.datetime.now().strftime('%Y%m%d_%H%M')}_{protein_id}"
    workspace_dir = os.path.join("output", task_id)
    current_workspace.set(workspace_dir)
    os.makedirs(workspace_dir, exist_ok=True)
    print(f" 独立任务目录已创建: {workspace_dir}")

    prompt_parts = []

    if protein_file:
        target_protein = os.path.join(workspace_dir, "protein.pdb")
        if os.path.abspath(protein_file) != os.path.abspath(target_protein):
            shutil.copy(protein_file, target_protein)
        prompt_parts.append("蛋白信息: protein.pdb")

    if not ligand_file and protein_file:
        import subprocess
        target_ligand_name = "ligand.pdb"
        target_ligand = os.path.join(workspace_dir, target_ligand_name)
        pymol_bin = os.getenv("PYMOL_BIN", "/usr/local/bin/pymol")
        pymol_cmds = f"load {target_protein}, complex; save {target_ligand}, complex and organic"
        cmd = f"{pymol_bin} -c -d '{pymol_cmds}'"
        try:
            subprocess.run(cmd, shell=True, check=True, capture_output=True)
            if os.path.exists(target_ligand) and os.path.getsize(target_ligand) > 0:
                print(" 系统提示: 已通过 PyMOL 自动分离并提取配体坐标至 ligand.pdb")
                prompt_parts.append(f"【系统强制规则】配体已自动保存为 {target_ligand_name}。")
            else:
                print(" 警告: PyMOL 运行结束，但在结构中未识别到有效配体。")
        except Exception as e:
            print(f" PyMOL 提取配体失败: {str(e)}")
    elif ligand_file:
        ext = os.path.splitext(ligand_file)[1].lower()
        if ext not in [".pdb", ".mol2"]:
            ext = ".pdb"
        target_ligand_name = f"ligand{ext}"
        target_ligand = os.path.join(workspace_dir, target_ligand_name)
        if os.path.abspath(ligand_file) != os.path.abspath(target_ligand):
            shutil.copy(ligand_file, target_ligand)
        prompt_parts.append(f"【系统强制规则】配体已保存为 {target_ligand_name}。")

    if mutation:
        mutation = mutation.replace("，", ",")
        prompt_parts.append(f"目标突变位点: {mutation}。")
    if params:
        prompt_parts.append(f"其他参数: {params}")
    if user_msg:
        prompt_parts.append(f"指令: {user_msg}")

    final_prompt = " | ".join(prompt_parts)
    print(f" 注入的系统指令: {final_prompt}\n")
    print("-" * 50)

    # sys_rule 同样精简
    sys_rule = (
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

    config = {"configurable": {"thread_id": task_id}}

    try:
        for event in agent_executor.stream(
            {"messages": [SystemMessage(content=sys_rule), HumanMessage(content=final_prompt)]},
            config,
            stream_mode="updates",
        ):
            for node_name, node_state in event.items():
                if "messages" in node_state:
                    last_msg = node_state["messages"][-1]
                    if last_msg.type == "tool":
                        content = str(last_msg.content)
                        if "【执行报错】" in content or "【系统异常】" in content:
                            print(f" 底层报错 ({last_msg.name}):\n{content}")
                        else:
                            print(f"✅ 执行成功: {last_msg.name}")
                    elif last_msg.type == "ai" and last_msg.content:
                        print(f"\n Agent回复: {last_msg.content}\n")
                    elif last_msg.type == "human" and getattr(last_msg, "name", "") == "ProgressReporter":
                        print(f"\n{last_msg.content}\n")
                    elif last_msg.type == "human" and getattr(last_msg, "name", "") == "BindingDirector":
                        print(f"\n{last_msg.content}\n")
                    elif last_msg.type == "human" and getattr(last_msg, "name", "") == "DisorderDirector":
                        print(f"\n{last_msg.content}\n")
    except Exception as e:
        print(f" 运行崩溃: {str(e)}")


# ==========================================
# 14. Gradio 页面构建
# ==========================================
with gr.Blocks() as demo:
    gr.Markdown("##  Rosetta Agent 控制台")

    with gr.Row():
        with gr.Column(scale=1):
            gr.Markdown("###  任务参数")
            protein_input = gr.File(label="上传受体蛋白 (.pdb, 需与配体同一坐标系)", file_types=[".pdb"])
            ligand_input = gr.File(label="上传预对接配体 (.pdb, 保持原位三维坐标)", file_types=[".pdb", ".mol2"])

            mutation_mode = gr.Radio(
                choices=["单位点突变(普通) ", "多位点连续突变 (批量)"],
                value="单位点突变 (普通)",
                label=" 执行模式",
            )

            mutation_input = gr.Textbox(label="目标突变位点", placeholder="单位点填: 76\n多位点填: 1, 2, 3, 11, A_23 (用逗号分隔)", lines=2)
            params_input = gr.Textbox(label="其他关键参数", placeholder="例如: 并发数 10", lines=2)

        with gr.Column(scale=2):
            chatbot = gr.Chatbot(label="Agent 执行日志", height=380)
            error_box = gr.Textbox(label=" 状态与报错", lines=3, interactive=False)

            with gr.Row():
                user_input = gr.Textbox(label="附加指令 (可选)", placeholder="例如: 开始全饱和突变", scale=4)
                submit_btn = gr.Button(" 提交执行", variant="primary", scale=1)

            gr.Markdown("---")
            gr.Markdown("###  结果获取")
            with gr.Row():
                fetch_btn = gr.Button(" 刷新并打包下载结果", variant="secondary", scale=1)
                download_file = gr.File(label="突变结果压缩包 (.zip)", interactive=False, scale=2)

    task_state = gr.State("")

    submit_btn.click(
        fn=submit_task,
        inputs=[protein_input, ligand_input, mutation_mode, mutation_input, params_input, user_input, chatbot, error_box, task_state],
        outputs=[chatbot, user_input, error_box, task_state],
    )
    user_input.submit(
        fn=submit_task,
        inputs=[protein_input, ligand_input, mutation_mode, mutation_input, params_input, user_input, chatbot, error_box, task_state],
        outputs=[chatbot, user_input, error_box, task_state],
    )
    fetch_btn.click(
        fn=package_results,
        inputs=[task_state],
        outputs=[download_file, error_box],
    )


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Rosetta Agent 控制台")
    parser.add_argument("--mode", choices=["web", "cli"], default="web", help="运行模式：web(默认) 或 cli")
    parser.add_argument("--protein", type=str, help="CLI模式必填: 蛋白质文件路径 (如 ./1abc.pdb)")
    parser.add_argument("--ligand", type=str, help="CLI模式选填: 配体文件路径")
    parser.add_argument("--mutation", type=str, help="CLI模式必填: 突变位点 (如 76)")
    parser.add_argument("--params", type=str, default="", help="CLI模式选填: 额外参数")
    parser.add_argument("--msg", type=str, default="", help="CLI模式选填: 附加自然语言指令")

    args = parser.parse_args()

    if args.mode == "cli":
        if not args.protein or not args.mutation:
            print(" 错误：CLI 模式下，必须提供 --protein 和 --mutation 参数！")
            print(" 示例: python rosetta_agent.py --mode cli --protein protein.pdb --mutation 76")
            exit(1)
        run_cli(args.protein, args.ligand, args.mutation, args.params, args.msg)
    else:
        print(" 正在启动网页控制台...")
        demo.queue().launch(server_name="0.0.0.0", server_port=7777, share=False, theme=custom_theme)