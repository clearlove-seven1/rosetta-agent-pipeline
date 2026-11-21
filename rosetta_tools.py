import os
import re
import sys
import glob
import json
import shutil
import time
import subprocess
import contextvars
from dotenv import load_dotenv

# 任务运行时公共层（任务 ID/路径校验/位点解析/持久状态）——2026-11-20 收敛到唯一实现
import task_runtime
from task_runtime import (
    AA_3TO1 as _AA_3TO1,  # 兼容旧引用：编号表本体已迁至 task_runtime
    build_numbering as _build_numbering_impl,
    split_tokens as _split_tokens,
    resolve_targets as _resolve_targets,
    precheck_targets as _shared_precheck,
    create_task_dir as _create_task_dir,
    state_write, state_effective, set_manifest,
)

from langchain_core.tools import tool
from langchain_openai import OpenAIEmbeddings
from langchain_community.vectorstores import Chroma
from langchain_community.document_loaders import TextLoader
from langchain_text_splitters import RecursiveCharacterTextSplitter

# ==========================================
# 0. 环境配置与独立工作区管理
# ==========================================
# 加载 .env 配置文件（无需再修改源码即可适配不同服务器）
load_dotenv()

ROSETTA_BIN = os.getenv("ROSETTA_BIN_DIR", "/opt/software/rosetta/main/source/bin")
ROSETTA_SCRIPTS = os.getenv("ROSETTA_SCRIPTS_DIR", "/opt/software/rosetta/main/source/scripts/python/public")
PYMOL_BIN = os.getenv("PYMOL_BIN", "/usr/local/bin/pymol")

# 线程安全的上下文变量，确保多用户并发时各自目录物理隔离
current_workspace = contextvars.ContextVar('current_workspace', default='.')


# ==========================================
# 0.5 科学口径开关（v0.1 科学纠偏）
# 默认全部关闭：本项目的 ESM-2 分数与跨库相关性评级均未经验证，
# 不得作为默认结论输出。需要时在 .env / 环境变量中显式打开，并自行承担解释责任。
# ==========================================
def _env_flag(name: str) -> bool:
    """读取 0/1 开关型环境变量，缺省视为关闭。"""
    return os.getenv(name, "0").strip().lower() in ("1", "true", "yes", "on")


def exploratory_eval_enabled() -> bool:
    """SKEMPI/PDBbind 自动相关性计算与评级（探索性）：默认关闭。

    关闭时对照工具只输出原始数据表与「未评测」状态，不计算相关系数、不给结论。
    """
    return _env_flag("WB_EXPLORATORY_EVAL_ENABLED")


def disorder_filter_enabled() -> bool:
    """ESM-2/B-factor disorder 启发式位点过滤（探索性）：默认关闭。

    关闭时 disorder_check 节点记为「未评估」，不推理、不注入 disordered_sites。
    工具本身（predict_disorder）仍可显式调用，但输出为探索性信号而非概率。
    """
    return _env_flag("WB_DISORDER_FILTER_ENABLED")

def get_workspace_path(sub_dir: str = "") -> str:
    """获取当前任务的独立目录，并自动创建"""
    base_dir = current_workspace.get()
    full_path = os.path.join(base_dir, sub_dir) if sub_dir else base_dir
    os.makedirs(full_path, exist_ok=True)
    return full_path


def _ws_task_dir():
    """当前工作区是任务目录（output/task_*）时返回其绝对路径，否则 None。

    持久状态只对真正的任务目录记录；MCP 把工作区重置为项目根时不写状态。
    """
    ws = os.path.abspath(current_workspace.get())
    return ws if os.path.basename(ws).startswith("task_") else None


def _record_status(stage: str = None, **updates):
    """把阶段/状态写入任务目录内的 task_status.json（P1 持久化；非任务目录时静默跳过）。"""
    task_dir = _ws_task_dir()
    if not task_dir:
        return None
    try:
        if stage is not None:
            updates.setdefault("stage", stage)
        return state_write(task_dir, **updates)
    except OSError:
        return None  # 状态落盘失败不影响主流程


# runner 子进程用的自包含状态更新代码（runner 不 import 项目模块，避免重复初始化 RAG）。
# 2026-11-21 复核加固：①flock 跨进程互斥（与服务端 state_write 同语义，防丢更新）；
# ②_finalize_status 带 PID 守卫——同任务目录若被新一轮提交接管（runner_pids.ddg
#   变了），晚到的旧 runner 不得把状态改回 completed/failed，只留说明。
_RUNNER_STATUS_SRC = '''import os, json, tempfile, datetime as _dt
try:
    import fcntl as _fcntl
except ImportError:
    _fcntl = None

def _load_state(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def _dump_state(path, state, task_dir):
    fd, tmp = tempfile.mkstemp(dir=task_dir, prefix=".ts_", suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)

def _update_status(task_dir, **updates):
    path = os.path.join(task_dir, "task_status.json")
    try:
        lf = open(os.path.join(task_dir, ".task_status.lock"), "w")
    except OSError:
        lf = None
    try:
        if lf is not None and _fcntl is not None:
            _fcntl.flock(lf, _fcntl.LOCK_EX)
        state = _load_state(path)
        state.setdefault("task_id", os.path.basename(task_dir))
        state.setdefault("created_at", _dt.datetime.now().strftime("%Y-%m-%dT%H:%M:%S"))
        state.update(updates)
        state["updated_at"] = _dt.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        _dump_state(path, state, task_dir)
    except Exception:
        pass  # 状态落盘失败不终止计算
    finally:
        if lf is not None:
            lf.close()  # flock 随关闭释放；进程崩溃由内核释放，无残留锁

def _finalize_status(task_dir, my_pid, **updates):
    path = os.path.join(task_dir, "task_status.json")
    try:
        lf = open(os.path.join(task_dir, ".task_status.lock"), "w")
    except OSError:
        lf = None
    try:
        if lf is not None and _fcntl is not None:
            _fcntl.flock(lf, _fcntl.LOCK_EX)
        state = _load_state(path)
        state.setdefault("created_at", _dt.datetime.now().strftime("%Y-%m-%dT%H:%M:%S"))
        recorded = (state.get("runner_pids") or {}).get("ddg")
        if recorded is not None and str(recorded) != str(my_pid):
            state["stale_runner_note"] = (
                f"旧 runner(pid={my_pid})晚到结束；任务已由 runner(pid={recorded})接管，状态保持不变")
            state["updated_at"] = _dt.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
            _dump_state(path, state, task_dir)
            return
        mp = updates.pop("manifest_patch", None)
        state.update(updates)
        if mp:
            _m = state.get("manifest") or {}
            _m.update(mp)
            state["manifest"] = _m
        state["updated_at"] = _dt.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        _dump_state(path, state, task_dir)
    except Exception:
        pass
    finally:
        if lf is not None:
            lf.close()
'''

# ==========================================
# 1. 基础读取与诊断工具
# ==========================================
def _pdb_has_atoms(path: str) -> bool:
    """PDB 文件里是否真的有 ATOM/HETATM 原子行（空 selection 会写出仅含 END 的 4 字节文件）。"""
    try:
        with open(path, errors="ignore") as f:
            return any(l.startswith(("ATOM", "HETATM")) for l in f)
    except OSError:
        return False


def _mol2_has_atoms(path: str) -> bool:
    """MOL2 文件的 @<TRIPOS>ATOM 段是否真有原子行。"""
    try:
        with open(path, errors="ignore") as f:
            in_atom = False
            for l in f:
                if l.startswith("@<TRIPOS>ATOM"):
                    in_atom = True
                elif l.startswith("@<TRIPOS>"):
                    in_atom = False
                elif in_atom and l.strip():
                    return True
        return False
    except OSError:
        return False


def _ws_has_real_ligand(ws: str) -> bool:
    """工作区是否存在「真配体」文件（含原子坐标，而非空壳文件）。"""
    cand = [os.path.join(ws, "ligand.pdb"), os.path.join(ws, "ligand.mol2")] + glob.glob(os.path.join(ws, "*.mol2"))
    for p in cand:
        if p.lower().endswith(".mol2"):
            if _mol2_has_atoms(p):
                return True
        elif _pdb_has_atoms(p):
            return True
    return False


@tool
def read_file(file_path: str, max_lines: int = 50) -> str:
    """读取指定文件的内容。用于查看 .ddg 结果文件、目录列表等。"""
    try:
        # 如果是相对路径，则相对于当前工作区
        if not os.path.isabs(file_path):
            file_path = os.path.join(current_workspace.get(), file_path)
            
        if not os.path.exists(file_path):
            return f"错误：文件 {file_path} 不存在。"
        with open(file_path, 'r', encoding='utf-8') as f:
            lines = f.readlines()
            if len(lines) > max_lines:
                return "".join(lines[:max_lines]) + f"\n... (截断，仅显示前 {max_lines} 行)"
            return "".join(lines)
    except Exception as e:
        return f"读取文件失败: {str(e)}"

@tool
def check_logs(log_path: str, lines: int = 20) -> str:
    """错误排查工具：仅读取日志最后 N 行，极大地节省 Token。"""
    if not os.path.isabs(log_path):
        log_path = os.path.join(current_workspace.get(), log_path)
        
    if not os.path.exists(log_path):
        return f"【日志排查失败】未找到文件 {log_path}。"
    try:
        # 参数列表形式（不经 shell）：路径含空格/特殊字符也安全，且无注入面
        result = subprocess.run(["tail", "-n", str(int(lines)), log_path],
                                capture_output=True, text=True)
        if result.returncode == 0:
            return f"【日志截取成功】{log_path} 最后 {lines} 行：\n{result.stdout}"
        return f"【日志截取报错】{result.stderr}"
    except Exception as e:
        return f"【系统异常】{str(e)}"

# ==========================================
# 1.5 编号体系核心：唯一负责 PDB<->Rosetta 映射的实现（实现收敛于 task_runtime）
# ==========================================
# 残基身份四元组 = (链 ID, PDB 残基号, 插入码, 野生型氨基酸)。
# 2026-11-20 修复：旧实现只数 CA、丢弃插入码、多链同号静默取第一链、无链输入退化匹配。
# 新实现（task_runtime.build_numbering）：
#   - 插入码（100A）独立于 100 参与映射；
#   - 残基须具备完整主链 N/CA/C，缺失者按 Rosetta 实测行为剔除（不占 pose 编号）；
#   - 备用构象/非标准残基/多模型的剔除规则与 run_cartesian_relax 清洗完全一致；
#   - 无链纯编号在多链重号时标记歧义，解析时明确报错（见 task_runtime.resolve_targets）。

def _build_rosetta_numbering(pdb_path: str):
    """【兼容包装】保留旧三元组返回签名；内部委托 task_runtime.build_numbering。

    返回三元组：
      rosetta_to_pdb : {Rosetta绝对行号(int): "链_PDB编号[插入码](str)"}
      pdb_to_rosetta : {"链_编号[icode]": 绝对行号, "编号[icode]": 绝对行号或 None(多链重号)}
      wt_aa_dict     : {Rosetta绝对行号(int): 单字母氨基酸(str)}

    ⚠️ 新代码请直接用 task_runtime.build_numbering + resolve_targets；
    本包装仅供既有调用方（rosetta_server 预检、run_fixbb_design）平滑迁移。
    """
    nmap = _build_numbering_impl(pdb_path)
    rosetta_to_pdb = dict(nmap.rosetta_to_pdb)
    pdb_to_rosetta = dict(nmap.strict)
    for numkey, idx in nmap.plain.items():
        pdb_to_rosetta[numkey] = None if idx is task_runtime._AMBIGUOUS else idx
    return rosetta_to_pdb, pdb_to_rosetta, dict(nmap.wt)



