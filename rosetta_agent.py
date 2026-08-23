import os
import glob
import shutil
import zipfile
import datetime
import traceback
import time
from dotenv import load_dotenv
import uuid
# 【修复 1】补上了 get_workspace_path 的引入
from rosetta_tools import current_workspace, get_workspace_path 

# .env 配置文件
load_dotenv()
rosetta_bin = os.getenv("ROSETTA_BIN_DIR", "/opt/software/rosetta/main/source/bin")
os.environ["PATH"] = rosetta_bin + ":" + os.environ.get("PATH", "")

from typing import Annotated, Literal, TypedDict

from langchain_openai import ChatOpenAI
from langchain_core.messages import SystemMessage, HumanMessage, ToolMessage
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
    search_rosetta_docs
)

# ==========================================
# 1. 基础配置与模型初始化
# ==========================================
api_key = os.getenv("LLM_API_KEY")

base_url = os.getenv(
    "LLM_BASE_URL",
    "https://api.shubiaobiao.cn/v1"
)

if not api_key:
    raise ValueError(
        "没有找到 LLM_API_KEY，请检查 .env 文件"
    )

llm = ChatOpenAI(
    model=os.getenv("LLM_MODEL", "grok-4-1-fast-non-reasoning"), 
    api_key=os.getenv("LLM_API_KEY", "你的默认API_KEY"), 
    base_url=os.getenv("LLM_BASE_URL", "https://your-llm-provider.example/v1"),
    temperature=0
)

# 注册工具箱并与大模型绑定
tools = [get_rosetta_numbering, parameterize_ligand, run_cartesian_relax, run_saturation_mutagenesis, check_logs, diagnose_and_fix, auto_repair, read_file, search_rosetta_docs]
llm_with_tools = llm.bind_tools(tools)

# ==========================================
# 2. 定义 LangGraph 状态流与节点
# ==========================================
class AgentState(TypedDict):
    messages: Annotated[list, add_messages]

def agent_node(state: AgentState):
    """大模型思考与决策节点"""
    response = llm_with_tools.invoke(state["messages"])
    return {"messages": [response]}

# === 自定义工具节点，强制将 task_id 注入到后台工具的工作线程中 ===
class TaskAwareToolNode(ToolNode):
    def invoke(self, input, config=None, **kwargs):
        # 拦截执行，从配置中强行读取 thread_id (即我们的 task_id) 并注入
        if config and "configurable" in config:
            task_id = config["configurable"].get("thread_id", ".")
            # 【修复】：在这里强制加上 output 前缀，保持与主线程路径一致
            target_dir = os.path.join("output", task_id)
            current_workspace.set(target_dir)
        return super().invoke(input, config=config, **kwargs)
        
    async def ainvoke(self, input, config=None, **kwargs):
        if config and "configurable" in config:
            task_id = config["configurable"].get("thread_id", ".")
            # 【修复】：在这里强制加上 output 前缀
            target_dir = os.path.join("output", task_id)
            current_workspace.set(target_dir)
        return await super().ainvoke(input, config=config, **kwargs)

# 纯工具执行节点 (使用改造后的节点)
tool_node = TaskAwareToolNode(tools)

def force_fix_node(state: AgentState):
    """强制修复节点：拦截错误并注入底层系统指令"""
    force_prompt = (
        "【系统强制指令】上一步的工具执行返回了报错信息。你现在必须遵守以下规则，且**绝对禁止向我提问或确认**：\n"
        "1. 如果提示缺少前置文件（如弛豫构象或参数文件），立即调用 run_cartesian_relax 或 parameterize_ligand 补齐。\n"
        "2. 如果提示解析错误或结果为空，立即调用 diagnose_and_fix 和 auto_repair。\n"
        "现在，立刻直接调用对应的修复工具"
    )
    return {"messages": [HumanMessage(content=force_prompt, name="System_Auto_Director")]}

# ==========================================
# 3. 定义路由逻辑
# ==========================================
def route_after_agent(state: AgentState) -> Literal["tools", "__end__"]:
    messages = state["messages"]
    last_message = messages[-1]
    if last_message.tool_calls:
        return "tools"
    return "__end__"

