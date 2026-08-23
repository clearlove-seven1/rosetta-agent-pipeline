import os
import glob
import shutil
import time
import subprocess
import contextvars
from dotenv import load_dotenv

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

def get_workspace_path(sub_dir: str = "") -> str:
    """获取当前任务的独立目录，并自动创建"""
    base_dir = current_workspace.get()
    full_path = os.path.join(base_dir, sub_dir) if sub_dir else base_dir
    os.makedirs(full_path, exist_ok=True)
    return full_path

# ==========================================
# 1. 基础读取与诊断工具
# ==========================================
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
        result = subprocess.run(f"tail -n {lines} {log_path}", shell=True, capture_output=True, text=True)
        if result.returncode == 0:
            return f"【日志截取成功】{log_path} 最后 {lines} 行：\n{result.stdout}"
        return f"【日志截取报错】{result.stderr}"
    except Exception as e:
        return f"【系统异常】{str(e)}"

# ==========================================
# 1.5 编号体系核心：唯一负责 PDB<->Rosetta 映射的实现
# ==========================================
_AA_3TO1 = {
    "ALA": "A", "CYS": "C", "ASP": "D", "GLU": "E", "PHE": "F", "GLY": "G",
    "HIS": "H", "ILE": "I", "LYS": "K", "LEU": "L", "MET": "M", "ASN": "N",
    "PRO": "P", "GLN": "Q", "ARG": "R", "SER": "S", "THR": "T", "VAL": "V",
    "TRP": "W", "TYR": "Y",
}

def _build_rosetta_numbering(pdb_path: str):
    """扫描 PDB 中所有 CA 原子，一次性构建双向编号映射（模块内唯一实现）。

    返回三元组：
      rosetta_to_pdb : {Rosetta绝对行号(int): "链_PDB编号(str)"}
      pdb_to_rosetta : {"链_PDB编号": 绝对行号, "PDB编号": 绝对行号}（兼容不带链的纯数字查询）
      wt_aa_dict     : {Rosetta绝对行号(int): 单字母氨基酸(str)}

    ⚠️ 本函数是整个流水线中【唯一】负责 PDB<->Rosetta 编号转换的实现。
    调用方拿到结果后严禁再做二次映射，否则会导致突变位点双重偏移错位。
    """
    rosetta_to_pdb = {}
    pdb_to_rosetta = {}
    wt_aa_dict = {}
    rosetta_idx = 1
    with open(pdb_path, 'r') as f:
        for line in f:
            if line.startswith("ATOM") and line[12:16].strip() == "CA":
                chain = line[21].strip()
                pdb_res = line[22:26].strip()
                strict_key = f"{chain}_{pdb_res}"
                rosetta_to_pdb[rosetta_idx] = strict_key
                pdb_to_rosetta[strict_key] = rosetta_idx
                # 兼容不带链的纯数字查询（多链重复残基号时取第一个）
                if pdb_res not in pdb_to_rosetta:
                    pdb_to_rosetta[pdb_res] = rosetta_idx
                wt_aa_dict[rosetta_idx] = _AA_3TO1.get(line[17:20].strip())
                rosetta_idx += 1
    return rosetta_to_pdb, pdb_to_rosetta, wt_aa_dict



