"""
eval_p0_fixes_20261120.py — P0/P1 修复（残基编号 / 任务隔离 / 持久状态）验证
================================================================================

不调 LLM、不跑 Rosetta 计算；所有产物写在系统临时目录。
沿袭 eval_v01_guardrails.py 的"纯逻辑 + 源码级断言"风格。

验证点：
  A. 残基编号映射（task_runtime.build_numbering / resolve_targets / precheck_targets）
     1) 单链连续编号 / 缺失残基(非连续) / 插入码 100 与 100A
     2) 多链同号：无链输入明确报错（不再静默取第一链）；带链输入正确
     3) 不存在位点 / 错误链名 / "B76"类歧义格式 / 引号装饰 → 一律报错（退化匹配已移除）
     4) 备用构象(altLoc)只计一次 / 非标准残基(MSE)剔除 / 缺主链残基按 Rosetta 行为剔除 / 多模型只取第 1 个
     5) 预检与执行同一实现：MSE 位点预检即拒绝（不再弛豫后才报错）
  B. 任务 ID 与路径安全
     6) 同标签连续创建 → 不同 task_id、不覆盖；ID 分段与 skempi_eval 兼容
     7) validate_task_dir：穿越/分隔符/前缀/符号链接外指 全部拒绝；旧格式 ID 兼容
     8) 含空格路径：JSON 任务行的 argv 保真（无 shell 参与）
  C. 持久任务状态
     9) 生命周期 created→running→completed/failed；重启模拟（跨进程落盘读取）
     10) 损坏/缺失状态文件不炸；多任务状态隔离；重复提交不覆盖
     11) running + 进程退出 + 无汇总 → interrupted（待核实），有汇总 → completed
  D. 突变/弛豫执行文件生成（不运行 Rosetta）
     12) rosetta_tasks.txt 为 JSON 行；argv 参数列表无 shell 元字符；mutfile 用绝对行号
     13) 生成的 runner（run_ddg.py / run_relax_parallel.py）py_compile 通过（f-string 陷阱）
     14) 状态与 manifest 落盘（runner_pids / ddg_params / gromacs_ready=false）
  E. 入口一致性（源码级）
     15) server 预检 / MCP set_workspace / 各入口任务创建 均委托公共层；旧退化正则已移除

运行（rosetta-agent conda env，仓库根目录）：
  python eval_p0_fixes_20261120.py
"""
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent
os.chdir(PROJECT_ROOT)
os.environ.pop("WB_EXPLORATORY_EVAL_ENABLED", None)
os.environ.pop("WB_DISORDER_FILTER_ENABLED", None)

sys.path.insert(0, str(PROJECT_ROOT))
import task_runtime as tr  # noqa: E402

RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond), detail))
    print(f"{'✅ PASS' if cond else '❌ FAIL'}  {name}" + (f"  | {detail}" if detail and not cond else ""))


# ---------------------------------------------------------------
# PDB 行构造（严格按列）
# ---------------------------------------------------------------
def atom_line(serial, name, resname, chain, resnum, icode=" ", altloc=" ", occ=1.00):
    nm = name.rjust(4)
    elem = (name[0] if not name.startswith(" ") else name[1]).rjust(2)
    return (f"ATOM  {serial:5d} {nm}{altloc}{resname:>3s} {chain}{resnum:>4d}{icode}   "
            f"{1.000:8.3f}{2.000:8.3f}{3.000:8.3f}{occ:6.2f}{0.00:6.2f}          {elem}  \n")


def hetatm_line(serial, name, resname, chain, resnum, icode=" "):
    nm = name.rjust(4)
    elem = name[0].rjust(2)
    return (f"HETATM{serial:5d} {nm}   {resname:>3s} {chain}{resnum:>4d}{icode}   "
            f"{1.000:8.3f}{2.000:8.3f}{3.000:8.3f}{1.00:6.2f}{0.00:6.2f}          {elem}  \n")


def write_pdb(path, lines):
    Path(path).write_text("".join(lines) + "END\n", encoding="utf-8")