@tool
def get_rosetta_numbering(pdb_path: str, target_resnums: str, chain_id: str = "") -> str:
    """【只读核对工具】展示 PDB 晶体学编号 <-> Rosetta 绝对行号的映射关系（不修改任何文件）。

    用于批量突变前人工核对编号映射是否正确。
    位点格式：A_76 / A_100A（链_编号[插入码]）或 76（唯一时可用，多链重号会报错）。
    chain_id 留空 = 自动判定（单链任务无需填；多链同号必须显式给链）。
    注意：本工具输出的绝对行号仅供【查看核对】，切勿把它传给 run_saturation_mutagenesis！
    run_saturation_mutagenesis 接收的是【PDB 晶体学编号】（如 76,73），内部会自动完成转换。
    """

    # === 优先使用清洗后的结构 ===
    relax_pdb = os.path.join(current_workspace.get(), "cartesian_relax", "complex_rosetta_ready.pdb")
    if os.path.exists(relax_pdb):
        pdb_path = relax_pdb
    elif not os.path.isabs(pdb_path):
        pdb_path = os.path.join(current_workspace.get(), pdb_path)

    if not os.path.exists(pdb_path):
        return f"【执行报错】未找到 {pdb_path}。请确认文件路径。"

    try:
        # 1. 与突变执行共用同一 token 解析与映射（杜绝多套编号逻辑）
        nmap = _build_numbering_impl(pdb_path)
        tokens = _split_tokens(target_resnums)
        # 指定了链时给无链 token 补上链前缀（保持旧调用方式兼容）
        if chain_id:
            tokens = [t if "_" in t else f"{chain_id}_{t}" for t in tokens]
        if not tokens:
            return "【执行报错】未提供有效的晶体学编号，请确保输入格式如 '76' 或 'A_76, B_100A'。"
        targets, errors = _resolve_targets(tokens, nmap)

        detail_str = []
        for t in targets:
            detail_str.append(f"PDB {t['chain']}_{t['numkey']} ({t['wt']}) -> Rosetta绝对行号 {t['idx']}")
        for e in errors:
            detail_str.append(f"PDB {e['token']} -> 【未匹配】{e['reason']}")

        if not targets:
            head = "【执行报错】没有任何位点匹配成功。" if errors else \
                   "【执行报错】未找到对应的原子。请检查链 ID 和晶体学编号是否正确。"
            chains_note = f"可用链: {'/'.join(nmap.chains) or '无'}；" if nmap.chains else ""
            return head + chains_note + "\n".join(detail_str)

        extra = f"（结构解析异常: {'；'.join(nmap.anomalies)}）" if nmap.anomalies else ""
        return (
            "【映射核对】\n" + "\n".join(detail_str) + extra +
            "\n\n【编号体系铁律】run_saturation_mutagenesis 接收【PDB 晶体学编号】并自动完成转换。"
            "请把原始 PDB 编号（如 76,73）原样传给 run_saturation_mutagenesis，"
            "绝对不要把上面的绝对行号再传给它，否则会导致双重偏移错位！"
            "\n【诚实边界】以上绝对行号是按清洗规则静态推断的 pose 编号；极端情况（零占用度等）"
            "以实际 Rosetta 日志为准。"
        )

    except Exception as e:
        return f"【异常】: {str(e)}"

@tool
def parameterize_ligand(ligand_path: str = "ligand.pdb") -> str:
    """自动侦测 3 字母代号并生成参数。支持输入 PDB 自动调 PyMOL 加氢并转 MOL2。

    纯蛋白任务（工作区无任何配体文件）时返回跳过提示而非报错，流水线可继续弛豫。
    """
    if not os.path.isabs(ligand_path):
        ligand_path = os.path.join(current_workspace.get(), ligand_path)

    if not os.path.exists(ligand_path):
        # 纯蛋白分支：工作区里确实没有配体文件时，如实跳过（不标记为执行报错），
        # 让三段流水线可以继续弛豫，而不是陷入修复循环
        ws = current_workspace.get()
        has_ligand = _ws_has_real_ligand(ws)
        if not has_ligand:
            return ("【纯蛋白任务】工作区未检测到配体文件，跳过参数化步骤。"
                    "请直接调用 run_cartesian_relax 对蛋白结构进行弛豫。")
        return f"【执行报错】未找到 {ligand_path}"

    out_dir = get_workspace_path("parameterize_ligand")

    # 1. 判断并执行 PDB 转 MOL2 (加氢)
    file_ext = os.path.splitext(ligand_path)[1].lower()
    if file_ext == ".pdb":
        mol2_filename = os.path.basename(ligand_path).replace(".pdb", ".mol2")
        mol2_path = os.path.join(out_dir, mol2_filename)
        
        pymol_cmds = f"load {ligand_path}; h_add; save {mol2_path}"

        try:
            subprocess.run([PYMOL_BIN, "-c", "-d", pymol_cmds],
                           check=True, capture_output=True)
            if not os.path.exists(mol2_path):
                return f"【执行报错】PyMOL 运行结束，但未生成 {mol2_path}"
        except Exception as e:
            return f"【执行报错】PyMOL 转换失败: {str(e)}"
    elif file_ext == ".mol2":
        mol2_path = ligand_path
    else:
        return "【执行报错】不支持的文件格式，仅支持 .pdb 或 .mol2"

    # 2. 侦测 3 字母代号
    ligand_name = "LIG"
    try:
        with open(mol2_path, 'r') as f:
            lines = f.readlines()
            for i, line in enumerate(lines):
                if "@<TRIPOS>ATOM" in line:
                    parts = lines[i+1].strip().split()
                    if len(parts) >= 8:
                        raw_name = parts[7]
                        ligand_name = ''.join([c for c in raw_name if c.isalpha()]).upper()[:3]
                    break
    except Exception:
        pass

    # 3. 运行 Rosetta 参数化脚本（参数列表 + cwd，不经 shell，路径含空格也安全）
    script_path = os.path.join(ROSETTA_SCRIPTS, "molfile_to_params.py")
    abs_mol2_path = os.path.abspath(mol2_path)
    cmd = ["python", script_path, "-n", ligand_name, "-p", ligand_name,
           "--keep-names", "--clobber", abs_mol2_path]

    try:
        result = subprocess.run(cmd, cwd=out_dir, capture_output=True, text=True)
        if result.returncode == 0:
            try:
                for f in os.listdir(out_dir):
                    if f.endswith(".params") and f != f"{ligand_name}.params":
                        os.remove(os.path.join(out_dir, f))
            except:
                pass
            return f"【参数化成功】自动检测代号: {ligand_name}。参数路径: {out_dir}/{ligand_name}.params"
        return f"【执行报错】执行失败。核心报错：\n" + "\n".join(result.stderr.strip().split('\n')[-15:])
    except Exception as e:
        return f"【系统异常】{str(e)}"

@tool
def run_cartesian_relax(protein_pdb: str = "protein.pdb", nstruct: int = 3) -> str:
    """物理清洗 PDB，自动拼接配体，并强制多核并发弛豫。"""
    if not os.path.isabs(protein_pdb):
        protein_pdb = os.path.join(current_workspace.get(), protein_pdb)
        
    out_dir = get_workspace_path("cartesian_relax")
    params_dir = get_workspace_path("parameterize_ligand")

    # 增加 IO 延迟重试，解决找不到 params 的报错
    params_file = None
    for _ in range(5):
        params_files = glob.glob(f"{params_dir}/*.params")
        if params_files:
            params_file = os.path.abspath(params_files[0])
            break
        time.sleep(1)

    ws = current_workspace.get()
    has_ligand_file = _ws_has_real_ligand(ws)

    if not params_file and has_ligand_file:
        # 工作区有配体文件却探测不到 params：参数化确实失败，走修复分支
        return "【执行报错】连续 5 次探测未找到 .params 文件，请确认 tool2 是否正确执行。"
    if not params_file:
        # 纯蛋白分支：无配体任务不拼接配体，relax 不带 -extra_res_fa
        ligand_name = None
        ligand_pdb = None
    else:
        ligand_name = os.path.basename(params_file).replace(".params", "")
        ligand_pdb = os.path.join(params_dir, f"{ligand_name}_0001.pdb")

        if not os.path.exists(ligand_pdb):
            candidate = glob.glob(f"{params_dir}/{ligand_name}*.pdb")
            if candidate: ligand_pdb = candidate[0]
            else: return f"【执行报错】未找到对应的配体结构文件 {ligand_pdb}。"

    ready_pdb = os.path.join(out_dir, "complex_rosetta_ready.pdb")
    standard_aas = {"ALA", "CYS", "ASP", "GLU", "PHE", "GLY", "HIS", "ILE", "LYS", "LEU", "MET", "ASN", "PRO", "GLN", "ARG", "SER", "THR", "VAL", "TRP", "TYR"}

    try:
        with open(protein_pdb, 'r') as f_in, open(ready_pdb, 'w') as f_out:
            seen_endmdl = False  # 多模型 PDB 只取第 1 个模型（与 task_runtime.build_numbering 一致）
            for line in f_in:
                if line.startswith("ENDMDL"):
                    seen_endmdl = True
                    continue
                if line.startswith("ATOM") and not seen_endmdl:
                    res_name = line[17:20].strip()
                    alt_loc = line[16]
                    if res_name not in standard_aas: continue
                    if alt_loc not in [' ', 'A', '1']: continue
                    if alt_loc != ' ': line = line[:16] + ' ' + line[17:]
                    f_out.write(line)
            f_out.write("TER\n")
            if ligand_pdb and os.path.exists(ligand_pdb):
                with open(ligand_pdb, 'r') as f_lig:
                    for line in f_lig:
                        if line.startswith("HETATM") or line.startswith("ATOM"): f_out.write(line)
    except Exception as e:
        return f"【系统异常】组装报错: {str(e)}"

    rosetta_relax_bin = os.path.join(ROSETTA_BIN, "relax.linuxgccrelease")
    abs_ready_pdb = os.path.abspath(ready_pdb)
    mode_note = "" if params_file else "（纯蛋白模式：未检测到配体，未拼接配体、未加载 extra_res）"

    # Rosetta 参数与旧实现逐字一致（含顺序）；仅把"shell 字符串"改为参数列表执行，
    # 路径含空格/特殊字符时不再断裂。%I% 是每路弛豫的序号占位符。
    relax_argv = [rosetta_relax_bin, "-s", abs_ready_pdb]
    if params_file:
        relax_argv += ["-extra_res_fa", params_file]
    relax_argv += [
        "-relax:cartesian", "-score:weights", "ref2015_cart",
        "-relax:min_type", "lbfgs_armijo_nonmonotone",
        "-relax:constrain_relax_to_start_coords", "-relax:coord_constrain_sidechains",
        "-use_input_sc", "-ignore_unrecognized_res", "-ignore_zero_occupancy",
        "-missing_density_to_jump", "-nstruct", "1",
        "-out:prefix", "cart_relax_%I%_", "-out:file:scorefile", "score_%I%.sc",
    ]
    task_dir_abs = os.path.dirname(os.path.abspath(out_dir))

    runner_py = (
        "import os, json, subprocess\n"
        "from concurrent.futures import ThreadPoolExecutor\n\n"
        f"task_dir = {task_dir_abs!r}\n"
        f"relax_argv = {relax_argv!r}\n"
        f"nstruct = {int(nstruct)}\n\n"
        + _RUNNER_STATUS_SRC + """

def execute(i):
    argv = [a.replace("%I%", str(i)) for a in relax_argv]
    # 参数列表执行（无 shell，路径含空格安全）；退出码追加进日志便于诊断
    with open(f"relax_run_{i}.log", "w") as lf:
        rc = subprocess.run(argv, stdout=lf, stderr=subprocess.STDOUT).returncode
    with open(f"relax_run_{i}.log", "a") as lf:
        lf.write(f"\\n[exit code {rc}]\\n")
    return rc

print(f" 开始多核并发弛豫 (共 {nstruct} 个构象)...")
rcs = []
with ThreadPoolExecutor(max_workers=nstruct) as pool:
    for rc in pool.map(execute, range(1, nstruct + 1)):
        rcs.append(rc)
print(" 各路退出码:", rcs)

print(" 合并打分文件...")
with open("score.sc", "w") as fout:
    fout.write("SEQUENCE:\\nSCORE: total_score description\\n")
    for i in range(1, nstruct + 1):
        try:
            with open(f"score_{i}.sc", "r") as fin:
                for line in fin:
                    if line.startswith("SCORE:") and "description" not in line:
                        fout.write(line)
        except Exception:
            pass
_update_status(task_dir, relax_finished=True,
               relax_exit_codes=rcs,
               status_note="弛豫完成" if os.path.exists("score.sc") else "弛豫结束但未生成 score.sc")
print(" 弛豫全流程结束！")
"""
    )
    with open(os.path.join(out_dir, "run_relax_parallel.py"), "w") as f:
        f.write(runner_py)

    # 后台启动：参数列表 + cwd + start_new_session（替代 nohup+cd 的 shell 串，
    # 任务目录名含空格时也能正确工作；start_new_session 使子进程脱离本进程组，
    # 服务进程收到 Ctrl-C/终止信号时不会被连带杀死，语义等同 nohup）
    try:
        with open(os.path.join(out_dir, "master_relax.log"), "w") as log_f:
            proc = subprocess.Popen([sys.executable, "-u", "run_relax_parallel.py"],
                                    cwd=out_dir, stdout=log_f, stderr=subprocess.STDOUT,
                                    start_new_session=True)
        # P1 状态：记录弛豫 runner 进程号，供重启后判定计算是否仍在运行
        _record_status(stage="relax", status="running",
                       runner_pids={"relax": proc.pid, "ddg": None},
                       params={"nstruct": int(nstruct),
                               "weights": "ref2015_cart", "min_type": "lbfgs_armijo_nonmonotone"})
        return f"【计算已挂起】已调动后台多核并发执行 {nstruct} 个弛豫任务。{mode_note}"
    except Exception as e:
        _record_status(stage="relax", status="failed",
                       error_type=type(e).__name__, error_detail=str(e))
        return f"【系统异常】{str(e)}"