@tool
def get_rosetta_numbering(pdb_path: str, target_resnums: str, chain_id: str = "A") -> str:
    """【只读核对工具】展示 PDB 晶体学编号 <-> Rosetta 绝对行号的映射关系（不修改任何文件）。

    用于批量突变前人工核对编号映射是否正确。
    注意：本工具输出的绝对行号仅供【查看核对】，切勿把它传给 run_saturation_mutagenesis！
    run_saturation_mutagenesis 接收的是【PDB 晶体学编号】（如 76,73），内部会自动完成转换。
    """

    # === 优先使用清洗后的结构 ===
    relax_pdb = os.path.join(current_workspace.get(), "tool3_relax", "complex_rosetta_ready.pdb")
    if os.path.exists(relax_pdb):
        pdb_path = relax_pdb
    elif not os.path.isabs(pdb_path):
        pdb_path = os.path.join(current_workspace.get(), pdb_path)

    if not os.path.exists(pdb_path):
        return f"【执行报错】未找到 {pdb_path}。请确认文件路径。"

    try:
        # 1. 兼容多位点解析
        targets = [int(x.strip()) for x in str(target_resnums).split(",") if x.strip().isdigit()]
        if not targets:
            return "【执行报错】未提供有效的晶体学编号，请确保输入格式如 '76' 或 '76, 21'。"

        # 2. 复用唯一映射实现（杜绝与 run_saturation_mutagenesis 出现两套编号逻辑）
        rosetta_to_pdb, pdb_to_rosetta, wt_aa_dict = _build_rosetta_numbering(pdb_path)

        # 3. 结果汇总（只读核对，绝不引导 Agent 传递绝对行号）
        detail_str = []
        matched_any = False
        for t in targets:
            key = f"{chain_id}_{t}" if chain_id else str(t)
            if key in pdb_to_rosetta:
                r_idx = pdb_to_rosetta[key]
                wt = wt_aa_dict.get(r_idx, "?")
                detail_str.append(f"PDB {chain_id}_{t} ({wt}) -> Rosetta绝对行号 {r_idx}")
                matched_any = True
            else:
                detail_str.append(f"PDB {chain_id}_{t} -> 【未找到】")

        if not matched_any:
            return f"【执行报错】未找到对应的 CA 原子。请检查链 ID ({chain_id}) 和晶体学编号是否正确。"

        return (
            "【映射核对】\n" + "\n".join(detail_str) +
            "\n\n【编号体系铁律】run_saturation_mutagenesis 接收【PDB 晶体学编号】并自动完成转换。"
            "请把原始 PDB 编号（如 76,73）原样传给 run_saturation_mutagenesis，"
            "绝对不要把上面的绝对行号再传给它，否则会导致双重偏移错位！"
        )

    except Exception as e:
        return f"【异常】: {str(e)}"

@tool
def parameterize_ligand(ligand_path: str = "ligand.pdb") -> str:
    """自动侦测 3 字母代号并生成参数。支持输入 PDB 自动调 PyMOL 加氢并转 MOL2。"""
    if not os.path.isabs(ligand_path):
        ligand_path = os.path.join(current_workspace.get(), ligand_path)
        
    if not os.path.exists(ligand_path):
        return f"【执行报错】未找到 {ligand_path}"
    
    out_dir = get_workspace_path("tool2_params")
    
    # 1. 判断并执行 PDB 转 MOL2 (加氢)
    file_ext = os.path.splitext(ligand_path)[1].lower()
    if file_ext == ".pdb":
        mol2_filename = os.path.basename(ligand_path).replace(".pdb", ".mol2")
        mol2_path = os.path.join(out_dir, mol2_filename)
        
        pymol_cmds = f"load {ligand_path}; h_add; save {mol2_path}"
        cmd = f"{PYMOL_BIN} -c -d '{pymol_cmds}'"
        
        try:
            subprocess.run(cmd, shell=True, check=True, capture_output=True)
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

    # 3. 运行 Rosetta 参数化脚本
    script_path = os.path.join(ROSETTA_SCRIPTS, "molfile_to_params.py")
    abs_mol2_path = os.path.abspath(mol2_path)
    cmd = f"cd {out_dir} && python {script_path} -n {ligand_name} -p {ligand_name} --keep-names --clobber {abs_mol2_path}"
    
    try:
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
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
        
    out_dir = get_workspace_path("tool3_relax")
    params_dir = get_workspace_path("tool2_params")
    
    # 增加 IO 延迟重试，解决找不到 params 的报错
    params_file = None
    for _ in range(5):
        params_files = glob.glob(f"{params_dir}/*.params")
        if params_files:
            params_file = os.path.abspath(params_files[0])
            break
        time.sleep(1)
        
    if not params_file:
        return "【执行报错】连续 5 次探测未找到 .params 文件，请确认 tool2 是否正确执行。"
        
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
            for line in f_in:
                if line.startswith("ATOM"):
                    res_name = line[17:20].strip()
                    alt_loc = line[16]
                    if res_name not in standard_aas: continue
                    if alt_loc not in [' ', 'A', '1']: continue
                    if alt_loc != ' ': line = line[:16] + ' ' + line[17:]
                    f_out.write(line)
            f_out.write("TER\n")
            if os.path.exists(ligand_pdb):
                with open(ligand_pdb, 'r') as f_lig:
                    for line in f_lig:
                        if line.startswith("HETATM") or line.startswith("ATOM"): f_out.write(line)
    except Exception as e:
        return f"【系统异常】组装报错: {str(e)}"

    rosetta_relax_bin = os.path.join(ROSETTA_BIN, "relax.linuxgccrelease")
    abs_ready_pdb = os.path.abspath(ready_pdb)
    
    runner_py = f"""import os
import time
import subprocess
from concurrent.futures import ThreadPoolExecutor

def execute(i):
    cmd = (
        f"{rosetta_relax_bin} -s {abs_ready_pdb} -extra_res_fa {params_file} "
        f"-relax:cartesian -score:weights ref2015_cart -relax:min_type lbfgs_armijo_nonmonotone "
        f"-relax:constrain_relax_to_start_coords -relax:coord_constrain_sidechains -use_input_sc "
        f"-ignore_unrecognized_res -ignore_zero_occupancy -missing_density_to_jump "
        f"-nstruct 1 -out:prefix cart_relax_{{i}}_ -out:file:scorefile score_{{i}}.sc > relax_run_{{i}}.log 2>&1"
    )
    subprocess.run("unset DISPLAY && " + cmd, shell=True)

print(f" 开始多核并发弛豫 (共 {nstruct} 个构象)...")
with ThreadPoolExecutor(max_workers={nstruct}) as pool:
    for i in range(1, {nstruct} + 1):
        pool.submit(execute, i)

print(" 合并打分文件...")
with open("score.sc", "w") as fout:
    fout.write("SEQUENCE:\\nSCORE: total_score description\\n")
    for i in range(1, {nstruct} + 1):
        try:
            with open(f"score_{{i}}.sc", "r") as fin:
                for line in fin:
                    if line.startswith("SCORE:") and "description" not in line:
                        fout.write(line)
        except Exception:
            pass
print(" 弛豫全流程结束！")
"""
    with open(os.path.join(out_dir, "run_relax_parallel.py"), "w") as f:
        f.write(runner_py)

    try:
        subprocess.Popen(f"cd {out_dir} && nohup python -u run_relax_parallel.py > master_relax.log 2>&1 &", shell=True)
        return f"【计算已挂起】已调动后台多核并发执行 {nstruct} 个弛豫任务。"
    except Exception as e:
        return f"【系统异常】{str(e)}"