def seq_lines(chain, nums, start_serial=1, resname="ALA", atoms=("N", "CA", "C")):
    out, s = [], start_serial
    for n in nums:
        for a in atoms:
            out.append(atom_line(s, a, resname, chain, n))
            s += 1
    return out, s


TMP = Path(tempfile.mkdtemp(prefix="wb_p0_"))

# ===============================================================
# A. 残基编号映射
# ===============================================================
# A1 单链连续 1..10
pdb1 = TMP / "cont.pdb"
lines, _ = seq_lines("A", range(1, 11))
write_pdb(pdb1, lines)
nm = tr.build_numbering(str(pdb1))
check("A1a 连续编号 idx==num", nm.strict["A_5"] == 5 and nm.plain["5"] == 5 and nm.size == 10)
check("A1b wt 身份正确", nm.wt[5] == "A")
check("A1c 无异常", nm.anomalies == [])

# A2 缺失残基（缺 6）
pdb2 = TMP / "gap.pdb"
lines, _ = seq_lines("A", [1, 2, 3, 4, 5, 7, 8, 9, 10])
write_pdb(pdb2, lines)
nm = tr.build_numbering(str(pdb2))
check("A2 缺失残基后位号前移", nm.strict["A_7"] == 6 and nm.strict["A_10"] == 9)

# A3 插入码 100 / 100A
pdb3 = TMP / "icode.pdb"
l1, s = seq_lines("A", [99, 100])
l2 = [atom_line(s, "N", "ALA", "A", 100, icode="A"), atom_line(s + 1, "CA", "ALA", "A", 100, icode="A"),
      atom_line(s + 2, "C", "ALA", "A", 100, icode="A")]
l3, _ = seq_lines("A", [101], start_serial=s + 3)
write_pdb(pdb3, l1 + l2 + l3)
nm = tr.build_numbering(str(pdb3))
check("A3a 插入码独立映射", nm.strict.get("A_100") == 2 and nm.strict.get("A_100A") == 3)
check("A3b 100A 不再被折叠成 100", nm.plain.get("100A") == 3)

# A4 多链同号
pdb4 = TMP / "multi.pdb"
la, s = seq_lines("A", [1, 2, 3])
lb, _ = seq_lines("B", [1, 2, 3], start_serial=s)
write_pdb(pdb4, la + lb)
nm = tr.build_numbering(str(pdb4))
targets, errors = tr.resolve_targets(["2"], nm)
check("A4a 多链同号无链输入报错", not targets and len(errors) == 1 and "A_2" in errors[0]["reason"] and "B_2" in errors[0]["reason"])
targets, errors = tr.resolve_targets(["A_2", "B_2"], nm)
check("A4b 带链输入各自命中", len(targets) == 2 and targets[0]["idx"] == 2 and targets[1]["idx"] == 5)

# A5 错误链/不存在/歧义格式/引号
for tok, key in [("Z_2", "错误链名"), ("999", "不存在"), ("A_999", "不存在(带链)"), ("B76", "歧义无分隔"), ("'2'", "引号装饰"), ("C_76x", "乱后缀")]:
    targets, errors = tr.resolve_targets([tok], nm)
    check(f"A5 {key}被拒绝({tok})", not targets and len(errors) == 1, str(errors))

# A6 altLoc 备用构象
pdb6 = TMP / "altloc.pdb"
l1, s = seq_lines("A", [1])
l2 = [atom_line(s, "N", "GLY", "A", 2, altloc="A"), atom_line(s + 1, "CA", "GLY", "A", 2, altloc="A"),
      atom_line(s + 2, "C", "GLY", "A", 2, altloc="A"),
      atom_line(s + 3, "CA", "GLY", "A", 2, altloc="B")]  # B 副本应被剔除
l3, _ = seq_lines("A", [3], start_serial=s + 4)
write_pdb(pdb6, l1 + l2 + l3)
nm = tr.build_numbering(str(pdb6))
check("A6 同残基 altLoc 只计一次", nm.size == 3 and nm.strict["A_2"] == 2)

