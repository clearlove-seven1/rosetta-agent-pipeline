"""结构产出开关验收(2026-11-21):export_structures/export_sites。
覆盖:默认行为不变(无旗标/全 false)/全部导出/子集导出/非法输入拒绝/
stub cartesian_ddg 端到端(真实 run_ddg.py 执行:归集+落点核对+manifest+artifacts)。
不跑真实 Rosetta/LLM;stub 二进制模拟 cartesian_ddg 的落盘行为(实测命名规则)。
"""
import glob
import json
import os
import subprocess
import sys
import tempfile
import time

PROJ = os.path.dirname(os.path.abspath(__file__))
TMP = tempfile.mkdtemp(prefix="export_eval_", dir=os.path.join(PROJ, "output"))
print(f"夹具根: {TMP}")

# ---- stub cartesian_ddg:必须在 import rosetta_tools 之前放好并设置 ROSETTA_BIN_DIR ----
STUBBIN = os.path.join(TMP, "stub_bin")
os.makedirs(STUBBIN, exist_ok=True)
STUB = os.path.join(STUBBIN, "cartesian_ddg.linuxgccrelease")
with open(STUB, "w", encoding="utf-8") as f:
    f.write('''#!/usr/bin/env python3
# 模拟 cartesian_ddg 的落盘行为(实测命名规则):MUT_<行号><三字母>_bj<N>.pdb、WT_bj<N>.pdb、<mutfile去.txt>.ddg
import os, sys
args = sys.argv[1:]
def argval(flag):
    return args[args.index(flag) + 1] if flag in args else ""
mut = argval("-ddg:mut_file")
src = argval("-s")
wt = idx = mutaa = None
for l in open(mut, errors="ignore"):
    p = l.split()
    if len(p) == 3 and p[1].isdigit():
        wt, idx, mutaa = p[0], int(p[1]), p[2]
AA3 = {"A": "ALA", "C": "CYS", "D": "ASP", "E": "GLU", "F": "PHE", "G": "GLY", "H": "HIS",
       "I": "ILE", "K": "LYS", "L": "LEU", "M": "MET", "N": "ASN", "P": "PRO", "Q": "GLN",
       "R": "ARG", "S": "SER", "T": "THR", "V": "VAL", "W": "TRP", "Y": "TYR"}
aa3 = AA3.get(mutaa, "GLY")
# Rosetta 绝对行号 = 标准残基序号(按 -s 文件中残基出现顺序)
src_res, cur = [], None
for l in open(src, errors="ignore"):
    if l.startswith("ATOM") or l.startswith("HETATM"):
        key = (l[21:22], l[22:26].strip())
        if key != cur:
            cur = key
            src_res.append(key)
chain, resseq = src_res[idx - 1]
out = []
for l in open(src, errors="ignore").read().split("\\n"):
    if (l.startswith("ATOM") or l.startswith("HETATM")) and l[21:22] == chain and l[22:26].strip() == resseq:
        l = l[:17] + aa3 + l[20:]
    out.append(l)
mut_text = "\\n".join(out)
for i in (1, 2, 3):
    open("MUT_%d%s_bj%d.pdb" % (idx, aa3, i), "w").write(mut_text)
    open("WT_bj%d.pdb" % i, "w").write(open(src, errors="ignore").read())
base = os.path.basename(mut)
if base.endswith(".txt"):
    base = base[:-4]
with open(base + ".ddg", "w") as f:
    for i in (1, 2, 3):
        f.write("COMPLEX:   Round%d: WT:   100.0  fa_atr:  0.0\\n" % i)
        f.write("COMPLEX:   Round%d: MUT_%d%s:   98.0  fa_atr:  0.0\\n" % (i, idx, aa3))
''')
os.chmod(STUB, 0o755)
os.environ["ROSETTA_BIN_DIR"] = STUBBIN

import task_runtime as tr  # noqa: E402
import rosetta_tools as rt  # noqa: E402

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, ok, detail))
    print(("✅ " if ok else "❌ ") + name + (f"  [{detail}]" if detail and not ok else ""))


def pdb_text(chain, nums, aa="ALA"):
    lines = []
    serial = 0
    for n in nums:
        for name in (" N  ", " CA ", " C  "):
            serial += 1
            lines.append("ATOM  %5d %s %s %s%4d    0.000    0.000    0.000  1.00  0.00           C"
                         % (serial, name, aa, chain, n))
    return "\n".join(lines) + "\n"