@tool
def run_saturation_mutagenesis(target_resnum: str, jobs: int = 10, delay: float = 1.5) -> str:
    """高通量错峰并发流水线：自动等待弛豫完成进行突变计算。"""
    relax_dir = get_workspace_path("tool3_relax")
    mut_dir = get_workspace_path("tool4_mut_results")
    sum_dir = get_workspace_path("tool5_summary")
    params_dir = get_workspace_path("tool2_params")
    
    params_file = None
    for _ in range(5):
        params_files = glob.glob(f"{params_dir}/*.params")
        if params_files:
            params_file = os.path.abspath(params_files[0])
            break
        time.sleep(1)
        
    if not params_file:
        return "【执行报错】探测未找到 .params 文件，请确认 tool2 是否正确执行。"
    
    ready_pdb_path = os.path.join(relax_dir, "complex_rosetta_ready.pdb")
    if not os.path.exists(ready_pdb_path):
        return "【执行报错】未找到初始拼装结构 complex_rosetta_ready.pdb，tool3可能未正确执行。"

    # 接收大模型传来的【PDB 晶体学编号】（如 76,73），增加超级健壮的去重和字符清理
    import re
    raw_str = str(target_resnum).replace("'", "").replace('"', "").replace("[", "").replace("]", "")
    pdb_targets = [x.strip() for x in raw_str.split(",") if x.strip()]

    all_aas = ["A", "C", "D", "E", "F", "G", "H", "I", "K", "L", "M", "N", "P", "Q", "R", "S", "T", "V", "W", "Y"]

    # 1. 唯一一次 PDB -> Rosetta 绝对行号映射（复用共享实现，杜绝双重映射）
    rosetta_to_pdb, pdb_to_rosetta, wt_aa_dict = _build_rosetta_numbering(ready_pdb_path)

    # 2. 把【PDB 编号】映射为 Rosetta 绝对行号（含正则退化匹配）
    targets = []
    for p in pdb_targets:
        if p in pdb_to_rosetta:
            targets.append(pdb_to_rosetta[p])
        else:
            # 暴力提取纯数字进行退化匹配，无视大模型可能附加的乱码
            num_match = re.search(r'\d+', p)
            if num_match and num_match.group() in pdb_to_rosetta:
                targets.append(pdb_to_rosetta[num_match.group()])
            else:
                return f"【执行报错】无法在清洗后的结构中找到目标 PDB 晶体学编号 {p}，请检查该位点是否被剔除（或发生了文件并发读取冲突）。"

    tasks_file = os.path.join(mut_dir, "rosetta_tasks.txt")
    rosetta_ddg_bin = os.path.join(ROSETTA_BIN, "cartesian_ddg.linuxgccrelease")
    abs_wt_relaxed = os.path.abspath(os.path.join(relax_dir, "wt_relaxed.pdb"))
    
    with open(tasks_file, 'w') as f_tasks:
        for t_res in targets:
            wt_aa = wt_aa_dict.get(t_res)
            pdb_num = rosetta_to_pdb.get(t_res)
            if not wt_aa or not pdb_num: 
                f_tasks.write(f"# ERROR: 无法在清洗后的结构中找到目标绝对行号 {t_res}\n")
                continue 
            
            f_tasks.write(f"# [编号映射] PDB {pdb_num} -> Rosetta绝对行号 {t_res} ({wt_aa})（审计）\n")
            mut_aas = [aa for aa in all_aas if aa != wt_aa]
            for mut_aa in mut_aas:
                mutfile_name = f"mut_{wt_aa}_{pdb_num}_{mut_aa}.txt" 

                with open(os.path.join(mut_dir, mutfile_name), 'w') as f_mut:
                    f_mut.write("total 1\n1\n")
                    f_mut.write(f"{wt_aa} {t_res} {mut_aa}\n")
                
                cmd = (
                    f"{rosetta_ddg_bin} "
                    f"-s {abs_wt_relaxed} -ddg:mut_file {mutfile_name} -extra_res_fa {params_file} "
                    f"-ddg:iterations 3 -ddg:cartesian -ddg:dump_pdbs false -ddg:bbnbrs 1 "
                    f"-score:weights ref2015_cart -relax:min_type lbfgs_armijo_nonmonotone "
                    f"-relax:cartesian -mute all -out:prefix ddg_{wt_aa}_{pdb_num}_{mut_aa}_"
                )
                f_tasks.write(f"{cmd} > mut_{wt_aa}_{pdb_num}_{mut_aa}.log 2>&1\n")

    abs_sum_file = os.path.abspath(os.path.join(sum_dir, "ddg_results.txt"))
    cleaner_py = f"""import os
import glob
results = []
for f in glob.glob("*.ddg"):
    try:
        parts = f.replace('.ddg', '').split('_')
        # 因为加入了链 ID，被下划线切割后的块数变多了，从 4 改为 5
        if len(parts) < 5: continue 
        
        chain_id = parts[2]
        pdb_num = int(parts[3])
        # 拼接成类似 D_A73_A 的漂亮格式
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
    out.write("突变\\t平均ΔΔG(REU)\\n")
    for chain_id, pdb_num, mut_name, ddg in results:
        out.write(f"{{mut_name}}\\t{{ddg:.4f}}\\n")
"""
    with open(os.path.join(mut_dir, "auto_clean.py"), "w") as f: f.write(cleaner_py)

    abs_relax_dir = os.path.abspath(relax_dir)
    runner_py = f"""import time
import subprocess
import os
import glob
import shutil
from concurrent.futures import ThreadPoolExecutor

relax_dir = "{abs_relax_dir}"
score_path = os.path.join(relax_dir, "score.sc")
wt_pdb_path = os.path.join(relax_dir, "wt_relaxed.pdb")

print(" 正在后台默默等待 tool3 弛豫彻底完成... (最高等待 4 小时)")
max_wait = 14400
waited = 0
while not os.path.exists(score_path) and not glob.glob(os.path.join(relax_dir, "cart_relax_*.pdb")) and waited < max_wait:
    time.sleep(10)
    waited += 10

if os.path.exists(score_path):
    with open(score_path, 'r') as f: lines = f.readlines()
    data_lines = [l.split() for l in lines if l.startswith("SCORE:") and "total_score" not in l]
    if data_lines:
        data_lines.sort(key=lambda x: float(x[1]))
        best_pdb = os.path.join(relax_dir, f"{{data_lines[0][-1]}}.pdb")
        if os.path.exists(best_pdb): shutil.copy(best_pdb, wt_pdb_path)
            
if not os.path.exists(wt_pdb_path):
    pdbs = glob.glob(os.path.join(relax_dir, "cart_relax_*.pdb"))
    if pdbs: shutil.copy(pdbs[0], wt_pdb_path)
    else: 
        print(" 严重错误：弛豫未成功生成结构，突变任务终止！")
        exit(1)

with open("rosetta_tasks.txt", "r") as f:
    tasks = [line.strip() for line in f if line.strip() and not line.strip().startswith('#')]

print(f" 开始 Python 内置错峰并发计算 (并发:{jobs}, 延迟:{delay}s)...")
def execute(cmd): subprocess.run("unset DISPLAY && " + cmd, shell=True)

with ThreadPoolExecutor(max_workers={jobs}) as pool:
    for task in tasks:
        pool.submit(execute, task)
        time.sleep({delay})

subprocess.run("python auto_clean.py", shell=True)
"""
    with open(os.path.join(mut_dir, "run_ddg.py"), "w") as f: f.write(runner_py)
    
    subprocess.Popen(f"cd {mut_dir} && nohup python -u run_ddg.py > run_launcher.log 2>&1 &", shell=True)
    return f"【流水线通关】所有 {len(targets)} 个位点的突变任务已挂载！"