# A7 非标准残基 MSE（HETATM 与 ATOM 两种写法都被剔除）
pdb7 = TMP / "mse.pdb"
l1, s = seq_lines("A", [1])
l2 = [atom_line(s, "CA", "MSE", "A", 2), hetatm_line(s + 1, "SE", "MSE", "A", 2)]
l3, _ = seq_lines("A", [3], start_serial=s + 2)
write_pdb(pdb7, l1 + l2 + l3)
nm = tr.build_numbering(str(pdb7))
check("A7 MSE 剔除且计数异常", nm.size == 2 and any("非标准残基" in a for a in nm.anomalies))

# A8 缺主链原子(N/CA/C)的残基按 Rosetta 实测行为剔除（cartesian_ddg 会 skip 该残基）
pdb8 = TMP / "noca.pdb"
l1, s = seq_lines("A", [1])
l2 = [atom_line(s, "N", "ALA", "A", 2)]  # 只有 N，没有 CA/C
l3, _ = seq_lines("A", [3], start_serial=s + 1)
write_pdb(pdb8, l1 + l2 + l3)
nm = tr.build_numbering(str(pdb8))
check("A8 缺主链残基剔除且编号连续(Rosetta 实测对齐)",
      nm.size == 2 and nm.strict.get("A_2") is None and nm.strict["A_3"] == 2
      and any("缺主链" in a for a in nm.anomalies))

# A9 多模型只取第 1 个
pdb9 = TMP / "multimodel.pdb"
l1, s = seq_lines("A", [1, 2])
model2, _ = seq_lines("A", [1, 2], start_serial=s)
text = "MODEL        1\n" + "".join(l1) + "ENDMDL\nMODEL        2\n" + "".join(model2) + "ENDMDL\nEND\n"
pdb9.write_text(text, encoding="utf-8")
nm = tr.build_numbering(str(pdb9))
check("A9 多模型只取第 1 个", nm.size == 2 and any("MODEL" in a for a in nm.anomalies))

# A10 预检（与执行同源）
ok, note = tr.precheck_targets(str(pdb4), "2")
check("A10a 预检拒绝多链歧义", not ok and "A_2" in note)
ok, note = tr.precheck_targets(str(pdb4), "A_2,B_3")
check("A10b 预检通过并列出 WT", ok and "A_2(A)" in note and "B_3(A)" in note)
ok, note = tr.precheck_targets(str(pdb7), "2")
check("A10c MSE 位点预检即拒绝(不浪费弛豫)", not ok and "不存在" in note)

# ===============================================================
# B. 任务 ID 与路径安全
# ===============================================================
base_b = TMP / "output_b"
os.makedirs(base_b, exist_ok=True)
tid1, dir1 = tr.create_task_dir("1MC9", base=str(base_b))
tid2, dir2 = tr.create_task_dir("1MC9", base=str(base_b))
check("B1a 同标签两次创建 ID 不同", tid1 != tid2 and os.path.isdir(dir1) and os.path.isdir(dir2))
check("B1b 分段兼容 skempi 解析(parts[3:]==label)",
      tid1.split("_")[3:] == ["1MC9"] and tid1.startswith("task_"))
Path(dir1, "marker.txt").write_text("keep", encoding="utf-8")
check("B1c 不覆盖已存在任务目录", os.path.exists(Path(dir1, "marker.txt")))

tid3, dir3 = tr.create_task_dir("../../evil and spaces", base=str(base_b))
check("B2 危险标签被清洗(无斜杠/空格)", "/" not in tid3 and "\\" not in tid3 and " " not in tid3)

# validate：合法/穿越/前缀/符号链接
ok_dir = tr.validate_task_dir(tid1, base=str(base_b))
check("B3a 合法 ID 通过", os.path.samefile(ok_dir, dir1))
for bad in ["", "..", "../x", f"{tid1}/..", "task_x/../../x", "not_task", "task_..x/."]:
    try:
        tr.validate_task_dir(bad, base=str(base_b))
        check(f"B3 非法ID被拒({bad!r})", False, "未抛 ValueError")
    except ValueError:
        check(f"B3 非法ID被拒({bad!r})", True)