@tool
def run_saturation_mutagenesis(target_resnum: str, jobs: int = 10, delay: float = 1.5,
                               export_structures: bool = False,
                               export_sites: str = "") -> str:
    """高通量错峰并发流水线：自动等待弛豫完成进行突变计算。

    结构产出开关(2026-11-21):cartesian_ddg 实测本就会把 WT_/MUT_*.pdb 落在突变目录
    (此前"不产出结构"的说法系 11-16 旧行为的误判)。
      - export_structures=False(默认):行为与旧版完全一致(结构散落在突变目录,不整理不登记);
      - export_structures=True:计算结束后把指定位点的突变体结构归集到
        saturation_mutagenesis/mutant_structures/<wt>_<site>_<mut>/,做内容级落点核对
        (结构中该链该编号残基唯一且等于突变后氨基酸),写 structures_manifest.json,
        并把 structures_exported/structures_verified 登记进 manifest、
        mutant_structures 登记进 artifacts;
      - export_sites:仅在 export_structures=True 时可用;为扫描位点子集(如 "A_67" 或 "67,A_12"),
        缺省=全部扫描位点。非所选位点的结构仍散落在突变目录(不删除,可复查)。
    """
    relax_dir = get_workspace_path("cartesian_relax")
    mut_dir = get_workspace_path("saturation_mutagenesis")
    sum_dir = get_workspace_path("summary")
    params_dir = get_workspace_path("parameterize_ligand")
    
    params_file = None
    for _ in range(5):
        params_files = glob.glob(f"{params_dir}/*.params")
        if params_files:
            params_file = os.path.abspath(params_files[0])
            break
        time.sleep(1)

    ws = current_workspace.get()
    has_ligand_file = _ws_has_real_ligand(ws)
    if not params_file and has_ligand_file:
        return "【执行报错】探测未找到 .params 文件，请确认 tool2 是否正确执行。"
    # 纯蛋白分支：无配体任务 ddg 命令不带 -extra_res_fa
    mode_note = "" if params_file else "（纯蛋白模式）"
    
    ready_pdb_path = os.path.join(relax_dir, "complex_rosetta_ready.pdb")
    if not os.path.exists(ready_pdb_path):
        return "【执行报错】未找到初始拼装结构 complex_rosetta_ready.pdb，tool3可能未正确执行。"

    # 位点输入：与提交预检 / get_rosetta_numbering 共用同一解析实现（P0 收敛）。
    # 行为变化（相对旧版，见 CHANGES-rosetta.md）：
    #   - 移除"提取数字退化匹配"：错误链名（如 B76）、带插入码的 100A、多链同号无链输入
    #     旧版会静默映射到第一条链的该编号——现一律明确报错；
    #   - 插入码按 (编号+插入码) 精确匹配；插入码位点在弛豫清洗结构中真实存在。
    tokens = _split_tokens(target_resnum)
    if not tokens:
        return ("【执行报错】未解析到任何突变位点。格式如 '76'、'A_76'、'A_100A'；"
                "多位点用逗号分隔。")
    nmap = _build_numbering_impl(ready_pdb_path)
    resolved, errors = _resolve_targets(tokens, nmap)
    if errors:
        detail = "; ".join(f"{e['token']}({e['reason']})" for e in errors)
        return (f"【执行报错】位点解析失败: {detail}。"
                f"清洗后结构含 {nmap.size} 个标准残基，链: {'/'.join(nmap.chains) or '无'}。"
                "可先调用 get_rosetta_numbering 核对；多链同号必须带链 ID（如 A_76）。")
    anom_note = f"（结构解析异常: {'；'.join(nmap.anomalies)}）" if nmap.anomalies else ""

    # 结构产出开关:cartesian_ddg 实测本就落盘 WT_/MUT_*.pdb;开关只控制是否把
    # 指定位点的突变体结构归集+落点核对+登记(见函数 docstring 与 TASK-CONTRACT-v0.1 §4)。
    export_set = None
    export_note = ""
    if export_structures:
        if export_sites.strip():
            _es_tokens = _split_tokens(export_sites)
            _es_resolved, _es_errors = _resolve_targets(_es_tokens, nmap)
            if _es_errors:
                _detail = "; ".join(f"{e['token']}({e['reason']})" for e in _es_errors)
                return f"【执行报错】export_sites 解析失败: {_detail}。"
            _scan_set = {(t["chain"], t["numkey"]) for t in resolved}
            _unknown = [f"{t['chain']}_{t['numkey']}" for t in _es_resolved
                        if (t["chain"], t["numkey"]) not in _scan_set]
            if _unknown:
                return ("【执行报错】export_sites 位点 " + "/".join(_unknown)
                        + " 不在本次扫描位点("
                        + "/".join(f"{t['chain']}_{t['numkey']}" for t in resolved)
                        + ")内;结构导出仅支持本次扫描的位点。")
            export_set = {(t["chain"], t["numkey"]) for t in _es_resolved}
            export_note = "(结构导出:仅位点 " + export_sites.strip() + ")"
        else:
            export_note = "(结构导出:全部扫描位点)"
    elif export_sites.strip():
        return "【执行报错】export_sites 仅在 export_structures=True 时有意义。"

    all_aas = ["A", "C", "D", "E", "F", "G", "H", "I", "K", "L", "M", "N", "P", "Q", "R", "S", "T", "V", "W", "Y"]

    # 唯一一次 PDB -> Rosetta 绝对行号映射：编号映射基准 = complex_rosetta_ready.pdb
    # （与实际 cartesian_ddg 输入 wt_relaxed.pdb 同源——后者由前者弛豫产生，Rosetta 保持编号）
    tasks_file = os.path.join(mut_dir, "rosetta_tasks.txt")
    rosetta_ddg_bin = os.path.join(ROSETTA_BIN, "cartesian_ddg.linuxgccrelease")
    abs_wt_relaxed = os.path.abspath(os.path.join(relax_dir, "wt_relaxed.pdb"))

    # 任务行改为 JSON（argv 参数列表 + 日志名）：runner 不再经 shell 拼接，
    # 路径含空格/特殊字符安全，退出码可记录。科学旗标与旧 shell 串逐字一致。
    n_planned = 0
    with open(tasks_file, 'w') as f_tasks:
        for t in resolved:
            t_res, pdb_num, wt_aa = t["idx"], f"{t['chain']}_{t['numkey']}", t["wt"]
            f_tasks.write(f"# [编号映射] PDB {pdb_num} -> Rosetta绝对行号 {t_res} ({wt_aa})（审计）\n")
            for mut_aa in (aa for aa in all_aas if aa != wt_aa):
                mutfile_name = f"mut_{wt_aa}_{pdb_num}_{mut_aa}.txt"

                with open(os.path.join(mut_dir, mutfile_name), 'w') as f_mut:
                    f_mut.write("total 1\n1\n")
                    f_mut.write(f"{wt_aa} {t_res} {mut_aa}\n")

                argv = [rosetta_ddg_bin, "-s", abs_wt_relaxed, "-ddg:mut_file", mutfile_name]
                if params_file:
                    argv += ["-extra_res_fa", params_file]
                argv += [
                    "-ddg:iterations", "3", "-ddg:cartesian", "-ddg:bbnbrs", "1",
                    "-score:weights", "ref2015_cart", "-relax:min_type", "lbfgs_armijo_nonmonotone",
                    "-relax:cartesian", "-mute", "all",
                    "-out:prefix", f"ddg_{wt_aa}_{pdb_num}_{mut_aa}_",
                ]
                is_export = bool(export_structures
                                 and (export_set is None or (t["chain"], t["numkey"]) in export_set))
                f_tasks.write(json.dumps({"argv": argv, "log": f"mut_{wt_aa}_{pdb_num}_{mut_aa}.log",
                                          "export": is_export, "site": pdb_num,
                                          "wt": wt_aa, "mut": mut_aa, "idx": t_res},
                                         ensure_ascii=False) + "\n")
                n_planned += 1

    abs_sum_file = os.path.abspath(os.path.join(sum_dir, "ddg_results.txt"))
    cleaner_py = f"""import os
import re
import glob
results = []
for f in glob.glob("*.ddg"):
    try:
        parts = f.replace('.ddg', '').split('_')
        # 因为加入了链 ID，被下划线切割后的块数变多了，从 4 改为 5
        if len(parts) < 5: continue

        # 位号块兼容插入码（100A）与多字符链：只取前导数字作排序键，名字保留完整 numkey
        m_num = re.match(r"(\\d+)", parts[3])
        if not m_num: continue
        pdb_num = int(m_num.group(1))
        chain_id = parts[2]
        # 拼接成类似 A_A76_A / A_A100A_A 的格式
        mut_name = f"{{parts[1]}}_{{chain_id}}{{parts[3]}}_{{parts[4]}}"

        wt_scores, mut_scores = [], []
        with open(f, 'r') as fp:
            for line in fp:
                lp = line.split()
                if len(lp) >= 3:
                    if 'WT' in line:
                        try: wt_scores.append(float(lp[3]))
                        except: pass
                    elif 'MUT' in line:
                        try: mut_scores.append(float(lp[3]))
                        except: pass
        if wt_scores and mut_scores:
            ddg = min(mut_scores) - min(wt_scores)
            results.append((chain_id, pdb_num, mut_name, ddg))
    except Exception: pass

# 多级排序：先按链ID排，再按氨基酸行号排，最后按 ddG 值排
results.sort(key=lambda x: (x[0], x[1], x[3]))
with open("{abs_sum_file}", "w") as out:
    # 口径如实：ddg = min(突变体三次迭代) - min(WT 三次迭代)，不是平均值
    out.write("突变\\tΔΔG(REU, min(MUT)-min(WT))\\n")
    for chain_id, pdb_num, mut_name, ddg in results:
        out.write(f"{{mut_name}}\\t{{ddg:.4f}}\\n")
"""
    with open(os.path.join(mut_dir, "auto_clean.py"), "w") as f: f.write(cleaner_py)

    abs_relax_dir = os.path.abspath(relax_dir)
    task_dir_abs = os.path.dirname(os.path.abspath(mut_dir))
    runner_py = (
        "import time\n"
        "import json\n"
        "import sys\n"
        "import subprocess\n"
        "import os\n"
        "import glob\n"
        "import shutil\n"
        "from concurrent.futures import ThreadPoolExecutor\n\n"
        f"relax_dir = {abs_relax_dir!r}\n"
        f"task_dir = {task_dir_abs!r}\n\n"
        + _RUNNER_STATUS_SRC + '\n'
        + f'''score_path = os.path.join(relax_dir, "score.sc")
wt_pdb_path = os.path.join(relax_dir, "wt_relaxed.pdb")

print(" 正在后台默默等待 tool3 弛豫彻底完成 (等待合并打分文件 score.sc)...")
max_wait = 14400
waited = 0
last_print = 0
while not os.path.exists(score_path) and waited < max_wait:
    if waited - last_print >= 60:
        cur_count = len(glob.glob(os.path.join(relax_dir, "cart_relax_*.pdb")))
        print(f"   等待中... 已等待 {{waited//60}} 分钟, 已完成 {{cur_count}} 个弛豫构象")
        last_print = waited
    time.sleep(10)
    waited += 10

if os.path.exists(score_path):
    with open(score_path, 'r') as f:
        lines = f.readlines()
    data_lines = [l.split() for l in lines if l.startswith("SCORE:") and "total_score" not in l]
    if data_lines:
        data_lines.sort(key=lambda x: float(x[1]))
        best_desc = data_lines[0][-1]
        best_pdb = os.path.join(relax_dir, best_desc + ".pdb")
        if os.path.exists(best_pdb):
            shutil.copy(best_pdb, wt_pdb_path)
        else:
            print(" 警告: 最佳构象 PDB 不存在，回退到任意可用结构")
            pdbs = sorted(glob.glob(os.path.join(relax_dir, "cart_relax_*.pdb")))
            if pdbs: shutil.copy(pdbs[-1], wt_pdb_path)
else:
    print(" 警告: 超过 4 小时未生成 score.sc，回退使用任意已完成的弛豫结构")
    pdbs = sorted(glob.glob(os.path.join(relax_dir, "cart_relax_*.pdb")))
    if pdbs: shutil.copy(pdbs[-1], wt_pdb_path)

if not os.path.exists(wt_pdb_path):
    pdbs = sorted(glob.glob(os.path.join(relax_dir, "cart_relax_*.pdb")))
    if pdbs: shutil.copy(pdbs[-1], wt_pdb_path)
    else:
        print(" 严重错误：弛豫未成功生成结构，突变任务终止！")
        _finalize_status(task_dir, os.getpid(), status="failed", stage="mutagenesis",
                         error_type="NoRelaxStructure",
                         error_detail="弛豫未生成任何结构，突变计算未执行")
        exit(1)

with open("rosetta_tasks.txt", "r") as f:
    tasks = []
    for line in f:
        line = line.strip()
        if line and not line.startswith('#'):
            tasks.append(json.loads(line))  # {{"argv": [...], "log": "..."}}

print(f" 开始 Python 内置错峰并发计算 (并发:{int(jobs)}, 延迟:{float(delay)}s)...")
def execute(rec):
    # 参数列表执行（无 shell，任务路径含空格安全）；退出码追加进日志。
    # OSError（如 Rosetta 可执行文件缺失）不再炸线程池——记 -999 并入日志，
    # 让收尾按"0 个成功突变"如实判 failed，而不是静默无产物。
    rc = -999
    try:
        with open(rec["log"], "w") as lf:
            rc = subprocess.run(rec["argv"], stdout=lf, stderr=subprocess.STDOUT).returncode
    except OSError as _e:
        with open(rec["log"], "w") as lf:
            lf.write(f"[启动失败] {{_e}}\\n")
        rc = -999
    with open(rec["log"], "a") as lf:
        lf.write(f"\\n[exit code {{rc}}]\\n")
    return rc

with ThreadPoolExecutor(max_workers={int(jobs)}) as pool:
    for rec in tasks:
        pool.submit(execute, rec)
        time.sleep({float(delay)})

subprocess.run([sys.executable, "auto_clean.py"])

subprocess.run([sys.executable, "auto_clean.py"])

# 结构产出整理(export 任务):cartesian_ddg 本就会把 WT_/MUT_*.pdb 落在工作目录(实测),
# 此处把指定位点的 MUT_*.pdb 收进 mutant_structures/<wt>_<site>_<mut>/ 并做内容级落点核对。
# 注意:MUT 文件名含 (位点, 突变氨基酸) 二元组,并发任务间不重名(11-21 实测 57 个全部幸存)。
_aa3map = {{"A": "ALA", "C": "CYS", "D": "ASP", "E": "GLU", "F": "PHE", "G": "GLY", "H": "HIS",
            "I": "ILE", "K": "LYS", "L": "LEU", "M": "MET", "N": "ASN", "P": "PRO", "Q": "GLN",
            "R": "ARG", "S": "SER", "T": "THR", "V": "VAL", "W": "TRP", "Y": "TYR"}}
_mut_dir_abs = os.path.join(task_dir, "saturation_mutagenesis")
_struct_root = os.path.join(_mut_dir_abs, "mutant_structures")
_export_recs = [r for r in tasks if r.get("export")]
_n_struct_dirs = 0
_n_verified = 0
if _export_recs:
    os.makedirs(_struct_root, exist_ok=True)
    _entries = []
    for rec in _export_recs:
        _aa3 = _aa3map.get(rec.get("mut"), "")
        if not _aa3:
            continue
        _dname = "%s_%s_%s" % (rec.get("wt"), rec.get("site"), rec.get("mut"))
        _sdir = os.path.join(_struct_root, _dname)
        os.makedirs(_sdir, exist_ok=True)
        _files = sorted(glob.glob(os.path.join(_mut_dir_abs, "MUT_%s%s_*.pdb" % (rec.get("idx"), _aa3))))
        _verified = "missing"
        _detail = "未找到 MUT_%s%s_*.pdb(cartesian_ddg 未落盘该突变体?)" % (rec.get("idx"), _aa3)
        _resnames = set()
        if _files:
            _site_str = str(rec.get("site", ""))
            _chain = _site_str.split("_", 1)[0]
            _numkey = _site_str.split("_", 1)[-1]
            _digits = ""
            for _c in _numkey:
                if _c.isdigit():
                    _digits += _c
                else:
                    break
            if not _digits:
                _verified = "skipped_icode"
                _detail = "插入码位点暂不做内容级落点核对(仅按文件名归集)"
            else:
                with open(_files[0], "r", errors="ignore") as _pf:
                    for _line in _pf:
                        if _line.startswith("ATOM") or _line.startswith("HETATM"):
                            if _line[21:22].strip() == _chain and _line[22:26].strip() == _digits:
                                _resnames.add(_line[17:20].strip())
                if _aa3 in _resnames and len(_resnames) == 1:
                    _verified = "yes"
                    _n_verified += 1
                else:
                    _verified = "no"
                    _detail = "位点 %s_%s 处残基为 %s(预期 %s)" % (_chain, _numkey, sorted(_resnames), _aa3)
            for _f in _files:
                shutil.move(_f, os.path.join(_sdir, os.path.basename(_f)))
        _entries.append({{"dir": "mutant_structures/" + _dname, "site": rec.get("site"),
                          "wt": rec.get("wt"), "mut": rec.get("mut"),
                          "rosetta_idx": rec.get("idx"),
                          "files": [os.path.basename(f) for f in _files],
                          "verified": _verified, "detail": _detail}})
        _n_struct_dirs += 1
    with open(os.path.join(_struct_root, "structures_manifest.json"), "w", encoding="utf-8") as _sf:
        json.dump({{"note": "MUT 文件名内数字为 Rosetta 绝对行号;目录名/映射以 PDB 编号为准(见 rosetta_tasks.txt 审计行)",
                    "verified": "内容级落点核对:结构中该链该编号残基唯一且等于突变后氨基酸",
                    "entries": _entries}}, _sf, ensure_ascii=False, indent=1)

summary_path = os.path.join(task_dir, "summary", "ddg_results.txt")
n_ddg = len(glob.glob("*.ddg"))
n_planned = len(tasks)
_rosetta_version = None
try:
    for _log in sorted(glob.glob("mut_*.log")) + sorted(glob.glob(os.path.join(relax_dir, "*.log"))):
        with open(_log, "r", errors="ignore") as _lf:
            for _line in _lf:
                if "core.init: Rosetta version:" in _line:
                    _rosetta_version = _line.split("Rosetta version:", 1)[1].strip()[:160]
                    break
        if _rosetta_version:
            break
except Exception:
    _rosetta_version = None
_artifacts = {{"ddg_results": "summary/ddg_results.txt",
              "wt_relaxed": "cartesian_relax/wt_relaxed.pdb"}}
_manifest_patch = {{"mutations_succeeded": n_ddg, "mutations_failed": max(n_planned - n_ddg, 0),
                    "rosetta_version": _rosetta_version}}
if _export_recs:
    _artifacts["mutant_structures"] = "saturation_mutagenesis/mutant_structures"
    _manifest_patch["structures_exported"] = _n_struct_dirs
    _manifest_patch["structures_verified"] = _n_verified
if n_ddg > 0 and n_planned > 0 and n_ddg < n_planned:
    _finalize_status(task_dir, os.getpid(), status="partial", stage="mutagenesis",
                     status_note=f"部分成功（{{n_ddg}}/{{n_planned}} 个 .ddg）；失败项查看 mut_*.log 末尾的 [exit code]",
                     artifacts=_artifacts,
                     manifest_patch=_manifest_patch)
elif n_ddg > 0:
    _finalize_status(task_dir, os.getpid(), status="completed", stage="mutagenesis",
                     status_note=f"突变计算完成（{{n_ddg}} 个 .ddg）",
                     artifacts=_artifacts,
                     manifest_patch=_manifest_patch)
else:
    _finalize_status(task_dir, os.getpid(), status="failed", stage="mutagenesis",
                     error_type="NoSuccessfulMutants",
                     error_detail=f"0 个成功突变（{{'ddg_results.txt 在位' if os.path.exists(summary_path) else '无汇总'}})；逐个查看 mut_*.log 末尾的 [exit code]",
                     manifest_patch=_manifest_patch)
print(" 突变全流程结束！")
''')
    with open(os.path.join(mut_dir, "run_ddg.py"), "w") as f: f.write(runner_py)

    # 后台启动方式与 run_cartesian_relax 相同（参数列表 + cwd + start_new_session）
    try:
        with open(os.path.join(mut_dir, "run_launcher.log"), "w") as log_f:
            proc = subprocess.Popen([sys.executable, "-u", "run_ddg.py"],
                                    cwd=mut_dir, stdout=log_f, stderr=subprocess.STDOUT,
                                    start_new_session=True)
        # P1 状态：记录 ddg runner 进程号（保留 relax 记录）+ 轻量 manifest（P2）
        task_dir = _ws_task_dir()
        if task_dir:
            prev = task_runtime.state_read(task_dir)
            pids = dict(prev.get("runner_pids") or {})
            pids["ddg"] = proc.pid
            _record_status(stage="mutagenesis", status="running", runner_pids=pids)
            _protein_path = os.path.join(task_dir, "protein.pdb")
            _input_sha = None
            try:
                import hashlib as _hashlib
                with open(_protein_path, "rb") as _hf:
                    _input_sha = _hashlib.sha256(_hf.read()).hexdigest()
            except OSError:
                _input_sha = None
            set_manifest(
                task_dir,
                contract_version=task_runtime.CONTRACT_VERSION,
                input_structure_path="protein.pdb",
                input_structure_sha256=_input_sha,
                input_structure="protein.pdb",
                numbering_basis="cartesian_relax/complex_rosetta_ready.pdb（清洗后结构；规则见 task_runtime.build_numbering）",
                chains=nmap.chains,
                n_residues=nmap.size,
                target_sites=[f"{t['chain']}_{t['numkey']}(WT={t['wt']})" for t in resolved],
                mutations_planned=n_planned,
                ddg_program=os.path.basename(rosetta_ddg_bin),
                ddg_params={"iterations": 3, "cartesian": True, "bbnbrs": 1,
                            "weights": "ref2015_cart",
                            "min_type": "lbfgs_armijo_nonmonotone",
                            "extra_res_fa": bool(params_file)},
                rosetta_version=None,  # 静态无法获得（需运行时日志确认），留空不虚构
                gromacs_ready=False,   # 结构未经 MD 准备，不可直接作为 GROMACS 输入
                numbering_audit="rosetta_tasks.txt 中 # [编号映射] 注释行",
                structures_export=bool(export_structures),
            )
    except Exception as e:
        _record_status(stage="mutagenesis", status="failed",
                       error_type=type(e).__name__, error_detail=str(e))
        return f"【系统异常】突变任务提交失败: {str(e)}"
    return (f"【流水线通关】所有 {len(resolved)} 个位点的突变任务已挂载！{mode_note}"
            + (anom_note if anom_note else "") + (export_note if export_note else ""))