# ==========================================
# 3. 诊断与修复工具
# ==========================================
@tool
def diagnose_and_fix(tool_name: str) -> str:
    """智能诊断工具：分析工具执行结果，自动检测并修复已知问题。"""
    if tool_name == "run_saturation_mutagenesis":
        mut_dir = get_workspace_path("tool4_mut_results")
        sum_dir = get_workspace_path("tool5_summary")
        
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
    """自动修复工具：检测并修复 tool4_mut_results 下的 auto_clean.py 解析问题。"""
    mut_dir = get_workspace_path("tool4_mut_results")
    cleaner_path = os.path.join(mut_dir, "auto_clean.py")
    if not os.path.exists(cleaner_path):
        return "【修复失败】未找到 auto_clean.py"
    
    with open(cleaner_path, 'r') as f:
        content = f.read()
    
    if "'mut' in lp[0]" in content or "lp[-1]" in content:
        fixed_content = content.replace(
            "if len(lp) >= 3 and ('mut' in lp[0] or 'rep' in lp[0]):",
            "if len(lp) >= 3 and ('MUT' in ' '.join(lp) or 'COMPLEX' in lp[0]):"
        ).replace(
            "scores.append(float(lp[-1]))",
            "scores.append(float(lp[5]))"
        )
        with open(cleaner_path, 'w') as f:
            f.write(fixed_content)
        return "【自动修复成功】已修复 auto_clean.py 的解析逻辑。请重新运行 python auto_clean.py。"
    
    return "【无需修复】auto_clean.py 解析逻辑已正确。"