def route_after_tools(state: AgentState) -> Literal["agent", "force_fix"]:
    messages = state["messages"]
    last_message = messages[-1] 
    
    if isinstance(last_message, ToolMessage):
        if last_message.name == "check_logs":
            time.sleep(30)
            
        content = str(last_message.content)
        
        if "【执行报错】" in content:
            if "【系统异常】" not in content and "崩溃" not in content:
                print("\n [系统拦截] 检测到工具报错，已触发强制自动修复流...\n")
                return "force_fix"
                
    return "agent"

# ==========================================
# 4. 构建与编译状态图
# ==========================================
workflow = StateGraph(AgentState)

workflow.add_node("agent", agent_node)
workflow.add_node("tools", tool_node)
workflow.add_node("force_fix", force_fix_node)

workflow.add_edge(START, "agent")
workflow.add_conditional_edges("agent", route_after_agent)
workflow.add_conditional_edges(
    "tools", 
    route_after_tools, 
    {
        "agent": "agent",
        "force_fix": "force_fix"
    }
)
workflow.add_edge("force_fix", "agent") 

memory = MemorySaver()
agent_executor = workflow.compile(checkpointer=memory)

# ==========================================
# 5. 后台打包处理逻辑
# ==========================================
def package_results(task_id):
    if not task_id:
        return None, "❌ 找不到任务目标，请先提交执行一次突变任务。"
        
    # 重定向到 output 目录查找
    task_dir = os.path.join("output", task_id)
    summary_file = os.path.join(task_dir, "tool5_summary", "ddg_results.txt")
    mut_dir = os.path.join(task_dir, "tool4_mut_results")
    
    if not os.path.exists(summary_file) and not os.path.exists(mut_dir):
        return None, f" 未在 {task_dir} 下找到计算结果文件。"
    
    if not os.path.exists(summary_file):
        ddg_files = glob.glob(f"{mut_dir}/*.ddg") if os.path.exists(mut_dir) else []
        return None, f"⏳ {task_id} 后台计算仍在进行中 (当前已生成 {len(ddg_files)} 个部分突变结果)..."
    
    try:
        zip_filename = f"{task_id}_results.zip"
        
        with zipfile.ZipFile(zip_filename, 'w', zipfile.ZIP_DEFLATED) as zipf:
            # 遍历并打包指定 task_dir 目录下的对应结果
            for folder_name in ["tool5_summary", "tool4_mut_results"]:
                target_folder = os.path.join(task_dir, folder_name)
                if os.path.exists(target_folder):
                    for root, _, files in os.walk(target_folder):
                        for file in files:
                            file_path = os.path.join(root, file)
                            # 保持压缩包内的相对目录结构整洁 (去掉 output 前缀)
                            arcname = os.path.relpath(file_path, "output")
                            zipf.write(file_path, arcname)
                            
        return zip_filename, f"✅ 打包完成：{zip_filename}，请点击下载。"
    except Exception as e:
        return None, f"❌ 打包失败: {str(e)}"
# ==========================================
# 6. UI 主题与交互任务逻辑
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