# 符号链接指到任务根外
outside = TMP / "outside_target"
os.makedirs(outside, exist_ok=True)
link = base_b / "task_symlink_escape"
if link.exists() or link.is_symlink():
    os.remove(link)
os.symlink(str(outside), str(link))
try:
    tr.validate_task_dir("task_symlink_escape", base=str(base_b))
    check("B4 符号链接外指被拒", False, "未抛 ValueError")
except ValueError:
    check("B4 符号链接外指被拒", True)
# 旧格式 ID 兼容
legacy = base_b / "task_20260827_1934_1abc"
os.makedirs(legacy, exist_ok=True)
check("B5 旧格式 task_YYYYMMDD_HHMM_x 兼容", tr.validate_task_dir(legacy.name, base=str(base_b)) is not None)

# B6 含空格路径在 JSON argv 中保真（无 shell 参与）
space_ws = TMP / "task_with space"
os.makedirs(space_ws / "saturation_mutagenesis", exist_ok=True)
abs_arg = str(space_ws / "cartesian_relax" / "wt relaxed.pdb")
rec = {"argv": ["/opt/bin/cartesian_ddg.linuxgccrelease", "-s", abs_arg], "log": "mut log.log"}
line = json.dumps(rec, ensure_ascii=False)
back = json.loads(line)
check("B6 空格路径 JSON 行保真", back["argv"][2] == abs_arg and ">" not in line and "&&" not in line)

# ===============================================================
# C. 持久任务状态
# ===============================================================
sc = TMP / "state_c"
os.makedirs(sc, exist_ok=True)
tr.state_write(str(sc), status="created", input_summary={"entry": "test"})
tr.state_write(str(sc), status="running", stage="relax")
st = tr.state_read(str(sc))
check("C1a 生命周期合并写", st["status"] == "running" and st["input_summary"]["entry"] == "test"
      and "created_at" in st and "updated_at" in st)
# 重启模拟：另一个进程读取（subprocess 里读同一路径）
probe = subprocess.run(
    [sys.executable, "-c",
     "import json,sys;print(json.load(open(sys.argv[1]))['status'])", str(sc / "task_status.json")],
    capture_output=True, text=True, timeout=30)
check("C1b 重启后可查询(跨进程读取)", probe.returncode == 0 and probe.stdout.strip() == "running")
# 损坏/缺失
(sc / "task_status.json").write_text("{not json", encoding="utf-8")
check("C2a 损坏状态文件返回空", tr.state_read(str(sc)) == {})
check("C2b 缺失状态文件返回空", tr.state_read(str(TMP / "no_such_task")) == {})
tr.state_write(str(sc), status="running")  # 修复损坏文件
# 多任务隔离
sc2 = TMP / "state_c2"
os.makedirs(sc2, exist_ok=True)
tr.state_write(str(sc2), status="failed", error_detail="task2 only")
check("C3 多任务状态隔离", tr.state_read(str(sc))["status"] == "running"
      and tr.state_read(str(sc2))["status"] == "failed")
# running + 进程退出 + 无汇总 → interrupted；有汇总(且≥1突变) → completed
pid_dead = subprocess.Popen([sys.executable, "-c", "import sys; sys.exit(0)"])
pid_dead.wait()
tr.state_write(str(sc), status="running", runner_pids={"relax": None, "ddg": pid_dead.pid})
eff = tr.state_effective(str(sc))
check("C4a 进程已退出+无汇总 → interrupted(待核实)",
      eff["status"] == "interrupted" and "待核实" in eff["note"])
