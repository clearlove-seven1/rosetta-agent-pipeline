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
    relax_pdb = os.path.join(current_workspace.get(), "cartesian_relax", "complex_rosetta_ready.pdb")
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
    
    out_dir = get_workspace_path("parameterize_ligand")

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
    """自动修复工具：检测并修复 saturation_mutagenesis 下的 auto_clean.py 解析问题。"""
    mut_dir = get_workspace_path("saturation_mutagenesis")
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
    """【实验对照】拿 SKEMPI 2.0 数据库里同 PDB 同位点的实验 ΔΔG，跟我们的预测算 Pearson 相关性，输出可信度报告。

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
        f"【实验对照】{pdb_id_upper} 匹配到 {n} 个重叠位点",
        f"Pearson r = {r:.3f}  (n={n})",
        "",
        "位点详情 (wt-pos-mut: 预测 vs 实验):",
    ]
    for k in common:
        lines.append(f"  {k[0]}-{k[1]}-{k[2]}: {predicted[k]:+.2f}  vs  {experimental_avg[k]:+.2f}  REU")
    lines.append("")
    if r > 0.7:
        lines.append("✅ 相关性强：本批次 Rosetta 预测可信度高。")
    elif r > 0.4:
        lines.append("⚠️ 相关性中等：部分位点可信，建议人工复检偏差大的位点。")
    elif r > 0:
        lines.append("❌ 相关性弱：本批次结果参考价值低，建议检查输入结构/参数。")
    else:
        lines.append("❌ 负相关或零相关：预测与实验趋势不一致，需排查管线是否出错。")

    return "\n".join(lines)


# ==========================================
# 3.7. ESM-2 disorder 检测：蛋白质语言模型推理
# ==========================================
DISORDER_THRESHOLD = 0.5  # disorder score > 0.5 视为无序残基


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


def _predict_disorder_esm2(sequence, residues):
    """用 ESM-2 蛋白质语言模型推理 disorder 概率

    策略：拿 ESM-2 的 per-position masked language modeling log-prob，
    高 log-prob（模型自信）→ 有序；低 log-prob → 无序。

    返回 ({chain: {resnum: disorder_score}}, method_used)
    """
    import esm
    import torch

    # 用 35M 模型（快且准），没有 GPU 也跑得动
    model, alphabet = esm.pretrained.esm2_t12_35M_UR50D()
    model.eval()
    batch_converter = alphabet.get_batch_converter()

    _, _, batch_tokens = batch_converter([("protein", sequence)])
    if torch.cuda.is_available():
        model = model.cuda()
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
    """【disorder 检测】预测每个残基的 disorder 概率

    策略：
      1. 优先用 ESM-2 蛋白质语言模型（35M 参数，本地推理）
      2. fair-esm 未装或模型加载失败时，自动回退到 PDB 信号（B-factor + 残基 gap）

    输出报告包含：
      - 总残基数、有序/无序数量
      - 按位点列出的 disorder 分数（top 10 最无序）
      - 给用户的提示：哪些位点不要相信预测

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
        f"【disorder 检测】{os.path.basename(pdb_path)}",
        f"方法: {method}",
        f"总残基: {total} | 有序 (<0.5): {n_ordered} | 无序 (≥0.5): {n_disordered}  ({100*n_disordered/total:.1f}%)",
        "",
    ]

    if top_disordered:
        lines.append("Top 10 最无序残基 (disorder ≥ 0.5 的位点):")
        for chain, resnum, score in top_disordered:
            flag = "  ⚠️" if score >= DISORDER_THRESHOLD else ""
            lines.append(f"  {chain}_{resnum}: disorder={score:.3f}{flag}")

    lines.append("")
    if n_disordered > 0:
        lines.append(
            f"提示: 无序区域 (disorder ≥ {DISORDER_THRESHOLD}) 的 Rosetta 预测可信度低，"
            "建议在分析结果时优先看有序区域的位点。"
        )
    else:
        lines.append("✅ 所有残基 disorder < 0.5，结构稳定")

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
    """【蛋白-小分子对照】拿 PDBbind v2020 里同 PDB 的实测 Kd，跟 Rosetta score.sc 的 total_score 比 Spearman 相关性。

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
        f"【蛋白-小分子对照】共 {len(pdb_data)} 个 PDB",
        "",
        f"  {'PDB':<14}{'Affinity(M)':<18}{'-log10 Aff':<14}{'Rosetta(REU)':<14}{'nstruct'}",
    ]
    for d in pdb_data:
        lines.append(
            f"  {d['pdb_id']:<14}{d['aff_type']}={d['affinity']:.2e}  "
            f"{d['log_aff']:>10.2f}    {d['rosetta_score']:>+12.2f}    {d['nstruct']}"
        )

    if len(pdb_data) < 3:
        lines.extend([
            "",
            f"⚠️ 只 {len(pdb_data)} 个 PDB，Spearman 需要 ≥3 个数据点。",
            "  多配体评估：把 HIV protease / thrombin / carbonic anhydrase 的多个 PDB ID 都跑 run_cartesian_relax，",
            "  再用 'pdb_ids=1JTG,1HVH,1HVR,...;score_paths=/path1/sc;/path2/sc;...' 调用本工具。",
        ])
        return "\n".join(lines)

    # Spearman 计算（Rosetta 越低 ≈ Kd 越小 → 期望负相关）
    rosetta_vals = [d['rosetta_score'] for d in pdb_data]
    aff_vals = [d['log_aff'] for d in pdb_data]
    rho, n = _spearman(rosetta_vals, aff_vals)

    lines.extend([
        "",
        f"Spearman ρ = {rho:.3f}  (n={n}, 期望负相关：Rosetta 越低 ≈ Kd 越小)",
        "",
    ])
    if rho < -0.6:
        lines.append("✅ 强负相关：Rosetta 打分能很好预测 Kd 排序。")
    elif rho < -0.3:
        lines.append("⚠️ 中等负相关：部分一致，可作初筛。")
    elif rho < 0.3:
        lines.append("❌ 弱/无相关：Rosetta 打分对该体系不可靠，建议用 score-function=beta_nov16 等专用打分。")
    else:
        lines.append("❓ 正相关异常：检查 PDB 匹配逻辑或亲和力单位是否一致。")

    return "\n".join(lines)


# ==========================================
# 4. RAG 文档检索工具
# ==========================================
DOCS_DIR = "rosetta_manuals" 

def init_vector_db():
    """初始化并构建向量数据库"""
    # 让 Embeddings 也使用 .env 里配置好的 API Key 和代理 URL
    embeddings = OpenAIEmbeddings(
        api_key=os.getenv("LLM_API_KEY", ""),
        base_url=os.getenv("LLM_BASE_URL", "")
        # 如果你的中转 API 报错找不到默认模型，可以取消下面这行的注释并指定模型名称
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