"""task_runtime.py — 任务运行时公共层（2026-11-20 P0/P1 修复）

所有入口（FastAPI 平台 / Gradio+CLI / MCP）共用的一份实现，收敛四类此前在
各文件里重复且行为不一致的逻辑：

1. 任务 ID 生成与目录创建：同标签同时间不再互相覆盖（时间戳+随机后缀+独占建目录），
   用户输入的标签经白名单清洗，不能用于注入路径。
2. 任务 ID 校验与路径安全：/files /download /set_workspace 等把 task_id 拼进路径的
   地方统一走 validate_task_dir（禁止分隔符/..，realpath 后必须仍在任务根内 →
   符号链接与穿越都被拦截）。
3. PDB 编号映射与突变位点解析（P0 核心）：
   - 残基身份 = (链 ID, PDB 残基号, 插入码, 野生型氨基酸) 四元组；
   - 插入码（100A）、备用构象（altLoc）、多模型、非标准残基、缺主链原子残基均显式处理并记录异常；
   - 无链输入遇到多链同号 → 明确报错列出候选链，不再静默取第一条链；
   - 彻底移除"提取数字降级匹配"——错误链名/格式错误一律报错，不猜。
   ⚠️ 诚实边界：本模块按"清洗规则 + 顺序计数"静态推断 Rosetta pose 绝对编号，
   与真实 Rosetta 加载结果在极端情况下（零占用度原子、被 ignore 的残基等）可能不一致；
   工具输出中已注明需以任务目录内的编号映射审计行为准，必要时用 get_rosetta_numbering
   + 实际 ddg 日志核验。见 CHANGES-rosetta.md。
4. 任务持久状态（P1）：task_status.json 写在任务目录内（原子写），区分
   created/running/completed/failed/interrupted，服务重启后仍可查询；
   status=running 时结合 runner 进程存活 + 汇总产物判断，判不了就标注"待核实"，
   不虚报成功/失败。该文件同时承担未来 GROMACS 整合所需的轻量 manifest 角色
   （输入结构/位点/参数/产物路径/软件信息，取不到的字段留空并注明原因）。

设计约束：本模块不 import rosetta_tools / rosetta_agent（保持单向依赖，
rosetta_tools → task_runtime），不调 LLM、不跑 Rosetta，可独立测试。
"""
import glob
import json
import os
import re
import secrets
import tempfile
import datetime

try:
    import fcntl as _fcntl
except ImportError:  # 非 Linux（开发机）退化为无锁；生产服务器为 Linux
    _fcntl = None

# 项目根 = 本文件所在目录（rosetta_agent.py / rosetta_server.py 同级）
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
OUTPUT_BASE = os.path.join(PROJECT_ROOT, "output")

# ==========================================
# 1. 任务 ID 与任务目录
# ==========================================
# 兼容性说明：历史格式 task_<YYYYMMDD>_<HHMM/HHMMSS>_<label> 仍被完整支持
# （validate / list 均可识别）。新格式把"秒级时间戳+4位随机hex"放进同一个
# 时间 token（task_<date>_<HHMMSS><hex>_<label>），使 skempi_eval 的
# task_id.split("_")[3:] 蛋白 ID 解析不受影响（label 段仍是第 4 段起）。
_TASK_LABEL_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def sanitize_label(text: str, maxlen: int = 40) -> str:
    """把用户可控的标签清洗成安全的目录名片段。

    只保留字母/数字/._-，其余（空格、中文、引号、斜杠…）一律折叠为 "_"；
    去掉首尾的 ._ 与空白；超长截断。返回值可能为空串，由调用方兜底。
    """
    text = str(text or "")
    cleaned = _TASK_LABEL_SAFE.sub("_", text).strip("._ ")
    return cleaned[:maxlen]


def new_task_id(label: str) -> str:
    """生成唯一倾向的任务 ID（不含并发冲突的最终裁决，由 create_task_dir 兜底）。

    格式：task_<YYYYMMDD>_<HHMMSS><4hex>_<label>
    - 同一秒内同标签两次提交 → 4 位随机 hex（65536 空间）+ 建目录独占裁决，几乎不可能碰撞；
    - hex 混在时间 token 内部，不改变 task_id 的分段结构。
    """
    label = sanitize_label(label) or "protein"
    now = datetime.datetime.now()
    time_token = now.strftime("%H%M%S") + secrets.token_hex(2)
    return f"task_{now.strftime('%Y%m%d')}_{time_token}_{label}"