# ==========================================
# 3. 诊断与修复工具
# ==========================================
@tool
def diagnose_and_fix(tool_name: str) -> str:
    """智能诊断工具：分析工具执行结果，自动检测并修复已知问题。"""
    if tool_name == "run_saturation_mutagenesis":
        mut_dir = get_workspace_path("saturation_mutagenesis")
        sum_dir = get_workspace_path("summary")
        
        ddg_files = glob.glob(f"{mut_dir}/*.ddg")
        if not ddg_files:
            return "【诊断】未找到 .ddg 文件，突变计算可能失败。请检查 run_launcher.log。"
        
        result_file = os.path.join(sum_dir, "ddg_results.txt")
        if os.path.exists(result_file):
            with open(result_file, 'r') as f:
                lines = f.readlines()
            if len(lines) <= 1:
                return "【诊断】结果文件为空，可能是 auto_clean.py 解析错误。需要修复解析逻辑。"
            return f"【诊断成功】已生成 {len(lines)-1} 个突变结果。"
        
        return f"【诊断】找到 {len(ddg_files)} 个 .ddg 文件，但未生成结果。可能需要重新运行清洗。"
    return f"【诊断】未知工具: {tool_name}"

@tool  
def auto_repair() -> str:
    """自动修复工具：检测 saturation_mutagenesis 的汇总产物，必要时重跑 auto_clean.py 重建 ddg_results.txt。

    说明：auto_clean.py 每次运行饱和突变时都会重新生成（覆盖），因此本工具不改其代码，
    而是诊断「.ddg 已生成但 ddg_results.txt 缺失/为空」并尝试重跑汇总。
    """
    mut_dir = get_workspace_path("saturation_mutagenesis")
    sum_dir = get_workspace_path("summary")
    cleaner_path = os.path.join(mut_dir, "auto_clean.py")
    result_file = os.path.join(sum_dir, "ddg_results.txt")

    ddg_files = glob.glob(os.path.join(mut_dir, "*.ddg"))
    if not ddg_files:
        return "【诊断】saturation_mutagenesis 下没有 .ddg 文件，突变计算尚未产生结果，无需修复。"

    if os.path.exists(result_file) and os.path.getsize(result_file) > 0:
        with open(result_file, encoding="utf-8") as f:
            n = len(f.readlines()) - 1
        if n > 0:
            return f"【无需修复】ddg_results.txt 已含 {n} 条结果。"

    if not os.path.exists(cleaner_path):
        return "【修复失败】找到 .ddg 但未找到 auto_clean.py（突变任务可能中断在写汇总前）"
    try:
        r = subprocess.run(
            "python auto_clean.py", cwd=mut_dir, shell=True,
            capture_output=True, text=True, timeout=120,
        )
        if os.path.exists(result_file) and os.path.getsize(result_file) > 0:
            with open(result_file, encoding="utf-8") as f:
                n = len(f.readlines()) - 1
            return f"【自动修复成功】重跑 auto_clean.py，重建 ddg_results.txt（{n} 条）。"
        return f"【修复失败】重跑 auto_clean.py 后 ddg_results.txt 仍为空。stderr: {r.stderr[-300:]}"
    except Exception as e:
        return f"【修复失败】重跑 auto_clean.py 出错: {e}"


# ==========================================
# 3.5. 实验对照：SKEMPI 评估工具（计算 Pearson 相关性）
# ==========================================
import csv

SKEMPI_CSV_PATH = os.path.join(os.path.dirname(__file__), "rosetta_manuals", "skempi_v2.csv")