# 在参数最后增加 current_task_state 追踪状态
def submit_task(protein_file, ligand_file, mutation_mode, mutation, params, user_msg, history, error_box, current_task_state):
    mutation = mutation or ""
    params = params or ""
    user_msg = user_msg or ""
    history = history or []
    error_box = error_box or ""

    try:
        # ================= 新增修改部分开始 =================
        # 1. 尝试从上传的蛋白质文件中提取文件名（去掉后缀），比如 "1abc"
        protein_id = "unknown"
        if protein_file is not None:
            # 兼容 Gradio 的不同文件对象格式
            file_path = getattr(protein_file, "filepath", getattr(protein_file, "name", str(protein_file)))
            protein_id = os.path.splitext(os.path.basename(file_path))[0]
            
        # 2. 拼接时间戳和蛋白质 ID，生成新的 task_id
        task_id = f"task_{datetime.datetime.now().strftime('%Y%m%d_%H%M')}_{protein_id}"
        
        # 3. 将最终的工作区目录指向 output 文件夹下的这个 task_id
        workspace_dir = os.path.join("output", task_id)
        current_workspace.set(workspace_dir)
        
        # 4. 强制创建该目录，防止底层的工具报错找不到路径
        os.makedirs(workspace_dir, exist_ok=True)
        # ================= 新增修改部分结束 =================
        
        prompt_parts = []
        
        if protein_file is not None:
            # 拷贝蛋白复合物
            file_path = getattr(protein_file, "filepath", getattr(protein_file, "name", str(protein_file)))
            target_protein = os.path.join(workspace_dir, "protein.pdb")
            shutil.copy(file_path, target_protein)
            prompt_parts.append("蛋白信息: protein.pdb")
            
        # ==== 新增：如果在网页端只传了复合物（没传配体），依据 CLI 逻辑自动调用 PyMOL 分离 ====
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
                    prompt_parts.append(f"【系统强制规则】配体已自动保存为 {target_ligand_name}。第一步必须调用 parameterize_ligand(ligand_path='{target_ligand_name}')。")
            except Exception as e:
                print(f"❌ PyMOL 提取配体失败: {str(e)}")
                
        # ==== 原有逻辑：如果用户在网页端手动传了单独的配体文件 ====
        elif ligand_file is not None:
            lig_path = getattr(ligand_file, "filepath", getattr(ligand_file, "name", str(ligand_file)))
            ext = os.path.splitext(lig_path)[1].lower()
            if ext not in [".pdb", ".mol2"]: ext = ".pdb"
            target_ligand = os.path.join(workspace_dir, f"ligand{ext}")
            shutil.copy(lig_path, target_ligand)
            prompt_parts.append(f"【系统强制规则】配体已保存。你必须第一步调用 parameterize_ligand(ligand_path='ligand{ext}')")

# ... 后面的 if mutation.strip(): 等代码保持不变 ...
                
        if mutation.strip(): 
            if mutation_mode == "多位点连续突变 (批量)":
                prompt_parts.append(
                    f"【系统强制批量流水线】当前为多位点任务，目标位点：[{mutation}]。\n"
                    "你必须严格按顺序、不间断地生成工具调用（Tool Call），中间绝不允许停顿：\n"
                    "1. 必须先调用 parameterize_ligand。\n"
                    "2. 必须紧接着调用 run_cartesian_relax。\n"
                    "3. 必须直接将位点 [{mutation}] 传给 run_saturation_mutagenesis！\n"
                    "【红线警告】：\n"
                    "1. 绝对禁止调用 check_logs 或 get_rosetta_numbering 工具！\n"
                    "2. 在没有看到 run_saturation_mutagenesis 工具返回成功结果前，绝对禁止输出任何纯文本回复！如果你输出纯文本，流程就会中断，算你严重失职！\n"
                    "3. 只有成功执行完第三步，你才能回复总结并结束对话。"
                )
            else:
                prompt_parts.append(f"突变位点: {mutation}")
                
        if params.strip(): prompt_parts.append(f"其他参数: {params}")
        if user_msg.strip(): prompt_parts.append(f"指令: {user_msg}")
        
        final_prompt = " | ".join(prompt_parts)
        if not final_prompt:
            yield history, "", error_box
            return

        config = {"configurable": {"thread_id": task_id}}
        
        # 【修复 3.1】改用字典格式初始化消息
        history.append({"role": "user", "content": final_prompt})
        history.append({"role": "assistant", "content": "⏳ 任务已提交，底层执行中..."})
        yield history, "", error_box, task_id
        