def create_task_dir(label: str, base: str = OUTPUT_BASE):
    """创建任务目录并返回 (task_id, task_dir)。并发/重名安全：

    os.makedirs(exist_ok=False) 独占创建；冲突（同标签同秒+同随机 hex）则换一个
    task_id 重试，最多 5 次。绝不覆盖已存在的任务目录。
    """
    os.makedirs(base, exist_ok=True)
    last_err = None
    for _ in range(5):
        task_id = new_task_id(label)
        task_dir = os.path.join(base, task_id)
        try:
            os.makedirs(task_dir, exist_ok=False)
            return task_id, task_dir
        except FileExistsError as e:  # 极小概率撞名：换号重试，永不覆盖
            last_err = e
    raise RuntimeError(f"任务目录创建连续冲突 5 次（base={base}）：{last_err}")


def validate_task_dir(task_id: str, base: str = OUTPUT_BASE, must_exist: bool = True) -> str:
    """校验 task_id 并返回任务目录绝对路径；不合法/不存在抛 ValueError。

    规则（按序）：
      1. 非空、不含路径分隔符（任务 ID 是单层目录名，天然封死 ../ 类穿越）；
      2. realpath 后必须仍在任务根 base 内且不等于 base → 符号链接指到根外即拒绝；
      3. 目录名必须以 task_ 开头（与历史 _safe_task_dir 规则一致）。
    """
    task_id = (task_id or "").strip()
    if not task_id or "/" in task_id or "\\" in task_id:
        raise ValueError("task_id 非法：不能为空且不能包含路径分隔符")
    base_r = os.path.realpath(base)
    target = os.path.realpath(os.path.join(base, task_id))
    if target != base_r and not target.startswith(base_r + os.sep):
        raise ValueError("task_id 非法：解析后路径越出任务根目录（疑似穿越/符号链接）")
    if not os.path.basename(target).startswith("task_"):
        raise ValueError("task_id 非法：必须以 task_ 开头")
    if must_exist and not os.path.isdir(target):
        raise ValueError("任务工作区不存在")
    return target


# ==========================================
# 2. 任务持久状态（task_status.json，位于任务目录内）
# ==========================================
STATUS_CREATED = "created"
STATUS_RUNNING = "running"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
STATUS_INTERRUPTED = "interrupted"
# 部分成功：预期计算部分完成（如 15/19 个突变成功，其余失败）。
# 仅在 manifest.mutations_planned 已知时判定；未知（旧任务/旧测试夹具）保持旧语义。
STATUS_PARTIAL = "partial"

STATE_FILENAME = "task_status.json"


def _state_path(task_dir: str) -> str:
    return os.path.join(task_dir, STATE_FILENAME)


def _now_iso() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def state_read(task_dir: str) -> dict:
    """读取任务状态文件；缺失/损坏返回 {}（不抛异常——损坏状态不应炸掉列表页）。"""
    path = _state_path(task_dir)
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def state_write(task_dir: str, **updates) -> dict:
    """合并写任务状态：flock 跨进程互斥（读-改-写临界区）+ 原子替换 tmp→正式文件。

    2026-11-21 复核修复：旧实现"无锁读-改-写"在服务线程与 runner 子进程并发写
    同一任务时会丢更新（复核测试实证）。flock 由内核在进程退出（含崩溃）时释放，
    无残留锁风险。
    """
    os.makedirs(task_dir, exist_ok=True)
    path = _state_path(task_dir)
    lock_path = os.path.join(task_dir, ".task_status.lock")
    with open(lock_path, "w") as lf:
        if _fcntl is not None:
            _fcntl.flock(lf, _fcntl.LOCK_EX)
        state = state_read(task_dir)
        state.setdefault("task_id", os.path.basename(task_dir))
        state.setdefault("created_at", _now_iso())
        state.update(updates)
        state["updated_at"] = _now_iso()
        fd, tmp = tempfile.mkstemp(dir=task_dir, prefix=".task_status_", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False, indent=1)
            os.replace(tmp, path)
        except BaseException:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise
    return state