(sc / "summary").mkdir(exist_ok=True)
(sc / "summary" / "ddg_results.txt").write_text("突变\tΔΔG\nA_A2_A\t-1.5\n", encoding="utf-8")
(sc / "saturation_mutagenesis").mkdir(exist_ok=True)
(sc / "saturation_mutagenesis" / "sample.ddg").write_text("WT\nMUT\n", encoding="utf-8")
eff = tr.state_effective(str(sc))
check("C4b 进程退出+汇总与突变在位 → completed", eff["status"] == "completed")
sc3 = TMP / "state_c3"
os.makedirs(sc3 / "summary", exist_ok=True)
(sc3 / "summary" / "ddg_results.txt").write_text("突变\tΔΔG\n", encoding="utf-8")  # 空壳：仅表头
tr.state_write(str(sc3), status="running", runner_pids={"ddg": pid_dead.pid})
eff = tr.state_effective(str(sc3))
check("C4d 空壳汇总(0成功突变) → failed 不虚报成功",
      eff["status"] == "failed" and "0 个成功突变" in eff["note"])
# 存活 runner（真实 run_ddg.py 进程，cmdline 含 marker）→ running
stub_c4c = TMP / "stub_runner_c4c"
os.makedirs(stub_c4c, exist_ok=True)
(stub_c4c / "run_ddg.py").write_text("import time; time.sleep(30)\n", encoding="utf-8")
p_runner = subprocess.Popen([sys.executable, "run_ddg.py"], cwd=str(stub_c4c))
time.sleep(0.5)
tr.state_write(str(sc), status="running", runner_pids={"ddg": p_runner.pid})
eff = tr.state_effective(str(sc))
check("C4c 存活 runner → running", eff["status"] == "running")
p_runner.kill()
p_runner.wait()
# settle：错误收场 → failed
tr.settle_task_after_stream(str(sc2), had_error=True, error_type="ToolError", error_detail="x")
check("C5a settle 失败区分", tr.state_read(str(sc2))["status"] == "failed")
tr.settle_task_after_stream(str(sc2), had_error=False)
check("C5b settle 有汇总 → completed", tr.state_read(str(sc2))["status"] == "completed")

# ===============================================================
# D. 执行文件生成（不运行 Rosetta；Popen 打桩）
# ===============================================================
import rosetta_tools as rt  # noqa: E402（依赖 langchain；仅 import，不触发计算）

from contextvars import ContextVar  # noqa: E402


class _FakeProc:
    pid = 424242


real_popen = subprocess.Popen
subprocess.Popen = lambda *a, **k: _FakeProc()  # 打桩：捕获 runner 启动但不真跑