def _load_skempi():
    """加载 SKEMPI 2.0 数据库。

    自动探测分隔符（制表符 / 分号 / 逗号）。
    列名按 GitHub 镜像 youpze/ScML 的实际命名取：#Pdb / Mutation(s)_cleaned / Affinity_wt_parsed / Affinity_mut_parsed。
    文件不存在则返回 None。
    """
    if not os.path.exists(SKEMPI_CSV_PATH):
        return None
    with open(SKEMPI_CSV_PATH, 'r', encoding='utf-8') as f:
        sample = f.read(4096)
        f.seek(0)
        # 优先探测分号（SKEMPI 2.0 GitHub 镜像用的是 ;）
        if ';' in sample:
            delimiter = ';'
        elif '\t' in sample:
            delimiter = '\t'
        else:
            delimiter = ','
        return list(csv.DictReader(f, delimiter=delimiter))


def _parse_skempi_mutation(mut_str: str):
    """解析 SKEMPI 突变格式为统一字典。

    SKEMPI 格式示例：
      DA63B → partner=B, wt=D, pos=63, mut=A
      D63A  → wt=D, pos=63, mut=A
      VA45B,KE67B → 多个突变（逗号分隔）
    返回 {'wt', 'pos', 'mut', 'partner'}；失败返回 None。
    """
    s = mut_str.strip()
    if not s:
        return None
    partner = None
    if len(s) >= 5 and s[0].isalpha() and s[1].isalpha() and s[2].isdigit():
        partner = s[0]
        s = s[1:]
    if len(s) < 4:
        return None
    wt = s[0]
    pos_str = ''
    i = 1
    while i < len(s) - 1 and s[i].isdigit():
        pos_str += s[i]
        i += 1
    if not pos_str:
        return None
    mut = s[i] if i < len(s) else ''
    if not mut:
        return None
    try:
        pos = int(pos_str)
    except ValueError:
        return None
    return {'wt': wt, 'pos': pos, 'mut': mut, 'partner': partner}


def _parse_ddg_key(key: str):
    """ddg key 形如 'D_A63_B' → ('D', 63, 'B')。链无关三元组。"""
    import re
    parts = key.split('_')
    if len(parts) != 3:
        return None
    wt, chain_pos, mut = parts
    m = re.match(r'^([A-Z])(\d+)$', chain_pos)
    if not m:
        return None
    try:
        return (wt, int(m.group(2)), mut)
    except ValueError:
        return None


def _build_predicted_dict(ddg_path: str):
    """读 ddg_results.txt，返回 {(wt, pos, mut): ddg_value} 链无关 key。"""
    predicted = {}
    if not os.path.exists(ddg_path):
        return predicted
    with open(ddg_path, 'r', encoding='utf-8') as f:
        for line in f.readlines()[1:]:
            parts = line.strip().split('\t')
            if len(parts) >= 2:
                try:
                    val = float(parts[1])
                    tup = _parse_ddg_key(parts[0].strip())
                    if tup:
                        predicted[tup] = val
                except (ValueError, TypeError):
                    continue
    return predicted


def _skempi_key_from_parsed(p):
    """{'wt':'D','pos':63,'mut':'B','partner':'A'} → ('D', 63, 'B')。链无关。"""
    return (p['wt'], p['pos'], p['mut'])


def _pearson(x, y):
    """纯 Python Pearson 相关系数（不依赖 scipy）。返回 (r, n)。"""
    import math
    n = len(x)
    if n < 2:
        return float('nan'), n
    mean_x = sum(x) / n
    mean_y = sum(y) / n
    cov = sum((x[i] - mean_x) * (y[i] - mean_y) for i in range(n))
    var_x = sum((xi - mean_x) ** 2 for xi in x)
    var_y = sum((yi - mean_y) ** 2 for yi in y)
    if var_x == 0 or var_y == 0:
        return float('nan'), n
    r = cov / math.sqrt(var_x * var_y)
    return r, n


def _spearman(x, y):
    """纯 Python Spearman 秩相关（不依赖 scipy）。返回 (rho, n)。"""
    n = len(x)
    if n < 3:
        return float('nan'), n

    def _rank(data):
        """计算平均秩，处理并列值"""
        sorted_idx = sorted(range(len(data)), key=lambda i: data[i])
        ranks = [0.0] * len(data)
        i = 0
        while i < len(data):
            j = i
            while j + 1 < len(data) and data[sorted_idx[j + 1]] == data[sorted_idx[i]]:
                j += 1
            avg_rank = (i + j) / 2 + 1  # 1-indexed average rank
            for k in range(i, j + 1):
                ranks[sorted_idx[k]] = avg_rank
            i = j + 1
        return ranks

    rx = _rank(x)
    ry = _rank(y)
    rho, _ = _pearson(rx, ry)
    return rho, n


@tool
def evaluate_against_skempi(pdb_id: str, ddg_results_path: str = "") -> str:
    """【实验对照·探索性】拿 SKEMPI 2.0 数据库里同 PDB 同位点的实验 ΔΔG 与预测 ΔΔG 并排比对。

    默认（WB_EXPLORATORY_EVAL_ENABLED=0）只输出位点匹配表与「未评测」状态，
    不计算相关系数、不给任何可信度结论。显式开启后也只输出探索性 Pearson，
    并注明样本数与链无关匹配的局限，不构成可信度评级。

    已知口径限制（v0.1 如实标注）：
      - 预测值来自 cartesian_ddg 常规稳定性模式（REU），实验标签是结合 ΔΔG（kcal/mol），
        两者物理量不同，相关性仅作探索性参考；
      - 匹配 key 为链无关三元组 (wt, pos, mut)，多链同编号位点可能串位（v0.4 前不消除）。

    使用前提：
      1. 已下载 SKEMPI 2.0 CSV 到 rosetta_manuals/skempi_v2.csv（官方地址 https://life.bsc.es/pid/skempi2）
      2. 已完成 run_saturation_mutagenesis 生成 summary/ddg_results.txt
    """
    skempi = _load_skempi()
    if skempi is None:
        return (
            "【实验对照】SKEMPI 数据库未加载。请到 https://life.bsc.es/pid/skempi2 下载 skempi_v2.csv，"
            f"放到 {SKEMPI_CSV_PATH} 后重试。"
        )

    if not ddg_results_path:
        ddg_results_path = os.path.join(current_workspace.get(), "summary", "ddg_results.txt")
    predicted = _build_predicted_dict(ddg_results_path)
    if not predicted:
        return f"【实验对照】未在 {ddg_results_path} 读到预测结果。请先完成饱和突变。"

    pdb_id_upper = pdb_id.upper().strip()
    matching = []
    for entry in skempi:
        # GitHub 镜像用 #Pdb（无 id），旧版可能用 #Pdb id / PDB id
        pdb_field = (
            entry.get('#Pdb') or entry.get('#Pdb id') or
            entry.get('Pdb id') or entry.get('PDB id') or ''
        ).upper().strip()
        if pdb_field.startswith(pdb_id_upper):
            matching.append(entry)
    if not matching:
        return f"【实验对照】SKEMPI 中未找到 PDB {pdb_id_upper} 的记录。"

    experimental = {}
    R_KCAL = 1.987e-3  # kcal/(mol·K)
    for entry in matching:
        mut_field = (entry.get('Mutation(s)_cleaned') or entry.get('Mutation(s)_PDB') or entry.get('Mutation(s)') or '').strip()
        if not mut_field:
            continue
        # 优先用现成的 ddG 列；没有就从 parsed 亲和力算
        ddg_exp = None
        for cand in ('ddG_exp_kcal_mol', 'DDG(kcal/mol)', 'ΔΔG', 'ddG', 'ddG_exp'):
            if cand in entry and entry[cand]:
                try:
                    ddg_exp = float(entry[cand])
                    break
                except (ValueError, TypeError):
                    pass
        if ddg_exp is None:
            wt_aff = entry.get('Affinity_wt_parsed') or entry.get('Affinity_wt (M)')
            mut_aff = entry.get('Affinity_mut_parsed') or entry.get('Affinity_mut (M)')
            temp = entry.get('Temperature')
            try:
                kd_wt = float(wt_aff)
                kd_mut = float(mut_aff)
                T = float(temp) if temp else 298.0
                # 抑制强结合/弱结合极限（避免 RT*ln 数值爆掉）
                if kd_wt > 0 and kd_mut > 0:
                    import math
                    ddg_exp = R_KCAL * T * math.log(kd_mut / kd_wt)
            except (ValueError, TypeError):
                ddg_exp = None
        if ddg_exp is None:
            continue
        for single_mut in mut_field.split(','):
            parsed = _parse_skempi_mutation(single_mut.strip())
            if parsed:
                experimental.setdefault(_skempi_key_from_parsed(parsed), []).append(ddg_exp)

    experimental_avg = {k: sum(v) / len(v) for k, v in experimental.items()}
    common = sorted(set(predicted.keys()) & set(experimental_avg.keys()))
    if not common:
        return (
            f"【实验对照】{pdb_id_upper} 在 SKEMPI 中有 {len(matching)} 条记录，"
            "但未匹配到位点重叠（链无关三元组 (wt, pos, mut) 无交集）。"
        )

    pred_vals = [predicted[k] for k in common]
    exp_vals = [experimental_avg[k] for k in common]
    r, n = _pearson(pred_vals, exp_vals)

    lines = [
        f"【实验对照·探索性】{pdb_id_upper} 匹配到 {n} 个重叠位点",
        "口径说明：预测 = cartesian_ddg 常规稳定性模式 (REU)；实验 = SKEMPI 结合 ΔΔG (kcal/mol)。两者物理量不同。",
        "匹配说明：key 为链无关三元组 (wt, pos, mut)，多链同编号位点可能串位（未消除）。",
        "",
        "位点详情 (wt-pos-mut: 预测 REU vs 实验 kcal/mol):",
    ]
    for k in common:
        lines.append(f"  {k[0]}-{k[1]}-{k[2]}: {predicted[k]:+.2f}  vs  {experimental_avg[k]:+.2f}")

    if not exploratory_eval_enabled():
        lines.extend([
            "",
            "状态：未评测 —— 自动相关性计算与可信度评级默认关闭"
            "（WB_EXPLORATORY_EVAL_ENABLED=1 可开启探索性 Pearson 比对）。",
            "计算筛选 ≠ 实验验证。",
        ])
        return "\n".join(lines)

    lines.extend([
        "",
        f"探索性 Pearson r = {r:.3f}  (n={n})",
        "以上为探索性结果：预测量与实验标签物理量不同、匹配 key 链无关，不构成任何可信度结论。",
    ])
    return "\n".join(lines)


# ==========================================
# 3.7. ESM-2 "disorder" 启发式信号（v0.1：未经验证，默认不参与位点过滤）
# ==========================================
# 探索性信号展示阈值；ESM-2 分数未经无序预测校准，仅作参考，不作为「无序概率」使用
DISORDER_THRESHOLD = 0.5


def _parse_ca_atoms(pdb_path):
    """从 PDB 提取 CA 原子序列 + 每个残基的 (chain, resnum, b_factor, occupancy)

    返回 (sequence_str, [(chain, resnum, 3-letter_aa, b_factor, occupancy), ...])
    """
    residues = []
    seen = set()
    standard_aas = {"ALA", "CYS", "ASP", "GLU", "PHE", "GLY", "HIS", "ILE",
                    "LYS", "LEU", "MET", "ASN", "PRO", "GLN", "ARG", "SER",
                    "THR", "VAL", "TRP", "TYR"}
    aa3to1 = {"ALA": "A", "CYS": "C", "ASP": "D", "GLU": "E", "PHE": "F",
              "GLY": "G", "HIS": "H", "ILE": "I", "LYS": "K", "LEU": "L",
              "MET": "M", "ASN": "N", "PRO": "P", "GLN": "Q", "ARG": "R",
              "SER": "S", "THR": "T", "VAL": "V", "TRP": "W", "TYR": "Y"}
    try:
        with open(pdb_path, 'r', encoding='utf-8') as f:
            for line in f:
                if not line.startswith("ATOM"):
                    continue
                atom_name = line[12:16].strip()
                if atom_name != "CA":
                    continue
                resname = line[17:20].strip()
                if resname not in standard_aas:
                    continue
                chain = line[21].strip()
                try:
                    resnum = int(line[22:26].strip())
                    b_factor = float(line[60:66].strip())
                    occupancy = float(line[54:60].strip())
                except ValueError:
                    continue
                key = (chain, resnum)
                if key in seen:
                    continue
                seen.add(key)
                residues.append((chain, resnum, aa3to1[resname], b_factor, occupancy))
    except Exception:
        return "", []

    # 按 (chain, resnum) 排序
    residues.sort(key=lambda x: (x[0], x[1]))
    sequence = "".join(r[2] for r in residues)
    return sequence, residues