def _pid_alive(pid, marker: str = None) -> bool:
    """探测进程是否存活，且（给定 marker 时）仍是我们的 runner 进程。

    Linux：读 /proc/<pid>/cmdline——读不到或为空（僵尸）→ 已退出；cmdline 不含
    marker（如 run_ddg.py）→ PID 已被无关进程复用，按"已退出"处理（不凭 PID
    存在就断言原任务仍在运行）。
    非 Linux：退化 os.kill 探测（无 marker 校验能力）。
    """
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return True  # 无效记录 → 当作未知，宁可"待核实"不可误判
    if pid <= 0:
        return True
    if os.path.isdir("/proc"):
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                cmdline = f.read().decode(errors="ignore")
        except OSError:
            return False  # /proc 存在但读不到 → 进程已退出
        if not cmdline:
            return False  # 僵尸进程（cmdline 为空）
        if marker and marker not in cmdline:
            return False  # PID 复用：已不是我们的 runner
        return True
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True


def _ddg_count(task_dir: str) -> int:
    """任务目录内的 .ddg 产物数（0 个成功突变不得记为 completed）。"""
    return len(glob.glob(os.path.join(task_dir, "saturation_mutagenesis", "*.ddg")))


def state_effective(task_dir: str, base: str = OUTPUT_BASE) -> dict:
    """读状态并给出"生效状态"：running 状态结合 runner 进程存活（PID 复用防护）
    与产物证据解释。

    返回 {"status", "note", "state"}：
      - status=running 且 relax/ddg runner 进程仍存活（cmdline 含对应 runner 名）
        → running；
      - status=running 且记录的 runner 全部退出：
          · summary 在位且有 ≥1 个 .ddg → completed；
          · summary 在位但 0 个 .ddg → failed（空壳汇总=无成功突变，不视为成功）；
          · 仅弛豫产物在位 → running 语义 + "疑似已中断（待核实）"标注；
          · 什么都没有 → interrupted（由证据推断，不自动重试）；
      - status=running 但没有进程记录 → running + 待核实标注；
      - 其余状态原样透传。
    注意：这只解释"计算是否还在跑"，不代表 LangGraph 对话检查点可恢复（内存检查点，
    重启即失）——续聊能力由接口层另行说明。
    """
    _RUNNER_MARKERS = {"relax": "run_relax_parallel.py", "ddg": "run_ddg.py"}
    state = state_read(task_dir)
    status = state.get("status") or STATUS_CREATED
    note = state.get("status_note", "")
    if status == STATUS_RUNNING:
        pids = {k: v for k, v in (state.get("runner_pids") or {}).items() if v}
        if not pids:
            return {"status": STATUS_RUNNING,
                    "note": (note + "；" if note else "") + "无后台进程记录，运行状态待核实",
                    "state": state}
        alive = [p for k, p in pids.items() if _pid_alive(p, _RUNNER_MARKERS.get(k))]
        if alive:
            return {"status": STATUS_RUNNING, "note": note, "state": state}
        summary = os.path.join(task_dir, "summary", "ddg_results.txt")
        relax_done = os.path.join(task_dir, "cartesian_relax", "score.sc")
        if os.path.exists(summary):
            n_ddg = _ddg_count(task_dir)
            if n_ddg == 0:
                return {"status": STATUS_FAILED,
                        "note": (note + "；" if note else "")
                                + "runner 已退出，汇总存在但 0 个成功突变（空壳结果，不视为成功）",
                        "state": state}
            planned = (state.get("manifest") or {}).get("mutations_planned")
            if planned and n_ddg < planned:
                return {"status": STATUS_PARTIAL,
                        "note": (note + "；" if note else "")
                                + f"runner 进程已退出，部分成功：{n_ddg}/{planned} 个突变结果在位（失败项查看 mut_*.log 末尾 [exit code]）",
                        "state": state}
            return {"status": STATUS_COMPLETED,
                    "note": (note + "；" if note else "") + f"runner 进程已退出，{n_ddg} 个突变结果在位",
                    "state": state}
        if os.path.exists(relax_done):
            return {"status": state.get("status"),  # 弛豫完成但突变未完成 → 保持 running 语义，注明
                    "note": (note + "；" if note else "")
                            + "弛豫产物在位但无突变汇总，后台计算疑似已中断（待核实）",
                    "state": state}
        return {"status": STATUS_INTERRUPTED,
                "note": (note + "；" if note else "")
                        + "runner 进程已退出且未见汇总产物，判定为中断（待核实，不自动重试）",
                "state": state}
    return {"status": status, "note": note, "state": state}