# rosetta_agent.py 大约第 215 行 和 265 行的 sys_rule 替换为：
        sys_rule = (
            "你是一个严格遵守物理流水线的高级计算生物学 Agent。\n"
            "【最高绝密规则】：收到任务后，你必须按顺序调用完以下三个工具！\n"
            "第一步：调用 parameterize_ligand。\n"
            "第二步：必须等待第一步返回成功后，才能调用 run_cartesian_relax。\n"
            "第三步：必须等待第二步返回成功后，将用户输入的【PDB 晶体学编号】原样传给 run_saturation_mutagenesis！\n"
            "【编号体系铁律】：run_saturation_mutagenesis 接收的是【PDB 晶体学编号】（如 76,73），工具内部会自动完成到 Rosetta 绝对行号的转换。\n"
            "get_rosetta_numbering 仅用于人工核对映射（只读，不修改任何文件）；核对后仍必须传回原始 PDB 编号，绝对禁止把它的绝对行号输出再传给 run_saturation_mutagenesis，否则会导致双重偏移错位！\n"
            "【绝对禁令】：每次回复【只能调用一个工具】！绝对禁止一次性输出多个工具调用！"
        )
        
        for event in agent_executor.stream({
            "messages": [
                SystemMessage(content=sys_rule), 
                HumanMessage(content=final_prompt)
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
                            # 【修复 3.2】字典更新格式
                            history[-1]["content"] += f"\n\n✅ 执行成功: {last_msg.name}"
                        yield history, "", error_box, task_id
                        
                    elif last_msg.type == "human" and getattr(last_msg, "name", "") == "System_Auto_Director":
                        # 【修复 3.3】字典更新格式
                        history[-1]["content"] += "\n\n⚠️ 触发拦截: Agent 正在尝试自动补齐或修复..."
                        yield history, "", error_box, task_id
                        
                    elif last_msg.type == "ai" and last_msg.content:
                        # 【修复 3.4】字典更新格式
                        history[-1]["content"] += f"\n\n🤖 Agent: {last_msg.content}"
                        yield history, "", error_box, task_id
                        
    except Exception as e:
        error_detail = traceback.format_exc()
        error_box = f"❌ 致命崩溃: {str(e)}\n\n完整堆栈:\n{error_detail}"
        yield history, "", error_box, task_id
    
    return history, "", error_box, task_id

def run_cli(protein_file, ligand_file, mutation, params, user_msg):
    """专门为 CLI 模式设计的控制台执行器"""
    print(f"\n 开始构建底层计算任务...")
    
    protein_id = "unknown"
    if protein_file:
        protein_id = os.path.splitext(os.path.basename(protein_file))[0]
        
    task_id = f"task_{datetime.datetime.now().strftime('%Y%m%d_%H%M')}_{protein_id}"
    workspace_dir = os.path.join("output", task_id)
    current_workspace.set(workspace_dir)
    os.makedirs(workspace_dir, exist_ok=True)
    # =======================
    print(f" 独立任务目录已创建: {workspace_dir}")
    
    prompt_parts = []
    
# === 修复：安全复制蛋白文件，防止 SameFileError ===
    if protein_file: 
        target_protein = os.path.join(workspace_dir, "protein.pdb")
        if os.path.abspath(protein_file) != os.path.abspath(target_protein):
            shutil.copy(protein_file, target_protein)
        prompt_parts.append("蛋白信息: protein.pdb")
        
    # === 新增：直接调用 PyMOL 自动化分离配体 ===
    if not ligand_file and protein_file:
        import subprocess
        target_ligand_name = "ligand.pdb"
        target_ligand = os.path.join(workspace_dir, target_ligand_name)
        
        # 读取环境变量中的 PyMOL 路径，默认 fallback 到 /usr/local/bin/pymol
        pymol_bin = os.getenv("PYMOL_BIN", "/usr/local/bin/pymol")
        
        # 组合 PyMOL 命令：加载蛋白，精准提取有机物配体 (自动排除水和离子) 并保存
        pymol_cmds = f"load {target_protein}, complex; save {target_ligand}, complex and organic"
        cmd = f"{pymol_bin} -c -d '{pymol_cmds}'"
        
        try:
            # 静默运行 PyMOL 控制台指令
            subprocess.run(cmd, shell=True, check=True, capture_output=True)
            
            # 检查 PyMOL 是否成功输出了非空的配体文件
            if os.path.exists(target_ligand) and os.path.getsize(target_ligand) > 0:
                print(" 系统提示: 已通过 PyMOL 自动分离并提取配体坐标至 ligand.pdb")
                prompt_parts.append(f"【系统强制规则】配体已自动保存为 {target_ligand_name}。第一步必须调用 parameterize_ligand(ligand_path='{target_ligand_name}')。")
            else:
                print(" 警告: PyMOL 运行结束，但在结构中未识别到有效配体。")
        except Exception as e:
            print(f" PyMOL 提取配体失败: {str(e)}")
            
    # === 原有逻辑：供手动指定单独配体文件时使用 ===
    elif ligand_file:
        ext = os.path.splitext(ligand_file)[1].lower()
        if ext not in [".pdb", ".mol2"]: ext = ".pdb"
        target_ligand_name = f"ligand{ext}"
        target_ligand = os.path.join(workspace_dir, target_ligand_name)
        
        if os.path.abspath(ligand_file) != os.path.abspath(target_ligand):
            shutil.copy(ligand_file, target_ligand)
        prompt_parts.append(f"【系统强制规则】配体已保存为 {target_ligand_name}。第一步必须调用 parameterize_ligand(ligand_path='{target_ligand_name}')。")
        
    if mutation: 
        mutation = mutation.replace("，", ",")
        prompt_parts.append(
            f"目标突变位点: {mutation}。\n"
            "【红线警告】：\n"
            "1. 你必须严格按顺序、不间断地生成工具调用。\n"
            "2. 除非上一步明确返回了【执行报错】，否则绝对禁止在 tool3 成功前输出纯文本！\n"
            "3. 只有在 tool3 成功的前提下，才允许调用 tool4（run_saturation_mutagenesis）！\n"
            "4. 传给 tool4 的必须是【PDB 晶体学编号】（如 76,73），工具内部会自动转换为 Rosetta 绝对行号；\n"
            "   绝对禁止把 get_rosetta_numbering 返回的绝对行号再传给 tool4，否则会导致双重偏移错位。\n"
            "5. 如果 tool3 报错，请直接输出错误原因并结束。"
        )
    if params: prompt_parts.append(f"其他参数: {params}")
    if user_msg: prompt_parts.append(f"指令: {user_msg}")
    
    final_prompt = " | ".join(prompt_parts)
    print(f"📝 注入的系统指令: {final_prompt}\n")
    print("-" * 50)
    
    sys_rule = (
        "你是一个严格遵守物理流水线的高级计算生物学 Agent。\n"
        "【最高绝密规则】：收到任务后，你必须按顺序调用完以下三个工具！\n"
        "第一步：调用 parameterize_ligand。\n"
        "第二步：必须等待第一步返回成功后，才能调用 run_cartesian_relax。\n"
        "第三步：必须等待第二步返回成功后，将用户输入的【PDB 晶体学编号】原样传给 run_saturation_mutagenesis！\n"
        "【编号体系铁律】：run_saturation_mutagenesis 接收的是【PDB 晶体学编号】（如 76,73），工具内部会自动完成到 Rosetta 绝对行号的转换。\n"
        "get_rosetta_numbering 仅用于人工核对映射（只读，不修改任何文件）；核对后仍必须传回原始 PDB 编号，绝对禁止把它的绝对行号输出再传给 run_saturation_mutagenesis，否则会导致双重偏移错位！\n"
        "【绝对禁令】：每次回复【只能调用一个工具】！绝对禁止一次性输出多个工具调用！"
    )
    
    config = {"configurable": {"thread_id": task_id}} # 线程 ID 也跟着任务走
    
    try:
        # 使用大模型并流式打印结果到终端
        for event in agent_executor.stream(
            {"messages": [SystemMessage(content=sys_rule), HumanMessage(content=final_prompt)]}, 
            config, 
            stream_mode="updates"
        ):
            for node_name, node_state in event.items():
                if "messages" in node_state:
                    last_msg = node_state["messages"][-1]
                    
                    if last_msg.type == "tool":
                        content = str(last_msg.content)
                        if "【执行报错】" in content or "【系统异常】" in content:
                            print(f"❌ 底层报错 ({last_msg.name}):\n{content}")
                        else:
                            print(f"✅ 执行成功: {last_msg.name}")
                            
                    elif last_msg.type == "human" and getattr(last_msg, "name", "") == "System_Auto_Director":
                        print("🚨 触发拦截: Agent 正在尝试自动补齐或修复...")
                        
                    elif last_msg.type == "ai" and last_msg.content:
                        print(f"\n🤖 Agent回复: {last_msg.content}\n")
                        
    except Exception as e:
        print(f"❌ 运行崩溃: {str(e)}")
# ==========================================
# 7. 页面构建与启动
# ==========================================
with gr.Blocks() as demo:
    gr.Markdown("## 🧬 Rosetta Agent 专业控制台")
    
    with gr.Row():
        with gr.Column(scale=1):
            gr.Markdown("### ⚙️ 任务参数")
            protein_input = gr.File(label="上传受体蛋白 (.pdb, 需与配体同一坐标系)", file_types=[".pdb"])
            ligand_input = gr.File(label="上传预对接配体 (.pdb, 保持原位三维坐标)", file_types=[".pdb", ".mol2"])
            
            mutation_mode = gr.Radio(
                choices=["单位点突变(普通) ", "多位点连续突变 (批量)"], 
                value="单位点突变 (普通)", 
                label=" 执行模式"
            )
            
            mutation_input = gr.Textbox(label="目标突变位点", placeholder="单位点填: 76\n多位点填: 1, 2, 3, 11, A_23 (用逗号分隔)", lines=2)
            params_input = gr.Textbox(label="其他关键参数", placeholder="例如: 并发数 10", lines=2)
            
        with gr.Column(scale=2):
            # 这里的 gr.Chatbot 已恢复默认配置
            chatbot = gr.Chatbot(label="Agent 执行日志", height=380)
            error_box = gr.Textbox(label=" 状态与报错", lines=3, interactive=False)
            
            with gr.Row():
                user_input = gr.Textbox(label="附加指令 (可选)", placeholder="例如: 开始全饱和突变", scale=4)
                submit_btn = gr.Button("🚀 提交执行", variant="primary", scale=1)
            
            gr.Markdown("---")
            gr.Markdown("### 📦 结果获取")
            with gr.Row():
                fetch_btn = gr.Button(" 刷新并打包下载结果", variant="secondary", scale=1)
                download_file = gr.File(label="突变结果压缩包 (.zip)", interactive=False, scale=2)

# 定义一个不可见的 State 变量用于保存当前任务 ID
    task_state = gr.State("")

    # ===把 task_state 塞进下面的 inputs 和 outputs 列表里 ===
    submit_btn.click(
        fn=submit_task,
        inputs=[protein_input, ligand_input, mutation_mode, mutation_input, params_input, user_input, chatbot, error_box, task_state],
        outputs=[chatbot, user_input, error_box, task_state]
    )
    user_input.submit(
        fn=submit_task,
        inputs=[protein_input, ligand_input, mutation_mode, mutation_input, params_input, user_input, chatbot, error_box, task_state],
        outputs=[chatbot, user_input, error_box, task_state]
    )
    fetch_btn.click(
        fn=package_results,
        inputs=[task_state],  #  这里必须加上 inputs，把存好的 task_id 传给打包函数
        outputs=[download_file, error_box]
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
            print("❌ 错误：CLI 模式下，必须提供 --protein 和 --mutation 参数！")
            print("💡 示例：python rosetta_agent.py --mode cli --protein test.pdb --mutation 76")
            exit(1)
        run_cli(args.protein, args.ligand, args.mutation, args.params, args.msg)
        
    else:
        print("🌐 正在启动网页控制台...")
        demo.queue().launch(server_name="0.0.0.0", server_port=7777, share=False, theme=custom_theme)