def _predict_disorder_heuristic(residues):
    """启发式 disorder 检测：基于 B-factor + 残基序号 gap

    启发：B-factor 越高、残基序号有 gap（说明中间有缺失）→ 越可能是无序区域
    返回 {chain: {resnum: disorder_score}}
    """
    if not residues:
        return {}

    # 1. 按 chain 分组
    by_chain = {}
    for chain, resnum, aa, b, occ in residues:
        by_chain.setdefault(chain, []).append((resnum, b, occ))

    disorder = {}
    for chain, items in by_chain.items():
        # B-factor 归一化到 0-1
        b_values = [b for _, b, _ in items]
        if len(b_values) >= 2:
            b_min, b_max = min(b_values), max(b_values)
        else:
            b_min, b_max = 0, 1
        b_range = max(b_max - b_min, 1e-6)

        # 残基序号 gap 检测
        resnums = [r for r, _, _ in items]
        gap_score = {}
        for i, r in enumerate(resnums):
            if i == 0:
                gap_score[r] = 0.0
            else:
                gap = r - resnums[i-1] - 1  # 缺失的残基数
                # gap 越大，越可能是无序区
                gap_score[r] = min(1.0, gap * 0.3)

        # 合并：B-factor (70%) + gap 邻域 (30%)
        for r, b, _ in items:
            b_norm = (b - b_min) / b_range
            disorder.setdefault(chain, {})[r] = 0.7 * b_norm + 0.3 * gap_score.get(r, 0.0)

    return disorder


_ESM_CACHE = {}


def _load_esm2():
    """加载并缓存 ESM-2 t12 35M 模型（模块级单例，避免同一进程内重复初始化）。

    predict_disorder / predict_disorder_struct 都会被 disorder_check 节点连续调用，
    若每次都重新初始化 35M 模型会重复耗时并占用内存。
    """
    if "model" not in _ESM_CACHE:
        import esm
        import torch
        model, alphabet = esm.pretrained.esm2_t12_35M_UR50D()
        model.eval()
        if torch.cuda.is_available():
            model = model.cuda()
        _ESM_CACHE["model"] = model
        _ESM_CACHE["alphabet"] = alphabet
    return _ESM_CACHE["model"], _ESM_CACHE["alphabet"]


def _predict_disorder_esm2(sequence, residues):
    """输出 ESM-2 对观测残基的未校准置信度启发式分数（探索性信号，非 disorder 概率）。

    实现：对未 mask 的完整序列取模型在每位点输出野生型氨基酸的 log-prob，
    经 (log_p + 3)/3 线性映射到 0-1 后取反。该实现既非 masked language modeling，
    也未在任何 disorder 数据集上校准——分数与真实无序倾向的关系未经验证（v0.1 如实标注）。

    返回 ({chain: {resnum: score}}, method_used)
    """
    import torch

    # 用 35M 模型（快且准），没有 GPU 也跑得动；模块级缓存避免重复初始化
    model, alphabet = _load_esm2()
    batch_converter = alphabet.get_batch_converter()

    _, _, batch_tokens = batch_converter([("protein", sequence)])
    if torch.cuda.is_available():
        batch_tokens = batch_tokens.cuda()

    with torch.no_grad():
        logits = model(batch_tokens)["logits"][0, 1:-1]  # 去掉 BOS/EOS

    # 取每个位置上正确氨基酸的 log_prob
    aa_to_idx = {a: alphabet.get_idx(a) for a in "ACDEFGHIKLMNPQRSTVWY"}
    seq_indices = torch.tensor([aa_to_idx.get(a, alphabet.get_idx("X")) for a in sequence])
    log_probs = torch.log_softmax(logits, dim=-1)
    per_pos_log_p = log_probs[range(len(sequence)), seq_indices]

    # 归一化到 0-1：max log_prob ≈ 0，min ≈ log(1/20) ≈ -3
    # disorder = (1 - normalized_confidence)
    confidence = (per_pos_log_p + 3.0) / 3.0  # [-3, 0] → [0, 1]
    confidence = torch.clamp(confidence, 0.0, 1.0)
    disorder_tensor = 1.0 - confidence

    # 映射回 (chain, resnum)
    result = {}
    disorder_list = disorder_tensor.cpu().tolist()
    for i, (chain, resnum, aa, b, occ) in enumerate(residues):
        result.setdefault(chain, {})[resnum] = disorder_list[i]

    return result, "ESM-2 t12 (35M)"


@tool
def predict_disorder(pdb_path: str = "") -> str:
    """【探索性信号】输出每残基的未校准启发式分数（非 disorder 概率，未经本项目验证）

    策略：
      1. 优先用 ESM-2 置信度启发式（35M 参数，本地推理）
      2. fair-esm 未装或模型加载失败时，自动回退到 PDB 信号（B-factor + 残基 gap）

    输出报告包含：
      - 总残基数、高/低信号数量
      - 按位点列出的分数（top 10 高信号）
      - 提示：该分数与真实无序倾向的关系未经验证，仅供探索性参考

    Args:
        pdb_path: PDB 文件路径。不传则自动找当前 workspace 的 cartesian_relax/complex_rosetta_ready.pdb
    """
    if not pdb_path:
        candidates = [
            os.path.join(current_workspace.get(), "cartesian_relax", "complex_rosetta_ready.pdb"),
            os.path.join(current_workspace.get(), "protein.pdb"),
        ]
        for p in candidates:
            if os.path.exists(p):
                pdb_path = p
                break
        else:
            return "【disorder 检测】未找到 PDB 文件（请指定 pdb_path 或先完成弛豫）"

    if not os.path.exists(pdb_path):
        return f"【disorder 检测】{pdb_path} 不存在"

    sequence, residues = _parse_ca_atoms(pdb_path)
    if not sequence:
        return f"【disorder 检测】{pdb_path} 无标准 CA 原子（可能非蛋白 PDB）"

    # 尝试 ESM-2，失败回退
    method = "ESM-2 t12 (35M)"
    try:
        disorder, method = _predict_disorder_esm2(sequence, residues)
    except ImportError:
        disorder = _predict_disorder_heuristic(residues)
        method = "B-factor (fair-esm 未装，回退)"
    except Exception as e:
        disorder = _predict_disorder_heuristic(residues)
        method = f"B-factor (ESM-2 失败: {e.__class__.__name__}，回退)"

    # 汇总
    total = sum(len(v) for v in disorder.values())
    n_disordered = sum(
        1 for chain_dict in disorder.values()
        for score in chain_dict.values()
        if score >= DISORDER_THRESHOLD
    )
    n_ordered = total - n_disordered

    # 找 top 10 最无序的位点
    all_sites = [
        (chain, resnum, score)
        for chain, d in disorder.items()
        for resnum, score in d.items()
    ]
    all_sites.sort(key=lambda x: -x[2])
    top_disordered = all_sites[:10]

    lines = [
        f"【探索性信号】{os.path.basename(pdb_path)}",
        f"方法: {method}（未经验证的启发式分数，非 disorder 概率）",
        f"总残基: {total} | 低信号 (<0.5): {n_ordered} | 高信号 (≥0.5): {n_disordered}  ({100*n_disordered/total:.1f}%)",
        "",
    ]

    if top_disordered:
        lines.append("Top 10 高信号位点（≥0.5 仅作展示参考）:")
        for chain, resnum, score in top_disordered:
            flag = "  ⚠️" if score >= DISORDER_THRESHOLD else ""
            lines.append(f"  {chain}_{resnum}: score={score:.3f}{flag}")

    lines.append("")
    lines.append(
        "提示: 该分数与真实无序倾向的关系未经验证（默认不参与位点过滤），"
        "不能据此断定位点不可信或结构稳定。计算筛选 ≠ 实验验证。"
    )

    return "\n".join(lines)


def predict_disorder_struct(pdb_path: str = "") -> dict:
    """【disorder 检测（结构化版）】返回 {chain: {resnum: disorder_score}} 并落盘全量 CSV

    与 predict_disorder 共享 ESM-2 / B-factor 回退逻辑，但额外：
      1. 返回 dict 供 LangGraph state / 下游节点消费
      2. 全量打分写入 <workspace>/disorder/disorder_scores.csv
         列：chain, resnum, disorder_score, is_disordered

    Args:
        pdb_path: 同 predict_disorder，不传则按 cartesian_relax → protein 顺序找

    Returns:
        {chain: {resnum: disorder_score in [0,1]}}；失败返回 {}
    """
    if not pdb_path:
        candidates = [
            os.path.join(current_workspace.get(), "cartesian_relax", "complex_rosetta_ready.pdb"),
            os.path.join(current_workspace.get(), "protein.pdb"),
        ]
        for p in candidates:
            if os.path.exists(p):
                pdb_path = p
                break
        else:
            return {}

    if not os.path.exists(pdb_path):
        return {}

    sequence, residues = _parse_ca_atoms(pdb_path)
    if not sequence:
        return {}

    try:
        disorder, _method = _predict_disorder_esm2(sequence, residues)
    except ImportError:
        disorder = _predict_disorder_heuristic(residues)
    except Exception:
        disorder = _predict_disorder_heuristic(residues)

    # 全量打分落盘 CSV（不只写无序的——下游要画分布图、做 SKEMPI 过滤都要全量）
    try:
        out_dir = os.path.join(current_workspace.get(), "disorder")
        os.makedirs(out_dir, exist_ok=True)
        csv_path = os.path.join(out_dir, "disorder_scores.csv")
        with open(csv_path, 'w', newline='', encoding='utf-8') as f:
            w = csv.writer(f)
            w.writerow(["chain", "resnum", "disorder_score", "is_disordered"])
            for chain, d in disorder.items():
                for resnum, score in sorted(d.items()):
                    w.writerow([
                        chain, resnum, f"{score:.4f}",
                        "1" if score >= DISORDER_THRESHOLD else "0",
                    ])
    except Exception:
        pass  # CSV 写失败不影响主流程

    return disorder


# ==========================================
# 3.6. 蛋白-小分子评估：PDBbind 比对（Spearman 相关性）
# ==========================================
PDBBIND_CSV_PATH = os.path.join(os.path.dirname(__file__), "rosetta_manuals", "pdbbind_v2020.csv")


def _load_pdbbind():
    """加载 PDBbind v2020 CSV。

    预期列名：pdb_id / PDB / Kd / Ki / IC50（不同版本可能略不同）。
    优先用制表符/分号分隔符；文件不存在返回 None。
    """
    if not os.path.exists(PDBBIND_CSV_PATH):
        return None
    with open(PDBBIND_CSV_PATH, 'r', encoding='utf-8') as f:
        sample = f.read(4096)
        f.seek(0)
        if '\t' in sample:
            delim = '\t'
        elif ';' in sample:
            delim = ';'
        else:
            delim = ','
        return list(csv.DictReader(f, delimiter=delim))


def _extract_pdbbind_affinity(entry):
    """从 PDBbind 记录里提取亲和力（优先 Kd > Ki > IC50）。返回 (value_M, type) 或 (None, None)。"""
    for key in ('Kd', 'Ki', 'IC50'):
        for cand in (key, key + ' (M)', key.lower()):
            val = entry.get(cand) or entry.get(key + '_parsed')
            if val:
                try:
                    v = float(val)
                    if v > 0:
                        return v, key
                except (ValueError, TypeError):
                    continue
    return None, None


def _parse_score_sc(score_sc_path):
    """读 Rosetta score.sc，返回 [{'nstruct': str, 'total_score': float}]"""
    results = []
    with open(score_sc_path, 'r', encoding='utf-8') as f:
        lines = f.readlines()
    # 找 header 行（SCORE: ... total_score ... description）
    header_idx = -1
    ts_idx = -1
    for i, line in enumerate(lines):
        if line.startswith("SCORE:") and "total_score" in line:
            header_idx = i
            parts = line.split()
            try:
                ts_idx = parts.index('total_score')
            except ValueError:
                return results
            break
    if header_idx < 0:
        return results
    # 数据行在 header 之后
    for line in lines[header_idx + 1:]:
        if line.startswith("SCORE:") and "total_score" not in line:
            parts = line.split()
            try:
                results.append({
                    'nstruct': parts[-1] if parts[-1].endswith('.pdb') else '',
                    'total_score': float(parts[ts_idx]),
                })
            except (ValueError, IndexError):
                continue
    return results