def list_task_states(base: str = OUTPUT_BASE) -> dict:
    """扫描任务根下所有 task_* 目录的生效状态（供 /api/tasks、list_tasks 汇总）。"""
    out = {}
    if not os.path.isdir(base):
        return out
    for name in sorted(os.listdir(base)):
        p = os.path.join(base, name)
        if os.path.isdir(p) and name.startswith("task_"):
            out[name] = state_effective(p, base=base)
    return out


# ==========================================
# 3. PDB 编号映射（P0 核心）
# ==========================================
# 残基身份 = (链 ID, PDB 残基号, 插入码, 野生型氨基酸)。
# 铁律：整条流水线只在这里做一次 PDB→Rosetta 映射；调用方严禁二次换算。
#
# 与 run_cartesian_relax 清洗规则保持一致（见 rosetta_tools.run_cartesian_relax）：
#   - 仅 ATOM 记录、标准 20 氨基酸（HETATM 非标准残基会被清洗剔除 → 这里同样剔除并计入异常）；
#   - altLoc 仅保留 ' '/'A'/'1'（其余 altLoc 会被清洗剔除）；
#   - 只取第一个 MODEL（清洗会把所有 ATOM 拼进同一文件，多模型输入必须截断）。
# 差异说明：本模块按"残基键 (chain,num,icode) 首次出现的任意原子行"计数，
# 残基须具备完整主链（N/CA/C）；缺主链残基会被 Rosetta 跳过（cartesian_ddg 实测证据），
# 同样从映射中剔除并计入 anomalies（2026-11-21 地面真值修正）。

AA_3TO1 = {
    "ALA": "A", "CYS": "C", "ASP": "D", "GLU": "E", "PHE": "F", "GLY": "G",
    "HIS": "H", "ILE": "I", "LYS": "K", "LEU": "L", "MET": "M", "ASN": "N",
    "PRO": "P", "GLN": "Q", "ARG": "R", "SER": "S", "THR": "T", "VAL": "V",
    "TRP": "W", "TYR": "Y",
}

_STANDARD_AAS = set(AA_3TO1)
_AMBIGUOUS = object()  # 无链纯编号在多链间重号时的哨兵


class TokenFormatError(ValueError):
    """突变位点 token 格式无法解析（含原 token 与原因）。"""


class NumberingMap:
    """PDB 晶体学编号 ↔ Rosetta 绝对行号 双向映射（清洗规则同源）。

    属性：
      rosetta_to_pdb : {int idx: "链_残基号[插入码]"}   —— 索引从 1 连续递增
      strict         : {"链_残基号[icode]": idx}
      plain          : {"残基号[icode]": idx}；多链重号时值为 _AMBIGUOUS（查 get 返回 None）
      wt             : {idx: 单字母氨基酸}（非标准残基不会进入映射）
      chains         : 出现顺序的链 ID 列表
      anomalies      : 解析时发现的异常说明列表（altLoc 剔除/多模型/非标准残基/缺主链原子…）
    """

    def __init__(self):
        self.rosetta_to_pdb = {}
        self.strict = {}
        self.plain = {}
        self.wt = {}
        self.chains = []
        self.anomalies = []

    def get(self, key):
        """宽松查询：纯编号缺失或多链重号时都返回 None（由 resolve_targets 区分报错）。"""
        v = self.plain.get(key, _AMBIGUOUS)
        return None if v is _AMBIGUOUS else v

    def ambiguities_for(self, numkey: str):
        """列出无链编号 numkey 撞到的候选链键（生成报错信息用）。"""
        cands = []
        for ch in self.chains:
            k = f"{ch}_{numkey}"
            if k in self.strict:
                cands.append(k)
        return cands

    @property
    def size(self) -> int:
        return len(self.rosetta_to_pdb)