# ==========================================
# 4. RAG 文档检索工具
# ==========================================
DOCS_DIR = "rosetta_manuals" 

def init_vector_db():
    """初始化并构建向量数据库"""
    # 关键修改：让 Embeddings 也使用 .env 里配置好的 API Key 和代理 URL
    embeddings = OpenAIEmbeddings(
        api_key=os.getenv("LLM_API_KEY", ""),
        base_url=os.getenv("LLM_BASE_URL", "")
        # 如果你的中转 API 报错找不到默认模型，可以取消下面这行的注释并指定模型名称
        # model="text-embedding-3-small" 
    )
    
    if os.path.exists("chroma_db"):
        return Chroma(persist_directory="chroma_db", embedding_function=embeddings)
    
    # 确保文档存在再加载
    guide_path = f"{DOCS_DIR}/rosetta_cartesian_relax_guide.txt"
    if os.path.exists(guide_path):
        loader = TextLoader(guide_path, encoding="utf-8")
        docs = loader.load()
        text_splitter = RecursiveCharacterTextSplitter(chunk_size=500, chunk_overlap=50)
        splits = text_splitter.split_documents(docs)
        return Chroma.from_documents(documents=splits, embedding=embeddings, persist_directory="chroma_db")
    return None

vector_db = init_vector_db()

@tool
def search_rosetta_docs(query: str) -> str:
    """RAG 检索工具：当遇到未知的 Rosetta 参数、报错代码，输入明确疑问句调用此工具。"""
    if not vector_db:
         return "【系统提示】向量数据库未初始化或文档目录不存在。"
    try:
        docs = vector_db.similarity_search(query, k=3)
        if not docs:
            return "【检索结果】知识库中未找到相关内容。"
        context = "\n---\n".join([doc.page_content for doc in docs])
        return f"【检索成功】以下是来自 Rosetta 知识库的参考资料：\n{context}"
    except Exception as e:
        return f"【检索异常】{str(e)}"