def make_ws(name):
    name = "task_" + name  # _ws_task_dir 只对 task_* 目录落状态(与生产一致)
    ws = os.path.join(TMP, name)
    relax = os.path.join(ws, "cartesian_relax")
    os.makedirs(relax, exist_ok=True)
    ready = pdb_text("A", [1, 2, 4])
    with open(os.path.join(relax, "complex_rosetta_ready.pdb"), "w") as f:
        f.write(ready)
    with open(os.path.join(ws, "protein.pdb"), "w") as f:
        f.write(ready)
    with open(os.path.join(relax, "score.sc"), "w") as f:
        f.write("SCORE: -100.0 0.0 0.0 cart_relax_1\n")
    with open(os.path.join(relax, "cart_relax_1.pdb"), "w") as f:
        f.write(ready)
    rt.current_workspace.set(ws)  # 绑定工作区(与 D 组夹具一致)
    return ws


def read_recs(ws):
    with open(os.path.join(ws, "saturation_mutagenesis", "rosetta_tasks.txt"), encoding="utf-8") as f:
        return [json.loads(l) for l in f if l.strip() and not l.startswith("#")]


class _FakeProc:
    pid = 424242


real_popen = subprocess.Popen
subprocess.Popen = lambda *a, **k: _FakeProc()  # 打桩:只生成任务文件,不启动 runner

try:
    # ============ A. 生成层 ============
    print("\n----- A. 生成层 -----")
    ws1 = make_ws("e1_default")
    out1 = rt.run_saturation_mutagenesis.func(target_resnum="2,4", jobs=2, delay=0.0)
    recs1 = read_recs(ws1)
    check("A1 默认 38 行且带 export 字段", len(recs1) == 38 and all("export" in r for r in recs1),
          f"got {len(recs1)}")
    check("A2 默认全部 export=false(行为不变)", all(r["export"] is False for r in recs1))
    check("A3 argv 无 dump_pdbs 旗标", all("dump_pdbs" not in a for r in recs1 for a in r["argv"]))
    check("A4 返回文本无导出说明", "结构导出" not in out1)
    check("A5 行含 site/wt/mut/idx 供归集", all(all(k in r for k in ("site", "wt", "mut", "idx"))
                                                for r in recs1))

    ws2 = make_ws("e2_all")
    out2 = rt.run_saturation_mutagenesis.func(target_resnum="2,4", jobs=2, delay=0.0,
                                              export_structures=True)
    recs2 = read_recs(ws2)
    check("A6 全部导出:38 行全 export=true", all(r["export"] is True for r in recs2))
    check("A7 返回文本注明全部位点", "全部扫描位点" in out2)

    ws3 = make_ws("e3_subset")
    out3 = rt.run_saturation_mutagenesis.func(target_resnum="2,4", jobs=2, delay=0.0,
                                              export_structures=True, export_sites="4")
    recs3 = read_recs(ws3)
    n_true = sum(1 for r in recs3 if r["export"] is True)
    n_false = sum(1 for r in recs3 if r["export"] is False)
    check("A8 子集导出:位点4(idx3) 19 行 true / 位点2 19 行 false",
          n_true == 19 and n_false == 19
          and all(r["export"] is True for r in recs3 if r["idx"] == 3)
          and all(r["export"] is False for r in recs3 if r["idx"] == 2),
          f"true={n_true} false={n_false}")
    check("A9 返回文本注明仅位点 4", "仅位点 4" in out3)

    ws4 = make_ws("e4_unknown")
    out4 = rt.run_saturation_mutagenesis.func(target_resnum="2,4", jobs=2, delay=0.0,
                                              export_structures=True, export_sites="1")
    tf4 = os.path.join(ws4, "saturation_mutagenesis", "rosetta_tasks.txt")
    check("A10 export_sites 不在扫描位点(结构中存在但未扫) → 明确报错且不写任务",
          "【执行报错】" in out4 and "不在本次扫描位点" in out4 and not os.path.exists(tf4))

    ws5 = make_ws("e5_noexport")
    out5 = rt.run_saturation_mutagenesis.func(target_resnum="2,4", jobs=2, delay=0.0,
                                              export_sites="4")
    check("A11 export_sites 缺 export_structures → 明确报错",
          "【执行报错】" in out5 and "export_structures=True" in out5)

    ws6 = make_ws("e6_badtoken")
    out6 = rt.run_saturation_mutagenesis.func(target_resnum="2,4", jobs=2, delay=0.0,
                                              export_structures=True, export_sites="3x")
    check("A12 export_sites 非法 token → 明确报错", "【执行报错】" in out6 and "解析失败" in out6)