def build_numbering(pdb_path: str) -> NumberingMap:
    """扫描 PDB（按清洗规则过滤）构建映射。解析失败抛 OSError/ValueError。

    2026-11-21 地面真值修正：缺主链原子（N/CA/C 任一缺失）的标准残基会被
    Rosetta 跳过、不进入 pose（cartesian_ddg 实测证据："skipping pdb residue
    b/c it's missing too many mainchain atoms: 40 A ASN ... missing: CA"，见
    verify_numbering_20261121/noca/ddg_log.txt）。此前"按任意原子行计入编号"
    的规则与 Rosetta 实际行为相反（会造成错位或 ddg 断言崩溃），已改为剔除并
    计入异常。N/C 缺失未单独实测，按同一保守规则处理并在异常中注明。
    """
    nmap = NumberingMap()
    res_order = []       # [( (chain,numkey), resname )] 首次出现顺序
    res_atoms = {}       # (chain,numkey) -> set(保留行的原子名)
    seen_chains = set()
    model_seen = 0
    model_done = False
    n_altloc_skipped = 0
    n_nonstd = 0

    with open(pdb_path, "r", errors="ignore") as f:
        for line in f:
            rec = line[:6]
            if rec.startswith("MODEL"):
                model_seen += 1
                if model_seen > 1:
                    nmap.anomalies.append(
                        f"检测到多模型 PDB（{model_seen}+ 个 MODEL），仅取第 1 个模型参与映射")
                    model_done = True
                continue
            if rec.startswith("ENDMDL"):
                model_done = True
                continue
            if model_done:
                continue
            if not rec.startswith("ATOM"):
                continue
            altloc = line[16]
            if altloc not in (" ", "A", "1"):
                n_altloc_skipped += 1
                continue
            resname = line[17:20].strip()
            if resname not in _STANDARD_AAS:
                n_nonstd += 1
                continue
            chain = line[21].strip()
            numkey = line[22:26].strip() + (line[26].strip() if len(line) > 26 else "")
            key = (chain, numkey)
            if key not in res_atoms:
                res_order.append((key, resname))
                res_atoms[key] = set()
                if chain not in seen_chains:
                    seen_chains.add(chain)
                    nmap.chains.append(chain)
            res_atoms[key].add(line[12:16].strip())

    # 出号：剔除缺主链原子的残基（与 Rosetta pose 行为对齐，见 docstring 证据）
    dropped_backbone = []
    for (chain, numkey), resname in res_order:
        missing = {"N", "CA", "C"} - res_atoms[(chain, numkey)]
        if missing:
            dropped_backbone.append((chain, numkey, missing))
            continue
        idx = len(nmap.rosetta_to_pdb) + 1
        strict_key = f"{chain}_{numkey}"
        nmap.rosetta_to_pdb[idx] = strict_key
        nmap.strict[strict_key] = idx
        nmap.wt[idx] = AA_3TO1[resname]
        # 无链纯编号：唯一 → 登记；多链重号 → 标记歧义哨兵
        if numkey in nmap.plain and nmap.plain[numkey] is not _AMBIGUOUS:
            nmap.plain[numkey] = _AMBIGUOUS
        elif numkey not in nmap.plain:
            nmap.plain[numkey] = idx

    n_res = len(nmap.rosetta_to_pdb)
    if n_altloc_skipped:
        nmap.anomalies.append(f"{n_altloc_skipped} 行备用构象(altLoc∉' A 1')已剔除（与弛豫清洗一致）")
    if n_nonstd:
        nmap.anomalies.append(f"{n_nonstd} 行非标准残基/HETATM 已剔除（与弛豫清洗一致，Rosetta 加载同样忽略）")
    if dropped_backbone:
        detail = ", ".join(f"{c}_{n}(缺{'/'.join(sorted(m))})" for c, n, m in dropped_backbone[:5])
        nmap.anomalies.append(
            f"{len(dropped_backbone)} 个标准残基缺主链原子(N/CA/C)已剔除"
            f"（{detail}{'…' if len(dropped_backbone) > 5 else ''}；"
            "与 Rosetta 实测一致：缺失残基会被 Rosetta 跳过、不进入 pose；"
            "N/C 缺失未单独实测，按同一保守规则处理）")
    if n_res == 0:
        raise ValueError("PDB 中未找到可映射的标准氨基酸残基（空文件/全为非标准残基/全缺主链原子？）")
    return nmap