@tool
def evaluate_against_pdbbind(pdb_ids: str, score_paths: str = "") -> str:
    """【蛋白-小分子对照·探索性】把 Rosetta score.sc 的 total_score 与 PDBbind v2020 实测亲和力并排展示。

    默认（WB_EXPLORATORY_EVAL_ENABLED=0）只输出原始数据表与「未评测」状态，
    不计算 Spearman、不做任何强/中/弱评级。跨结构比较复合物 total_score 与 Kd
    缺少物理与评测依据（v0.4 前不作为可信度依据）。

    参数：
      - pdb_ids：单个 PDB ID（如 "1MC9"），或多个 PDB 列表用逗号分隔（如 "1JTG,1HVH,1HVR"）
        多 PDB 是同一蛋白不同配体（如 HIV protease 多抑制剂），用于算跨配体的 Spearman 相关性。
      - score_paths：可选，对应每个 PDB 的 score.sc 路径，用分号分隔（如 "/path/1JTG/sc;/path/1HVH/sc"）
        不传则对所有 PDB 都用当前 workspace 的 cartesian_relax/score.sc（仅单 PDB 模式合理）。

    使用前提：
      1. 已下载 PDBbind v2020（refined 或 general set）到 rosetta_manuals/pdbbind_v2020.csv
      2. 已完成 run_cartesian_relax 生成 score.sc（多 PDB 时每个 PDB 都跑一次）
    """
    pdbbind = _load_pdbbind()
    if pdbbind is None:
        return (
            "【蛋白-小分子对照】PDBbind 数据库未加载。请到 https://www.pdbbind.org.cn/download.php "
            f"下载 v2020（refined 或 general），导出为 CSV（pdb_id, Kd 列）放到 {PDBBIND_CSV_PATH}。"
        )

    # 解析 PDB ID 列表
    pdb_list = [p.strip().upper() for p in pdb_ids.split(',') if p.strip()]
    if not pdb_list:
        return "【蛋白-小分子对照】未提供 PDB ID"

    # 解析 score_paths（可选）
    default_sc = os.path.join(current_workspace.get(), "cartesian_relax", "score.sc")
    if score_paths:
        sc_list = [s.strip() for s in score_paths.split(';')]
        if len(sc_list) != len(pdb_list):
            return f"【蛋白-小分子对照】score_paths 数量 ({len(sc_list)}) 与 pdb_ids 数量 ({len(pdb_list)}) 不匹配"
    else:
        sc_list = [default_sc] * len(pdb_list)

    import math

    # 对每个 PDB 查 PDBbind + 读 score
    pdb_data = []
    for pdb_id, sc_path in zip(pdb_list, sc_list):
        # 找 PDBbind 记录
        matches = []
        for entry in pdbbind:
            pid = (entry.get('pdb_id') or entry.get('PDB') or entry.get('#Pdb') or entry.get('PDB ID') or '').upper().strip()
            if pid == pdb_id:
                aff, aff_type = _extract_pdbbind_affinity(entry)
                if aff is not None:
                    matches.append({'affinity': aff, 'type': aff_type})
        if not matches:
            return f"【蛋白-小分子对照】PDBbind 中未找到 PDB {pdb_id} 的记录"

        # 读 Rosetta score（取 best total_score）
        rosetta = _parse_score_sc(sc_path)
        if not rosetta:
            return f"【蛋白-小分子对照】{pdb_id}: 未在 {sc_path} 读到 Rosetta 评分"
        best_score = min(r['total_score'] for r in rosetta)

        # 取第一条 PDBbind 记录（同一 PDB 应该只有一条）
        m = matches[0]
        pdb_data.append({
            'pdb_id': pdb_id,
            'rosetta_score': best_score,
            'affinity': m['affinity'],
            'aff_type': m['type'],
            'log_aff': -math.log10(m['affinity']),
            'nstruct': len(rosetta),
        })

    # 报告
    lines = [
        f"【蛋白-小分子对照·探索性】共 {len(pdb_data)} 个 PDB",
        "",
        f"  {'PDB':<14}{'Affinity(M)':<18}{'-log10 Aff':<14}{'Rosetta(REU)':<14}{'nstruct'}",
    ]
    for d in pdb_data:
        lines.append(
            f"  {d['pdb_id']:<14}{d['aff_type']}={d['affinity']:.2e}  "
            f"{d['log_aff']:>10.2f}    {d['rosetta_score']:>+12.2f}    {d['nstruct']}"
        )

    lines.append("")
    lines.append(
        "口径说明：total_score 为复合物折叠+界面能量的综合 REU 值，不是结合自由能；"
        "跨体系与 Kd 并排仅为展示。"
    )
    if not exploratory_eval_enabled():
        lines.extend([
            "状态：未评测 —— 自动 Spearman 相关性与评级默认关闭"
            "（WB_EXPLORATORY_EVAL_ENABLED=1 可开启探索性比对）。",
            "计算筛选 ≠ 实验验证。",
        ])
        return "\n".join(lines)

    if len(pdb_data) < 3:
        lines.extend([
            f"⚠️ 只 {len(pdb_data)} 个 PDB，探索性 Spearman 需要 ≥3 个数据点。",
            "  多配体评估：把 HIV protease / thrombin / carbonic anhydrase 的多个 PDB ID 都跑 run_cartesian_relax，",
            "  再用 'pdb_ids=1JTG,1HVH,1HVR,...;score_paths=/path1/sc;/path2/sc;...' 调用本工具。",
        ])
        return "\n".join(lines)

    # Spearman 计算（仅显式开启探索性对照时执行；输出不附带任何评级）
    rosetta_vals = [d['rosetta_score'] for d in pdb_data]
    aff_vals = [d['log_aff'] for d in pdb_data]
    rho, n = _spearman(rosetta_vals, aff_vals)

    lines.extend([
        f"探索性 Spearman ρ = {rho:.3f}  (n={n}, 期望负相关：Rosetta 越低 ≈ Kd 越小)",
        "以上为探索性结果：样本量小、跨体系比较 total_score 缺乏依据，不构成可信度结论。",
    ])

    return "\n".join(lines)


# ==========================================
# 4. RAG 文档检索工具
# ==========================================
DOCS_DIR = "rosetta_manuals"


def _load_doc_chunks():
    """加载 rosetta_manuals 下全部 .txt/.md 语料（递归子目录）并切块。

    语料来源：rosetta_docs/ 下的 RosettaCommons/documentation 官方文档 + 本地报错笔记。
    """
    docs = []
    for root, _dirs, files in os.walk(DOCS_DIR):
        for fn in sorted(files):
            if not fn.lower().endswith((".txt", ".md")):
                continue
            path = os.path.join(root, fn)
            try:
                docs.extend(TextLoader(path, encoding="utf-8").load())
            except Exception:
                continue
    if not docs:
        return []
    text_splitter = RecursiveCharacterTextSplitter(chunk_size=600, chunk_overlap=60)
    return text_splitter.split_documents(docs)


def init_bm25_retriever():
    """构建 BM25 本地关键词检索器（零 API 依赖，主检索方案）。

    对 Rosetta 报错代码/参数名这类场景，关键词精确匹配往往比向量检索更可靠。
    """
    try:
        from langchain_community.retrievers import BM25Retriever
        splits = _load_doc_chunks()
        if not splits:
            print("⚠️ RAG：rosetta_manuals 下未找到 .txt/.md 语料，检索不可用")
            return None
        return BM25Retriever.from_documents(splits, k=4)
    except Exception as e:
        print(f"⚠️ RAG：BM25 检索器构建失败: {e}")
        return None


def init_vector_db():
    """构建向量数据库（可选增强；embedding API 不可用时优雅降级为 None）"""
    try:
        # 让 Embeddings 使用 .env 里配置好的 API Key 和代理 URL；
        # 中转站不支持 embedding 或需指定模型时，可在 .env 设 EMBEDDING_MODEL
        embeddings_kwargs = dict(
            api_key=os.getenv("EMBED_API_KEY", os.getenv("LLM_API_KEY", "")),
            base_url=os.getenv("EMBED_BASE_URL", os.getenv("LLM_BASE_URL", "")),
            model=os.getenv("EMBEDDING_MODEL", "BAAI/bge-m3"),
            chunk_size=16,
        )
        embeddings = OpenAIEmbeddings(**embeddings_kwargs)

        if os.path.exists("chroma_db"):
            return Chroma(persist_directory="chroma_db", embedding_function=embeddings)

        splits = _load_doc_chunks()
        if splits:
            return Chroma.from_documents(documents=splits, embedding=embeddings, persist_directory="chroma_db")
    except Exception as e:
        print(f"⚠️ 向量库构建失败（embedding API 不可用，回退到 BM25）: {e}")
        # 清理构建失败留下的空/损坏缓存
        try:
            if os.path.isdir("chroma_db"):
                shutil.rmtree("chroma_db")
        except Exception:
            pass
    return None


bm25_retriever = init_bm25_retriever()
vector_db = init_vector_db()


@tool
def search_rosetta_docs(query: str) -> str:
    """RAG 检索工具：检索 Rosetta 官方文档/报错笔记，遇到未知的参数、报错代码、用法时调用。"""
    docs = []
    source = None

    # 1. 优先向量语义检索（embedding API 可用时）
    if vector_db is not None:
        try:
            docs = vector_db.similarity_search(query, k=4)
            source = "向量检索"
        except Exception:
            docs = []

    # 2. 回退到 BM25 本地关键词检索（零 API 依赖）
    if not docs and bm25_retriever is not None:
        try:
            docs = bm25_retriever.invoke(query)
            source = "BM25 关键词检索"
        except Exception:
            docs = []

    if not docs:
        return "【检索结果】Rosetta 知识库中未找到相关内容（请检查 rosetta_manuals 语料）。"
    context = "\n---\n".join(d.page_content[:1500] for d in docs)
    return f"【检索成功·{source}】以下是 Rosetta 知识库的参考资料：\n{context}"


# ==========================================
# 5. 扩展分析与设计工具（v0.2 新工具包：score / SASA / 界面分析 / fixbb 设计）
# 旗标均已于 2026-11-14 用真实任务数据在本机冒烟验证（wt_relaxed.pdb + XYL.params）：
#   - score.linuxgccrelease -s pdb [-extra_res_fa x] -out:file:scorefile x.sc
#   - InterfaceAnalyzer.linuxgccrelease -s pdb [-extra_res_fa x] -pack_separated -packstat -out:file:scorefile x.sc
#     ⚠️ 本构建默认即计算 dG_separated/dG_cross 等列；命令行传 -dg_separated/-dG_cross
#        （无论大小写）都会 "option not found" 崩溃——文档中的列名≠旗标名，勿照抄。
#   - calculate_sasa.linuxgccrelease -s pdb [-extra_res_fa x]（总 SASA 输出在 stdout）
#   - fixbb.linuxgccrelease -s pdb -resfile rf.txt -nstruct N -ex1 -ex2 [-extra_res_fa x]
# ==========================================

def _resolve_structure_input(pdb_path: str = ""):
    """扩展工具通用默认结构选择：弛豫择优 → 清洗复合物 → 原始上传。返回 (绝对路径, 错误消息)。"""
    if pdb_path and pdb_path.strip():
        p = pdb_path if os.path.isabs(pdb_path) else os.path.join(current_workspace.get(), pdb_path)
        if not os.path.exists(p):
            return None, f"【执行报错】文件不存在: {p}"
        return os.path.abspath(p), None
    for cand in (
        os.path.join(current_workspace.get(), "cartesian_relax", "wt_relaxed.pdb"),
        os.path.join(current_workspace.get(), "cartesian_relax", "complex_rosetta_ready.pdb"),
        os.path.join(current_workspace.get(), "protein.pdb"),
    ):
        if os.path.exists(cand):
            return os.path.abspath(cand), None
    return None, "【执行报错】未找到可用结构。请指定 pdb_path，或先完成弛豫（cartesian_relax/wt_relaxed.pdb）。"


def _params_extra_args():
    """工作区存在配体参数时返回 ['-extra_res_fa', 路径]，否则空列表。"""
    params_files = glob.glob(os.path.join(current_workspace.get(), "parameterize_ligand", "*.params"))
    if params_files:
        return ["-extra_res_fa", os.path.abspath(params_files[0])]
    return []


def _read_rosetta_score_file(path: str) -> dict:
    """读 Rosetta score 文件第一行数据行 → {列名: 值}。列值保留字符串，调用方自行转 float。

    兼容两种表头：新应用用 total_score（fixbb/InterfaceAnalyzer），旧 score 应用用 score。
    """
    header = None
    try:
        with open(path, encoding="utf-8", errors="ignore") as f:
            for line in f:
                if not line.startswith("SCORE:"):
                    continue
                parts = line.split()[1:]
                if header is None:
                    if "total_score" in parts or "score" in parts:
                        header = parts
                    continue
                row = dict(zip(header, parts))
                row.pop("description", None)
                if "total_score" not in row and "score" in row:
                    row["total_score"] = row["score"]
                return row
    except Exception:
        pass
    return {}


def _fmt_row(row: dict, columns, labels) -> str:
    lines = []
    for col, label in zip(columns, labels):
        if col in row:
            lines.append(f"  {label}({col}) = {row[col]}")
    return "\n".join(lines)