try:
    ws = TMP / "task_gen_ws space"
    (ws / "cartesian_relax").mkdir(parents=True, exist_ok=True)
    # ready 结构：A 链 1..4，缺 3（非连续）
    ready_lines, _ = seq_lines("A", [1, 2, 4])
    write_pdb(ws / "cartesian_relax" / "complex_rosetta_ready.pdb", ready_lines)
    rt.current_workspace.set(str(ws))

    out = rt.run_saturation_mutagenesis.func(target_resnum="2,3,4", jobs=2, delay=0.0)
    mut_dir = ws / "saturation_mutagenesis"
    tasks_file = mut_dir / "rosetta_tasks.txt"

    check("D1a 位点 3 不存在明确报错(不写任务)", "【执行报错】" in out and "3" in out and not tasks_file.exists())

    out = rt.run_saturation_mutagenesis.func(target_resnum="2,4", jobs=2, delay=0.0)
    txt = tasks_file.read_text(encoding="utf-8")
    lines = [l for l in txt.splitlines() if l and not l.startswith("#")]
    check("D1b 两位点×19=38 条 JSON 任务行", len(lines) == 38, f"got {len(lines)}")
    recs = [json.loads(l) for l in lines]
    argv0 = recs[0]["argv"]
    check("D1c argv 为参数列表(无 shell 元字符)",
          all((">" not in a and "&&" not in a and "|" not in a) for r in recs for a in r["argv"]))
    check("D1d ddg 旗标与旧版逐字一致",
          argv0[0].endswith("cartesian_ddg.linuxgccrelease")
          and "-ddg:iterations" in argv0 and argv0[argv0.index("-ddg:iterations") + 1] == "3"
          and "-ddg:cartesian" in argv0 and "-ddg:bbnbrs" in argv0
          and argv0[argv0.index("-score:weights") + 1] == "ref2015_cart"
          and "-relax:min_type" in argv0)
    check("D1e -s 指向 wt_relaxed(与映射结构同源)", argv0[2].endswith("wt_relaxed.pdb"))
    mutfile = next((mut_dir / r["argv"][r["argv"].index("-ddg:mut_file") + 1]) for r in recs)
    mtxt = mutfile.read_text(encoding="utf-8")
    check("D1f mutfile 用 Rosetta 绝对行号(2→2)", "A 2" in mtxt and mtxt.startswith("total 1"))

    st = tr.state_read(str(ws))
    check("D1g 状态 running + runner_pids 落盘", st.get("status") == "running"
          and (st.get("runner_pids") or {}).get("ddg") == 424242)
    mf = st.get("manifest") or {}
    check("D1h manifest 记录位点/参数/整合边界",
          mf.get("target_sites") == ["A_2(WT=A)", "A_4(WT=A)"] and mf.get("mutations_planned") == 38
          and mf.get("gromacs_ready") is False and mf.get("rosetta_version") is None)
    check("D1i 审计注释在位", "# [编号映射] PDB A_2 -> Rosetta绝对行号 2 (A)（审计）" in txt)

    # 记录待编译校验的 runner（真桩恢复后再跑 py_compile，避免与打桩冲突）
    to_compile = [mut_dir / "run_ddg.py"]

    # 弛豫 runner 生成 + 清洗规则验证（多模型/altLoc/MSE 剔除、只取第 1 模型）
    (ws / "protein.pdb").write_text(
        "MODEL        1\n"
        + atom_line(1, "N", "ALA", "A", 1) + atom_line(2, "CA", "ALA", "A", 1)
        + atom_line(3, "CA", "GLY", "A", 2, altloc="A") + atom_line(4, "CA", "GLY", "A", 2, altloc="B")
        + atom_line(5, "CA", "MSE", "A", 3)
        + "ENDMDL\nMODEL        2\n" + atom_line(9, "CA", "ALA", "A", 1) + "ENDMDL\nEND\n",
        encoding="utf-8")
    rt.run_cartesian_relax.func(protein_pdb=str(ws / "protein.pdb"), nstruct=1)
    ready = (ws / "cartesian_relax" / "complex_rosetta_ready.pdb").read_text(encoding="utf-8")
    n_atoms = sum(1 for l in ready.splitlines() if l.startswith("ATOM"))
    check("D3a 清洗后=第1模型+标准AA+altLoc去重(3原子)", n_atoms == 3, f"got {n_atoms}")
    to_compile.append(ws / "cartesian_relax" / "run_relax_parallel.py")
    relax_src = (ws / "cartesian_relax" / "run_relax_parallel.py").read_text(encoding="utf-8")
    check("D3c 弛豫 runner 为参数列表执行(无 shell)",
          "shell=True" not in relax_src and "unset DISPLAY" not in relax_src
          and "-relax:cartesian" in relax_src and "ref2015_cart" in relax_src)
    check("D3d 弛豫启动后状态 running+relax pid", tr.state_read(str(ws))["runner_pids"]["relax"] == 424242)
finally:
    subprocess.Popen = real_popen