# ------------------------------------------
# 3.1 位点 token 解析与目标解析（唯一实现，四个入口共用）
# ------------------------------------------
# 接受格式：
#   "76"     纯晶体学编号（唯一时可用；多链重号报错）
#   "100A"   编号+插入码（唯一时可用）
#   "A_76"   链_编号
#   "A_100A" 链_编号+插入码
# 明确拒绝："A76"（无分隔符，提示写 A_76）、带杂字符/空白 token、负数、超长编号。
_TOKEN_RE = re.compile(r"^(?:([A-Za-z]{1,2})[_])?(\d{1,4})([A-Za-z])?$")


def parse_token(token: str):
    """解析单个位点 token → (chain|None, numstr, icode|None)；不合法抛 TokenFormatError。"""
    tok = str(token).strip()
    if "'" in tok or '"' in tok:
        raise TokenFormatError(f"{tok}: 含引号")
    m = _TOKEN_RE.match(tok)
    if not m:
        hint = ""
        if re.match(r"^[A-Za-z]{1,2}\d+$", tok):
            hint = "（链与编号之间请用下划线分隔，如 A_76）"
        raise TokenFormatError(f"{tok}: 位点格式无法解析{hint}")
    chain = m.group(1) or None
    num, icode = m.group(2), m.group(3)
    return chain, num, icode


def resolve_targets(tokens, nmap: NumberingMap):
    """把位点 token 列表解析成 Rosetta 绝对行号；返回 (targets, errors)。

    targets: [{"token", "chain", "numkey", "idx", "wt"}]
    errors : [{"token", "reason"}]
    行为约定（对比旧实现的行为变化）：
      - 无链纯编号在多链重号 → 报错列出候选链（旧实现静默取第一条链）；
      - 错误链名/编号不存在 → 报错（旧实现会先做"提取数字降级匹配"，已移除）；
      - 插入码（100A）→ 按 (编号+插入码) 精确匹配（旧实现把 100A 当 100 处理）。
    """
    targets, errors = [], []
    for tok in tokens:
        try:
            chain, num, icode = parse_token(tok)
        except TokenFormatError as e:
            errors.append({"token": str(tok), "reason": str(e)})
            continue
        numkey = num + (icode or "")
        if chain:
            chain = chain.upper()
            if chain not in nmap.chains:
                errors.append({"token": str(tok),
                               "reason": f"链 {chain} 不存在于结构中（可用链: {'/'.join(nmap.chains) or '无'}）"})
                continue
            key = f"{chain}_{numkey}"
            idx = nmap.strict.get(key)
            if idx is None:
                # 同号但无此插入码时给出更贴近的提示
                near = [k for k in nmap.strict if k.startswith(f"{chain}_{num}")]
                reason = f"位点 {key} 不在结构中"
                if near:
                    reason += f"（相近位点: {', '.join(sorted(near)[:6])}——注意插入码需精确匹配）"
                errors.append({"token": str(tok), "reason": reason})
                continue
        else:
            idx = nmap.get(numkey)
            if idx is None:
                cands = nmap.ambiguities_for(numkey)
                if cands:
                    errors.append({"token": str(tok),
                                   "reason": f"编号 {numkey} 在多条链上出现（{', '.join(cands)}），"
                                             f"请带链 ID 重试，如 {cands[0]}"})
                else:
                    errors.append({"token": str(tok), "reason": f"编号 {numkey} 不存在于结构中"})
                continue
            chain = nmap.rosetta_to_pdb[idx].rsplit("_", 1)[0]
        wt = nmap.wt.get(idx)
        targets.append({"token": str(tok).strip(), "chain": chain,
                        "numkey": numkey, "idx": idx, "wt": wt})
    return targets, errors


def split_tokens(mutation_str: str):
    """把位点输入串拆成 token 列表：支持中英文逗号、分号与空白分隔；去掉引号/括号装饰。"""
    raw = str(mutation_str or "")
    raw = raw.replace("，", ",").replace("、", ",").replace("；", ";").replace(";", " ")
    raw = raw.strip().strip("[]()").strip("'\" ")
    toks = [t for t in re.split(r"[,\s]+", raw) if t]
    return toks