@tool
def score_structure(pdb_path: str = "") -> str:
    """用 Rosetta ref2015 全原子打分函数对结构打分（REU），返回 total_score 与主要能量项。轻量同步计算。

    默认结构顺序：cartesian_relax/wt_relaxed.pdb → complex_rosetta_ready.pdb → protein.pdb；也可指定 pdb_path。

    口径说明：REU 是 Rosetta 能量单位，仅可用于比较不同结构/协议的相对合理性，
    不等于结合自由能，也不是实验测量值（计算筛选 ≠ 实验验证）。
    """
    pdb, err = _resolve_structure_input(pdb_path)
    if err:
        return err
    out_dir = get_workspace_path("analysis")
    # 旗标只传文件名：Rosetta 的 cwd 已是 out_dir，传相对工作区的路径会拼出不存在目录而崩溃
    sc_path = os.path.join(out_dir, "score_out.sc")
    cmd = [os.path.join(ROSETTA_BIN, "score.linuxgccrelease"),
           "-s", pdb, "-out:file:scorefile", "score_out.sc", "-mute", "all"] + _params_extra_args()
    try:
        proc = subprocess.run(cmd, cwd=out_dir, capture_output=True, text=True, timeout=1800)
    except subprocess.TimeoutExpired:
        return "【执行报错】打分超时（>30 分钟）。大复合物建议先弛豫再打分。"
    row = _read_rosetta_score_file(sc_path) if os.path.exists(sc_path) else {}
    if not row:
        tail = "\n".join((proc.stderr or "").strip().splitlines()[-5:])
        return f"【执行报错】打分未产出 score 文件。{tail}"
    detail = _fmt_row(row,
                      ["total_score", "fa_atr", "fa_rep", "fa_sol", "fa_elec", "lk_ball_wtd", "dslf_fa13"],
                      ["总得分", "范德华吸引", "范德华排斥", "溶剂化", "静电", "溶剂化极性", "二硫键"])
    return (f"【打分完成】{os.path.basename(pdb)}（ref2015）\n{detail}\n"
            "单位: REU（Rosetta 能量单位），仅代表相对物理合理性，不等于实验值。")


@tool
def calculate_sasa(pdb_path: str = "") -> str:
    """计算结构总溶剂可及面积 SASA（Å²，LeGrand 精确算法，默认 1.4 Å 探针半径）。轻量同步计算。"""
    pdb, err = _resolve_structure_input(pdb_path)
    if err:
        return err
    out_dir = get_workspace_path("analysis")
    cmd = [os.path.join(ROSETTA_BIN, "calculate_sasa.linuxgccrelease"),
           "-s", pdb, "-mute", "all"] + _params_extra_args()
    try:
        proc = subprocess.run(cmd, cwd=out_dir, capture_output=True, text=True, timeout=1200)
    except subprocess.TimeoutExpired:
        return "【执行报错】SASA 计算超时。"
    m = re.search(r"Solvent Accessible Surface Area is:\s*([0-9.]+)", proc.stdout or "")
    if not m:
        tail = "\n".join((proc.stderr or proc.stdout or "").strip().splitlines()[-5:])
        return f"【执行报错】未读到 SASA 结果。{tail}"
    total = float(m.group(1))
    n_res = len(glob.glob(os.path.join(current_workspace.get(), "saturation_mutagenesis", "*.ddg")))
    note = f"（该任务已有 {n_res} 个突变 ddG 数据）" if n_res else ""
    return (f"【SASA 计算完成】{os.path.basename(pdb)}\n  总溶剂可及面积 = {total:.1f} Å² {note}\n"
            "如需界面 SASA（dSASA_int），请对多链复合物调用 analyze_interface。")


@tool
def analyze_interface(pdb_path: str = "") -> str:
    """蛋白-蛋白多链复合物界面分析（InterfaceAnalyzer，ref2015）：
    dG_separated / dG_cross / dSASA_int / packstat / sc_value / complex_normalized。

    口径与边界（如实声明）：
      - 仅对至少 2 条蛋白链的复合物有意义，单链蛋白会如实返回不适用；
      - dG_separated/dG_cross 是 Rosetta 界面能量估计（REU），不是实验结合自由能；
      - 本构建的界面指标默认全算，packstat 为界面堆积质量的经验分（0-1）。
    """
    pdb, err = _resolve_structure_input(pdb_path)
    if err:
        return err
    chains = set()
    with open(pdb, encoding="utf-8", errors="ignore") as f:
        for line in f:
            if line.startswith("ATOM") and line[12:16].strip() == "CA":
                chains.add(line[21].strip())
    if len(chains) < 2:
        return (f"【不适用】{os.path.basename(pdb)} 仅含 {len(chains)} 条蛋白链"
                f"（{','.join(sorted(chains)) if chains else '无'}）。"
                "InterfaceAnalyzer 需要蛋白-蛋白多链复合物；单链蛋白请用 score_structure / calculate_sasa。")
    out_dir = get_workspace_path("analysis")
    sc_path = os.path.join(out_dir, "interface_out.sc")
    cmd = [os.path.join(ROSETTA_BIN, "InterfaceAnalyzer.linuxgccrelease"),
           "-s", pdb, "-pack_separated", "-packstat",
           "-out:file:scorefile", "interface_out.sc", "-mute", "all"] + _params_extra_args()
    try:
        proc = subprocess.run(cmd, cwd=out_dir, capture_output=True, text=True, timeout=1800)
    except subprocess.TimeoutExpired:
        return "【执行报错】界面分析超时（>30 分钟）。"
    row = _read_rosetta_score_file(sc_path) if os.path.exists(sc_path) else {}
    if not row:
        tail = "\n".join((proc.stderr or "").strip().splitlines()[-5:])
        return f"【执行报错】界面分析未产出 score 文件。{tail}"
    detail = _fmt_row(row,
                      ["dG_separated", "dG_cross", "complex_normalized", "dSASA_int",
                       "dSASA_hphobic", "dSASA_polar", "nres_int", "packstat", "sc_value"],
                      ["分离链界面能 dG_sep", "交叉界面能 dG_cross", "归一化复合物能",
                       "界面 SASA", "界面疏水 SASA", "界面极性 SASA", "界面残基数",
                       "堆积质量 packstat", "形状互补 sc"])
    return (f"【界面分析完成】{os.path.basename(pdb)}（蛋白链: {','.join(sorted(chains))}）\n{detail}\n"
            "口径: dG_separated/dG_cross 为 Rosetta 界面能量估计（REU），非实验结合自由能；"
            "可与 SKEMPI 同位点结合 ΔΔG 做探索性对照，不构成可信度结论。")


@tool
def run_fixbb_design(positions: str, nstruct: int = 5) -> str:
    """固定骨架多点设计（fixbb）：指定若干位点放开为全部 20 种氨基酸，packer 给出最优侧链组合。

    使用前提：工作区已有弛豫产物（cartesian_relax/wt_relaxed.pdb 或 complex_rosetta_ready.pdb）。
    positions: 位点列表，如 "A_21,A_23"（链_PDB编号）；纯数字默认取结构第一条蛋白链。
    nstruct: 设计克隆数（不同随机种子），默认 5。

    后台执行（分钟级），完成后在 <工作区>/fixbb_design/design_summary.txt 生成汇总：
    各克隆 total_score、最优克隆、逐位点氨基酸变化。可用 read_file 查看。
    口径与限制（fixbb 官方文档三条 limitation）：固定骨架不能保证新序列真实折叠；
    随机性导致各克隆结果不同；打分函数不完美——设计结果是计算候选，需实验验证。
    """
    import re as _re
    relax_dir = os.path.join(current_workspace.get(), "cartesian_relax")
    wt_pdb = os.path.join(relax_dir, "wt_relaxed.pdb")
    ready_pdb = os.path.join(relax_dir, "complex_rosetta_ready.pdb")
    if os.path.exists(wt_pdb):
        pdb = wt_pdb
    elif os.path.exists(ready_pdb):
        pdb = ready_pdb
    else:
        return ("【执行报错】未找到弛豫产物（cartesian_relax/wt_relaxed.pdb）。"
                "固定骨架设计应在弛豫后的结构上进行，请先运行 run_cartesian_relax。")

    nmap = _build_numbering_impl(pdb)
    first_chain = nmap.chains[0] if nmap.chains else ""

    # 解析与校验位点：与突变/预检共用同一解析实现（P0 收敛）。编号不存在直接拒绝。
    # 行为变化：无链编号在多链重号时现在报错（旧版静默取第一条链）。
    raw_tokens = _split_tokens(positions)
    if not raw_tokens:
        return "【执行报错】未提供设计位点。格式如 A_21,A_23（链_PDB编号）；单链结构可用纯编号 21。"
    resolved, errors = _resolve_targets(raw_tokens, nmap)
    if errors:
        detail = "; ".join(f"{e['token']}({e['reason']})" for e in errors)
        return (f"【执行报错】设计位点解析失败: {detail}（共 {nmap.size} 个残基，"
                f"第一条蛋白链为 {first_chain or '无'}）。请用 get_rosetta_numbering 核对编号。")
    design_spec = [(t["numkey"], t["chain"], t["wt"]) for t in resolved]

    out_dir = get_workspace_path("fixbb_design")
    # 绝对化：runner 进程的 cwd 已是 out_dir，内部再以相对路径开子进程会拼错目录
    out_dir = os.path.abspath(out_dir)
    resfile_path = os.path.join(out_dir, "design.resfile")
    with open(resfile_path, "w", encoding="utf-8") as f:
        f.write("NATRO\nstart\n")
        for resi, chain, _wt in design_spec:
            f.write(f"{resi} {chain} ALLAA\n")

    abs_pdb = os.path.abspath(pdb)
    abs_res = os.path.abspath(resfile_path)
    nstruct = max(1, min(int(nstruct), 20))
    spec_str = " ".join(f"{ch}_{resi}({wt})" for resi, ch, wt in design_spec)
    extra_args = _params_extra_args()  # ["-extra_res_fa", path] 或 []

    # 生成独立 runner：不 import 本模块（避免重复初始化 RAG），逐位点解析用内联 CA 解析
    runner_py = f"""import subprocess, os
out_dir = {out_dir!r}
cmd = [
    {os.path.join(ROSETTA_BIN, 'fixbb.linuxgccrelease')!r},
    "-s", {abs_pdb!r}, "-resfile", {abs_res!r},
    "-nstruct", "{nstruct}", "-ex1", "-ex2",
    "-out:prefix", "fx_", "-out:file:scorefile", "fx.sc", "-mute", "all",
] + {extra_args!r}
try:
    proc = subprocess.run(cmd, cwd=out_dir, capture_output=True, text=True, timeout=14400)
    rc = proc.returncode
except subprocess.TimeoutExpired:
    rc = -999

def read_score(path):
    header, rows = None, []
    try:
        with open(path, errors="ignore") as f:
            for line in f:
                if not line.startswith("SCORE:"):
                    continue
                parts = line.split()[1:]
                if header is None:
                    if "total_score" in parts:
                        header = parts
                    continue
                rows.append(dict(zip(header, parts)))
    except Exception:
        pass
    return rows

def parse_ca(pdb_path):
    aa3to1 = {dict(_AA_3TO1)!r}
    out = {{}}
    try:
        with open(pdb_path, errors="ignore") as f:
            for line in f:
                if line.startswith("ATOM") and line[12:16].strip() == "CA":
                    out[(line[21].strip(), line[22:26].strip())] = aa3to1.get(line[17:20].strip(), "?")
    except Exception:
        pass
    return out

rows = read_score(os.path.join(out_dir, "fx.sc"))
lines = ["固定骨架设计完成（fixbb，ref2015）",
         f"输入结构: {os.path.basename(abs_pdb)}",
         f"设计位点: {spec_str}",
         f"克隆数: {nstruct}   退出码: {{rc}}"]
if rows:
    def _ts(r):
        try:
            return float(r.get("total_score", "nan"))
        except (ValueError, TypeError):
            return float("inf")
    lines.append("各克隆 total_score(REU):")
    for r in rows:
        lines.append(f"    {{r.get('description', '')}}: {{r.get('total_score', '')}}")
    best = min(rows, key=_ts)
    best_name = best.get("description", "")
    lines.append(f"最优克隆: {{best_name}}（total_score = {{best.get('total_score', '')}} REU）")
    best_pdb = os.path.join(out_dir, best_name if best_name.endswith(".pdb") else best_name + ".pdb")
    if os.path.exists(best_pdb):
        ca = parse_ca(best_pdb)
        lines.append("逐位点变化（最优克隆）:")
        for resi, chain, wt in {design_spec!r}:
            mut = ca.get((chain, resi), "?")
            mark = "（未变）" if mut == wt else ""
            lines.append(f"    {{chain}}_{{resi}}: {{wt}} → {{mut}}{{mark}}")
else:
    lines.append("未解析到任何 score 行——请检查 design_launcher.log（常见原因: 位点与配体冲突、参数错误）。")
lines.append("口径: 固定骨架设计不能保证新序列真实折叠（fixbb 官方 limitation），设计结果是计算候选，需实验验证。")
with open(os.path.join(out_dir, "design_summary.txt"), "w", encoding="utf-8") as f:
    f.write("\\n".join(lines) + "\\n")
print("design_summary written")
"""
    runner_path = os.path.join(out_dir, "run_design.py")
    with open(runner_path, "w", encoding="utf-8") as f:
        f.write(runner_py)
    try:
        with open(os.path.join(out_dir, "design_launcher.log"), "w") as log_f:
            subprocess.Popen([sys.executable, "-u", "run_design.py"],
                             cwd=out_dir, stdout=log_f, stderr=subprocess.STDOUT,
                             start_new_session=True)
    except Exception as e:
        return f"【系统异常】设计任务启动失败: {e}"
    return (f"【设计任务已挂起】位点 {spec_str}，nstruct={nstruct}。"
            f"完成后用 read_file 读取 fixbb_design/design_summary.txt 查看最优克隆与逐位点变化。")