# 生成的 runner 必须可编译且无未定义名（py_compile 抓不到 NameError——
# 2026-11-21 复核曾实测 score_path 未定义导致 runner 崩溃，故加 AST 检查）
def _undefined_names(src: str):
    import ast
    import builtins
    tree = ast.parse(src)
    defined = set(dir(builtins))

    class Collector(ast.NodeVisitor):
        def visit_Import(self, n):
            for a in n.names:
                defined.add((a.asname or a.name).split(".")[0])
        def visit_ImportFrom(self, n):
            for a in n.names:
                defined.add(a.asname or a.name)
        def visit_FunctionDef(self, n):
            defined.add(n.name)
            self.generic_visit(n)
        visit_AsyncFunctionDef = visit_FunctionDef
        def visit_ClassDef(self, n):
            defined.add(n.name)
            self.generic_visit(n)
        def visit_arg(self, n):
            defined.add(n.arg)
        def visit_ExceptHandler(self, n):
            if n.name:
                defined.add(n.name)
            self.generic_visit(n)
        def visit_Name(self, n):
            if isinstance(n.ctx, ast.Store):
                defined.add(n.id)

    Collector().visit(tree)
    loads = {n.id for n in ast.walk(tree)
             if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
    return sorted(loads - defined)


for p in to_compile:
    if p.exists():
        rc = subprocess.run([sys.executable, "-m", "py_compile", str(p)],
                            capture_output=True, text=True, timeout=60)
        undef = _undefined_names(p.read_text(encoding="utf-8"))
        check(f"D2/D3b {p.name} py_compile+未定义名检查",
              rc.returncode == 0 and not undef,
              f"undefined={undef} stderr={rc.stderr[-200:]}")
    else:
        check(f"D2/D3b {p.name} py_compile+未定义名检查", False, "文件不存在")

# D4 fixbb 走同一解析（源码级 + 行为级）
check("D4a fixbb 使用公共解析", "_resolve_targets(raw_tokens, nmap)" in
      (PROJECT_ROOT / "rosetta_tools.py").read_text(encoding="utf-8"))
ws_fix = TMP / "task_fixbb_ws"
(ws_fix / "cartesian_relax").mkdir(parents=True, exist_ok=True)
write_pdb(ws_fix / "cartesian_relax" / "wt_relaxed.pdb", seq_lines("A", [1, 2, 3])[0])
rt.current_workspace.set(str(ws_fix))
out = rt.run_fixbb_design.func(positions="B_2", nstruct=1)
check("D4b fixbb 错误链名直接拒绝", "【执行报错】" in out and "B" in out)

# ===============================================================
# E. 入口一致性（源码级：公共层委托 + 旧实现移除）
# ===============================================================
src_server = (PROJECT_ROOT / "rosetta_server.py").read_text(encoding="utf-8")
src_tools = (PROJECT_ROOT / "rosetta_tools.py").read_text(encoding="utf-8")
src_mcp = (PROJECT_ROOT / "rosetta_mcp_server.py").read_text(encoding="utf-8")
src_agent = (PROJECT_ROOT / "rosetta_agent.py").read_text(encoding="utf-8")

check("E1a server 预检委托公共层", "task_runtime.precheck_targets" in src_server)
check("E1b server 任务创建走 create_task_dir", "task_runtime.create_task_dir" in src_server)
check("E1c server 路径校验走 validate_task_dir", "task_runtime.validate_task_dir" in src_server)
check("E1d 旧退化匹配正则已从 tools 移除",
      "暴力提取纯数字进行退化匹配" not in src_tools and "num_match" not in src_tools)
check("E1e 旧预检双正则已从 server 移除", "missing.append" not in src_server)
check("E1f MCP set_workspace 严格校验", "_resolve_task_dir" in src_mcp and "task_runtime.validate_task_dir" in src_mcp)
check("E1g MCP create_task 走 create_task_dir", "task_runtime.create_task_dir" in src_mcp)
check("E1h MCP 原子工具支持显式 task_id", '__signature__' in src_mcp and 'task_id: str = ""' in src_mcp)
check("E1i Gradio/CLI 任务创建走公共层", src_agent.count("task_runtime.create_task_dir") >= 2)
check("E1j Gradio/CLI PyMOL 已改参数列表",
      "subprocess.run([pymol_bin" in src_agent and "shell=True, check=True, capture_output" not in src_agent)
check("E1k runner 任务行消费为 JSON", "json.loads(line)" in src_tools)
check("E1l 科学开关默认仍在", rt.exploratory_eval_enabled() is False and rt.disorder_filter_enabled() is False)

# guardrails 既有断言不回归（抽查源码级）
check("E2 口径护栏仍在(agent summarize)", "不等于实验验证" in src_agent)

# ---------------------------------------------------------------
failures = [name for name, ok, _ in RESULTS if not ok]
print("\n========== 结果汇总 ==========")
print(f"通过 {len(RESULTS) - len(failures)} / {len(RESULTS)}")
if failures:
    print("失败项：", failures)
    sys.exit(1)
print("全部通过 ✅")