def precheck_targets(pdb_path: str, mutation: str):
    """提交前确定性预检（所有入口共用；与 run_saturation_mutagenesis 同一解析逻辑）。

    返回 (ok, note)：ok=False 时 note 含逐位点失败原因与结构链/残基信息；
    ok=True 时 note 含逐位点 WT 氨基酸（供 Agent 与用户核对）。
    注意：预检基于上传的原始 PDB 按清洗规则推断——弛豫清洗后的结构非标准残基/多余
    altLoc 会被剔除，本函数已按同一规则过滤，保证预检通过与执行时映射一致。
    """
    try:
        nmap = build_numbering(pdb_path)
    except (OSError, ValueError) as e:
        return False, f"预检失败：无法解析 PDB（{e}）"
    tokens = split_tokens(mutation)
    if not tokens:
        return False, "预检失败：未解析到任何突变位点"
    targets, errors = resolve_targets(tokens, nmap)
    anom = ("；".join(nmap.anomalies)) if nmap.anomalies else ""
    if errors:
        detail = "; ".join(f"{e['token']}({e['reason']})" for e in errors)
        note = (f"突变位点预检失败: {detail}。"
                f"结构含 {nmap.size} 个标准残基，链: {'/'.join(nmap.chains) or '无链号'}。")
        if anom:
            note += f" 解析异常: {anom}。"
        note += "请核对 PDB 晶体学编号（可用 get_rosetta_numbering 工具核对）。"
        return False, note
    confirmed = " ".join(f"{t['chain']}_{t['numkey']}({t['wt']})" for t in targets)
    note = f"位点预检通过: {confirmed}（PDB晶体学编号）"
    if anom:
        note += f"【结构解析异常: {anom}】"
    return True, note


# ==========================================
# 3.2 流结束后的状态收口（FastAPI / Gradio / CLI 共用）
# ==========================================
def settle_task_after_stream(task_dir: str, had_error: bool = False,
                             error_type: str = "", error_detail: str = "") -> str:
    """一轮 Agent 流结束（SSE/Gradio 流结束）时调用：判定并落盘当前状态。

    判定规则（诚实区分，不虚报）：
      - summary/ddg_results.txt 已在位 → completed（突变计算已全部完成）；
      - 流中出现过错误（had_error）且汇总缺失 → failed（记录错误类型与原因）；
      - relax/ddg runner 进程仍在运行 → running（注明：后台计算仍在进行，
        最终状态由 runner 完成时更新）；
      - 其余（无计算、无错误，如纯查询对话）→ completed。
    注意：这只描述计算状态；LangGraph 对话检查点是内存态，服务重启后不可续，
    本函数不做也不承诺自动续跑。
    返回写入的状态字符串。
    """
    state = state_read(task_dir)
    if not state:
        state = {"task_id": os.path.basename(task_dir)}
    summary = os.path.join(task_dir, "summary", "ddg_results.txt")
    n_ddg = _ddg_count(task_dir)
    if os.path.exists(summary) and n_ddg > 0:
        planned = (state.get("manifest") or {}).get("mutations_planned")
        if planned and n_ddg < planned:
            state_write(task_dir, status=STATUS_PARTIAL, stage=state.get("stage") or "mutagenesis",
                        status_note=f"Agent 流结束：部分成功（{n_ddg}/{planned} 个突变结果；失败项查看 mut_*.log）",
                        artifacts={"ddg_results": "summary/ddg_results.txt"})
            return STATUS_PARTIAL
        state_write(task_dir, status=STATUS_COMPLETED, stage=state.get("stage") or "mutagenesis",
                    status_note=f"Agent 流结束：汇总产物在位（{n_ddg} 个突变结果）",
                    artifacts={"ddg_results": "summary/ddg_results.txt"})
        return STATUS_COMPLETED
    if had_error:
        state_write(task_dir, status=STATUS_FAILED,
                    status_note="Agent 流以错误收场",
                    error_type=error_type or "ToolError",
                    error_detail=error_detail or "流水线中出现【执行报错】且未生成有效 ddg_results.txt")
        return STATUS_FAILED
    if os.path.exists(summary) and n_ddg == 0:
        state_write(task_dir, status=STATUS_FAILED,
                    status_note="汇总存在但 0 个成功突变（空壳结果，不视为成功）",
                    error_type="NoSuccessfulMutants",
                    error_detail="ddg_results.txt 仅含表头；查看 saturation_mutagenesis/mut_*.log 的 [exit code]")
        return STATUS_FAILED
    pids = [v for v in (state.get("runner_pids") or {}).values() if v]
    if any(_pid_alive(p) for p in pids):
        state_write(task_dir, status=STATUS_RUNNING,
                    status_note="Agent 流已结束，后台 Rosetta 计算仍在运行（完成后由 runner 更新最终状态）")
        return STATUS_RUNNING
    state_write(task_dir, status=STATUS_COMPLETED,
                status_note="Agent 流正常结束（本任务无后台计算或计算已由其他方式确认）")
    return STATUS_COMPLETED