finally:
    subprocess.Popen = real_popen

# ============ B. stub 端到端(真实 run_ddg.py) ============
print("\n----- B. stub 端到端(真实 runner,假 Rosetta) -----")
ws = ws3
mut_dir = os.path.join(ws, "saturation_mutagenesis")
tr.state_write(ws, runner_pids={"relax": None, "ddg": None})  # 中和 pid 守卫(手工启动 runner)
logf = open(os.path.join(mut_dir, "e2e_runner.log"), "w")
try:
    subprocess.run([sys.executable, "-u", "run_ddg.py"], cwd=mut_dir,
                   stdout=logf, stderr=subprocess.STDOUT, timeout=300)
finally:
    logf.close()

st = tr.state_read(ws)
deadline = time.time() + 30
while st.get("status") == "running" and time.time() < deadline:
    time.sleep(1)
    st = tr.state_read(ws)
check("B1 终态 completed(38/38)", st.get("status") == "completed", json.dumps(st, ensure_ascii=False)[:200])

sum_rows = [l for l in open(os.path.join(ws, "summary", "ddg_results.txt"), encoding="utf-8")
            if not l.startswith("突变")]
check("B2 汇总 38 行(.ddg 全部回收)", len(sum_rows) == 38, f"got {len(sum_rows)}")

struct_root = os.path.join(mut_dir, "mutant_structures")
dirs_ = sorted(d for d in (os.listdir(struct_root) if os.path.isdir(struct_root) else [])
               if os.path.isdir(os.path.join(struct_root, d)))
check("B3 mutant_structures 恰 19 个目录(仅位点4,清单文件不计)",
      len(dirs_) == 19 and all(d.startswith("A_A_4_") for d in dirs_), f"got {len(dirs_)}: {dirs_[:3]}")
one = os.path.join(struct_root, dirs_[0]) if dirs_ else ""
n_pdb = len(glob.glob(os.path.join(one, "MUT_*.pdb"))) if one else 0
check("B4 目录内 3 个 MUT 结构(bj1-3)", n_pdb == 3, f"got {n_pdb}")

sm_path = os.path.join(struct_root, "structures_manifest.json")
sm = json.load(open(sm_path, encoding="utf-8")) if os.path.exists(sm_path) else {}
entries = sm.get("entries", [])
check("B5 structures_manifest.json 19 条且全部 verified=yes",
      len(entries) == 19 and all(e["verified"] == "yes" for e in entries),
      json.dumps([e for e in entries if e["verified"] != "yes"][:2], ensure_ascii=False)[:200])

scattered = glob.glob(os.path.join(mut_dir, "MUT_2*_bj*.pdb"))
check("B6 非所选位点(位点2)结构仍在突变目录散落(不删除)", len(scattered) == 57, f"got {len(scattered)}")

mf = st.get("manifest") or {}
check("B7 manifest 登记 structures_exported/verified=19",
      mf.get("structures_exported") == 19 and mf.get("structures_verified") == 19,
      json.dumps({k: mf.get(k) for k in ("structures_exported", "structures_verified")},
                 ensure_ascii=False))
check("B8 artifacts.mutant_structures 已登记",
      (st.get("artifacts") or {}).get("mutant_structures") == "saturation_mutagenesis/mutant_structures")
check("B9 manifest structures_export 开关已登记", mf.get("structures_export") is True)

cc = tr.contract_check(ws)
check("B10 contract_check ok=True(契约不受影响,sha/终态齐)", cc["ok"] and not cc["legacy"],
      json.dumps(cc["missing"], ensure_ascii=False)[:160])

e2e_log = open(os.path.join(mut_dir, "e2e_runner.log"), encoding="utf-8").read()
check("B11 runner 正常跑完(结束标志在)", "突变全流程结束" in e2e_log and "Traceback" not in e2e_log)

# ============ 汇总 ============
failures = [name for name, ok, _ in RESULTS if not ok]
print("\n========== 结果汇总 ==========")
print(f"通过 {len(RESULTS) - len(failures)} / {len(RESULTS)}")
if failures:
    print("未通过:")
    for f_ in failures:
        print("  -", f_)
    sys.exit(1)
print("全部通过 ✅")
