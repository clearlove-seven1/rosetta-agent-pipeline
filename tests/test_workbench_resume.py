"""workbench 断点恢复校验的单元测试（纯逻辑，不依赖 Rosetta / langchain）

运行：python tests/test_workbench_resume.py
覆盖 validate_resume：输入哈希被改 → 整体拒绝；产物缺失/无哈希 → 该步重算；
产物哈希匹配 → 可复用；旧 schema 字段缺失 → 拒绝恢复。
"""

import hashlib
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from workbench.resume import validate_resume

RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond)))
    print(f"{'✅ PASS' if cond else '❌ FAIL'}  {name}" + (f"  | {detail}" if detail and not cond else ""))


def sha(path):
    return hashlib.sha256(open(path, "rb").read()).hexdigest()


tmp = tempfile.mkdtemp(prefix="wb_resume_")

# 构造输入文件与其清洗产物、relax/ddg 产物
src = os.path.join(tmp, "protein.pdb")
open(src, "w").write("ATOM      1  CA  ALA A   1\nEND\n")
clean = os.path.join(tmp, "clean_protein.pdb")
open(clean, "w").write("ATOM      1  CA  ALA A   1\nEND\n")
relaxed = os.path.join(tmp, "relaxed_0001.pdb")
open(relaxed, "w").write("REMARK relaxed\n")
ddgf = os.path.join(tmp, "sample.ddg")
open(ddgf, "w").write("COMPLEX:   Round1: WT:  -1.0\nCOMPLEX:   Round1: MUT_26PRO:  -2.0\n")


def base_manifest():
    return {
        "run_id": "test_run", "status": "interrupted",
        "task": {
            "protein_path": src,
            "protein_sha256": sha(src),
            "cleaned_protein": clean,
            "cleaned_protein_sha256": sha(clean),
            "seed": 1111111, "nstruct": 1, "iterations": 3,
            "seq_index": 26, "wt_aa": "Q", "mut_aa": "P",
            "chain": "A", "resnum": 29,
        },
        "steps": {
            "relax": {"status": "succeeded", "relaxed_pdb": relaxed,
                      "relaxed_pdb_sha256": sha(relaxed)},
            "cartesian_ddg": {"status": "succeeded", "ddg_file": ddgf,
                              "ddg_file_sha256": sha(ddgf)},
        },
        "result": {"label_check": "passed"},
        "config": {"score_function": "ref2015_cart"},
    }


# 1. 一切匹配 → 可恢复且两步皆可复用
v = validate_resume(base_manifest())
check("全匹配: ok=True", v["ok"] is True, str(v["fatal"]))
check("全匹配: relax 可复用", v["reusable"]["relax"] is True)
check("全匹配: ddg 可复用", v["reusable"]["ddg"] is True)

# 2. 输入被改动 → 整体拒绝
m2 = base_manifest()
m2["task"]["protein_sha256"] = "0" * 64
v = validate_resume(m2)
check("输入被改: ok=False", v["ok"] is False)
check("输入被改: fatal 提及禁止复用", any("禁止复用" in f for f in v["fatal"]), str(v["fatal"]))

# 3. 输入文件被删除 → 拒绝
m3 = base_manifest()
m3["task"]["protein_path"] = os.path.join(tmp, "gone.pdb")
v = validate_resume(m3)
check("输入缺失: 拒绝", v["ok"] is False and any("不存在" in f for f in v["fatal"]))

# 4. relax 产物缺失 → relax 不可复用（重算），但整体可恢复
m4 = base_manifest()
m4["steps"]["relax"]["relaxed_pdb"] = os.path.join(tmp, "no_such.pdb")
v = validate_resume(m4)
check("relax 产物缺失: 整体可恢复", v["ok"] is True)
check("relax 产物缺失: relax 不可复用", v["reusable"]["relax"] is False)
check("relax 产物缺失: ddg 仍可复用", v["reusable"]["ddg"] is True)

# 5. relax 产物被改动（哈希不符）→ 不可复用
m5 = base_manifest()
open(relaxed, "a").write("REMARK tampered\n")
v = validate_resume(m5)
check("relax 产物被改: 不可复用", v["reusable"]["relax"] is False)
open(relaxed, "w").write("REMARK relaxed\n")  # 还原

# 6. 旧 schema 无哈希字段 → 不可复用（防静默复用）
m6 = base_manifest()
del m6["steps"]["relax"]["relaxed_pdb_sha256"]
v = validate_resume(m6)
check("旧schema无哈希: relax 不可复用", v["reusable"]["relax"] is False)
check("旧schema: 有说明性 note", any("哈希记录" in n for n in v["notes"]))

# 7. label_check 未通过的 ddg 不可复用
m7 = base_manifest()
m7["result"]["label_check"] = "failed"
v = validate_resume(m7)
check("标签未过的 ddg: 不可复用", v["reusable"]["ddg"] is False)

# 8. manifest 缺输入哈希字段（schema 过旧）→ 拒绝
m8 = base_manifest()
del m8["task"]["protein_sha256"]
v = validate_resume(m8)
check("旧schema缺输入哈希: 拒绝恢复", v["ok"] is False and any("schema" in f for f in v["fatal"]))

# 9. result 为 None（中断恢复中间态）不崩溃；label_check 未知 → ddg 不可复用
m9 = base_manifest()
m9["result"] = None
v = validate_resume(m9)
check("result=None: 不崩溃且可恢复", v["ok"] is True)
check("result=None: ddg 不可复用（label 未知）", v["reusable"]["ddg"] is False)

# ---- 汇总 ----
fails = [n for n, ok in RESULTS if not ok]
print("\n========== 结果汇总 ==========")
print(f"通过 {len(RESULTS) - len(fails)} / {len(RESULTS)}")
sys.exit(1 if fails else 0)