# ==========================================
# 4. 轻量 manifest 辅助（写进 task_status.json，为 GROMACS 整合留接口）
# ==========================================
def set_manifest(task_dir: str, **fields) -> None:
    """把能准确获得的整合信息并入任务状态文件的 manifest 节点。

    取不到的信息由调用方留 None/空串并注明原因，绝不虚构。
    """
    state = state_read(task_dir)
    manifest = state.get("manifest") or {}
    manifest.update(fields)
    state_write(task_dir, manifest=manifest)


# ==========================================
# 5. 下游对接契约 pdw-task-v0.1（阶段 3）
# ==========================================
# 契约文本见 TASK-CONTRACT-v0.1.md；这里是可执行校验器：
# 下游执行器消费任务前调用 contract_check，ok=False 必须拒绝并出示 missing，
# 不得猜测补齐（总规划 §3.5"缺少必要信息时明确报错"）。
CONTRACT_VERSION = "pdw-task-v0.1"
_CONTRACT_REQUIRED = [
    "input_structure_path", "input_structure_sha256", "chains",
    "numbering_basis", "target_sites", "mutations_planned",
    "ddg_program", "ddg_params",
]


def contract_check(task_dir: str) -> dict:
    """校验任务目录状态文件是否满足下游消费契约（只读，不修改任何文件）。

    返回 {"ok", "legacy", "missing", "warnings", "manifest", "effective_status"}：
      - ok=False：缺必填字段或未到可消费终态（completed|partial）→ 下游必须拒绝；
      - legacy=True：无 contract_version 的旧任务（v0.1 之前落盘），字段不全属预期；
      - warnings：不改判 ok 的诚实提示（哈希不一致/版本未知/gromacs_ready=False 等）。
    """
    import hashlib as _hashlib
    state = state_read(task_dir)
    if not state:
        return {"ok": False, "legacy": False, "missing": ["task_status.json 不存在或不可解析"],
                "warnings": [], "manifest": {}, "effective_status": None}
    m = state.get("manifest") or {}
    legacy = not m.get("contract_version")
    missing = [k for k in _CONTRACT_REQUIRED if not m.get(k)]
    warnings = []
    if m.get("contract_version") and m["contract_version"] != CONTRACT_VERSION:
        warnings.append(f"契约版本 {m['contract_version']} 与本实现 {CONTRACT_VERSION} 不同，按同名规则校验")
    sha = m.get("input_structure_sha256")
    rel = m.get("input_structure_path") or "protein.pdb"
    fp = os.path.join(task_dir, rel)
    if sha and os.path.exists(fp):
        try:
            with open(fp, "rb") as f:
                h = _hashlib.sha256(f.read()).hexdigest()
            if h != sha:
                warnings.append(f"input_structure_sha256 与当前 {rel} 不一致（文件可能被改动）")
        except OSError:
            warnings.append(f"{rel} 无法读取，无法核对哈希")
    elif sha and not os.path.exists(fp):
        warnings.append(f"input_structure_path={rel} 在任务目录中不存在")
    if not m.get("rosetta_version"):
        warnings.append("rosetta_version 未知（旧任务或运行时捕获失败）")
    if m.get("gromacs_ready") is not True:
        warnings.append("gromacs_ready=False：结构未经 MD 准备，下游不得直接作为 GROMACS 输入")
    eff = state_effective(task_dir)
    eff_status = eff.get("status")
    if eff_status not in (STATUS_COMPLETED, STATUS_PARTIAL):
        missing.append(f"effective_status={eff_status}（未到可消费终态）")
    ok = not missing
    return {"ok": ok, "legacy": legacy, "missing": missing, "warnings": warnings,
            "manifest": m, "effective_status": eff_status}
