"""计划层验收(2026-11-21):classify_system / suggest_plan / 计划手册优先检索 / 工具挂载。
纯 Python+真实检索,不跑 Rosetta 计算。"""
import json
import os
import sys
import tempfile

PROJ = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJ)
import task_runtime as tr  # noqa: E402

TMP = tempfile.mkdtemp(prefix="playbook_eval_", dir=os.path.join(PROJ, "output"))
print(f"夹具根: {TMP}")
RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, ok, detail))
    print(("✅ " if ok else "❌ ") + name + (f"  [{detail}]" if detail and not ok else ""))


def _het_line(serial, atom_name, rn, elem="X"):
    """按 PDB 列规范拼 HETATM 行(残基名必须在 cols 18-20 = line[17:20]),内置自检。"""
    line = ("HETATM" + str(serial).rjust(5) + " "          # 1-11
            + atom_name.ljust(4)[:4] + " "                  # 13-16 + 17(altloc)
            + rn.ljust(3)[:3] + " X   1"                    # 18-20 + 21-26
            + "    0.000    0.000    0.000  1.00  0.00          " + elem)
    assert line[17:20] == rn.ljust(3)[:3], (line, repr(line[17:20]))
    return line


def write_pdb(name, chains, hetatm=(), waters=0):
    """chains: [(chain, [nums]), ...]; hetatm: [resname,...](各3原子); waters: HOH 原子数"""
    lines = []
    serial = 0
    for chain, nums in chains:
        for n in nums:
            for nm in (" N  ", " CA ", " C  "):
                serial += 1
                lines.append("ATOM  %5d %s ALA %s%4d    0.000    0.000    0.000  1.00  0.00           C"
                             % (serial, nm, chain, n))
    for rn in hetatm:
        for _ in range(3):
            serial += 1
            lines.append(_het_line(serial, " XX ", rn))
    for _ in range(waters):
        serial += 1
        lines.append(_het_line(serial, " O  ", "HOH", elem="O"))
    path = os.path.join(TMP, name)
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return path


# ============ A. 计划手册文件 ============
print("\n----- A. 计划手册文件 -----")
pb_dir = os.path.join(PROJ, "docs", "playbooks")
pb_files = sorted(os.listdir(pb_dir)) if os.path.isdir(pb_dir) else []
check("A1 三份 playbook 存在", pb_files == ["playbook_A_single_chain.md",
                                           "playbook_B_protein_ligand.md",
                                           "playbook_C_complex.md"], str(pb_files))
all_ok = all(("停止条件" in open(os.path.join(pb_dir, f), encoding="utf-8").read()
              and "默认参数" in open(os.path.join(pb_dir, f), encoding="utf-8").read())
             for f in pb_files)
check("A2 每份含停止条件与默认参数章节", all_ok)

# ============ B. classify_system ============
print("\n----- B. classify_system -----")
p_single = write_pdb("single.pdb", [("A", [1, 2, 4])])
info = tr.classify_system(p_single)
check("B1 单链 → A/单链蛋白", info["playbook"] == "A" and info["system_type"] == "single_chain"
      and info["chains"] == ["A"] and info["n_residues"] == 3,
      json.dumps(info, ensure_ascii=False)[:160])

p_lig = write_pdb("lig.pdb", [("A", [1, 2, 4])], hetatm=("LAG",), waters=2)
info = tr.classify_system(p_lig)
check("B2 单链+HETATM → B/蛋白+配体(水单独统计)",
      info["playbook"] == "B" and info["hetatm_resnames"] == ["LAG"]
      and info["waters"] == ["HOH"] and info["n_waters"] == 2,
      json.dumps(info, ensure_ascii=False)[:160])
check("B3 B 类缺项含配体参数化提示", any("配体参数化" in m for m in info["missing"]))

p_multi = write_pdb("multi.pdb", [("A", [1, 2]), ("B", [1, 2, 3])])
info = tr.classify_system(p_multi)
check("B4 两链 → C/多链复合物(缺项提示带链号)",
      info["playbook"] == "C" and info["chains"] == ["A", "B"]
      and any("带链号" in m for m in info["missing"]))

# ============ C. suggest_plan 工具 ============
print("\n----- C. suggest_plan -----")
import rosetta_tools as rt  # noqa: E402(会构建检索器)
rt.current_workspace.set(TMP)

out = rt.suggest_plan.func(protein_pdb_path=p_single)
check("C1 单链 → playbook A 指引+步骤+停止条件",
      "playbook A" in out and "run_cartesian_relax" in out and "停止条件" in out
      and "不能当结合自由能" in out, out[:200])

out = rt.suggest_plan.func(protein_pdb_path=p_lig)
check("C2 配体体系 → playbook B + parameterize_ligand 步骤",
      "playbook B" in out and "parameterize_ligand" in out and "extra_res_fa" in out, out[:200])

out = rt.suggest_plan.func(protein_pdb_path=os.path.join(TMP, "no_such.pdb"))
check("C3 文件不存在 → 执行报错", "【执行报错】" in out)

# ============ D. 检索分层 ============
print("\n----- D. 检索分层 -----")
check("D1 计划手册检索器已构建", rt.playbook_retriever is not None)
hits = rt.playbook_retriever.invoke("单链蛋白 突变扫描 计划")
check("D2 计划手册检索命中 Playbook 内容",
      bool(hits) and any("Playbook" in d.page_content or "playbook" in d.page_content
                         or "突变" in d.page_content for d in hits),
      f"{len(hits)} hits")

out = rt.search_rosetta_docs.func("单链蛋白突变扫描 计划 停止条件")
check("D3 search_rosetta_docs 计划手册优先层生效", "计划手册优先" in out, out[:200])
check("D4 通用层仍可用(总输出含通用检索段)", "检索成功" in out or "参考资料" in out or "未找到" in out)

# ============ E. 工具挂载 ============
print("\n----- E. 工具挂载 -----")
mcp_src = open(os.path.join(PROJ, "rosetta_mcp_server.py"), encoding="utf-8").read()
ag_src = open(os.path.join(PROJ, "rosetta_agent.py"), encoding="utf-8").read()
check("E1 MCP 导入+原子清单各含 suggest_plan(共 2 处)",
      mcp_src.count("suggest_plan") == 2)
check("E2 7779 对话 GENERAL_TOOLS 含 suggest_plan",
      "GENERAL_TOOLS = [suggest_plan," in ag_src)
rt_src = open(os.path.join(PROJ, "rosetta_tools.py"), encoding="utf-8").read()
check("E3 suggest_plan 为 @tool 定义且 docstring 声明只读",
      "@tool\ndef suggest_plan" in rt_src and "不启动任何计算" in rt_src)

# 汇总
failures = [name for name, ok, _ in RESULTS if not ok]
print("\n========== 结果汇总 ==========")
print(f"通过 {len(RESULTS) - len(failures)} / {len(RESULTS)}")
if failures:
    print("未通过:")
    for f_ in failures:
        print("  -", f_)
    sys.exit(1)
print("全部通过 ✅")
