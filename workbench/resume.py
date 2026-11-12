"""断点恢复校验（v0.3）

原则（规划 §5.4）：恢复前必须逐项检查输入、配置、依赖与已有产物；
**任何一项不匹配就拒绝复用该步产物（重算）或整体拒绝（输入被改动），
绝不静默复用**。校验逻辑保持纯函数（只做文件系统与哈希比对），可单测。
"""

import hashlib
import os


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def validate_resume(manifest: dict) -> dict:
    """检查一个既有 run 是否可恢复。返回：

    {
      "ok": bool,                       # False = 整体拒绝恢复
      "fatal": [str],                   # 致命问题（输入被改/依赖缺失/字段缺失）
      "reusable": {"relax": bool, "ddg": bool},   # 各步产物是否校验通过可复用
      "notes": [str],                   # 非致命说明（如实展示给用户）
    }
    """
    fatal = []
    notes = []
    reusable = {"relax": False, "ddg": False}

    # ── 依赖仍在 ──
    from .executors import check_dependencies
    missing = check_dependencies()
    if missing:
        fatal.append(f"依赖缺失: {missing}")

    # ── 输入文件未被改动（哈希比对；字段缺失按 unknown 处理并说明）──
    task = manifest.get("task", {})
    src = task.get("protein_path")
    recorded = task.get("protein_sha256")
    if not src or not recorded:
        fatal.append("manifest 缺少输入路径或哈希字段（schema 过旧），拒绝恢复")
    elif not os.path.exists(src):
        fatal.append(f"原始输入不存在: {src}")
    elif _sha256(src) != recorded:
        fatal.append(f"原始输入内容已变化（sha256 不匹配）: {src} —— 禁止复用任何旧产物，请新建运行")
    else:
        notes.append(f"输入校验通过: {os.path.basename(src)}")

    # ── 清洗产物（relax 的真实输入）──
    clean = task.get("cleaned_protein")
    clean_hash = task.get("cleaned_protein_sha256")
    if clean and clean_hash:
        if os.path.exists(clean) and _sha256(clean) == clean_hash:
            notes.append("清洗产物校验通过")
        else:
            # 清洗产物可从原始输入确定性重建，缺/不一致只提示重建，不算致命
            notes.append("清洗产物缺失或不一致——恢复时将自动重新生成")

    # ── relax 产物可否复用 ──
    relax = manifest.get("steps", {}).get("relax", {})
    if relax.get("status") == "succeeded":
        rp = relax.get("relaxed_pdb")
        rp_hash = relax.get("relaxed_pdb_sha256")
        if not rp_hash:
            notes.append("relax 产物无哈希记录（v0.2 旧任务）——不可复用，将重算")
        elif rp and os.path.exists(rp) and _sha256(rp) == rp_hash:
            reusable["relax"] = True
            notes.append(f"relax 产物校验通过，可复用: {os.path.basename(rp)}")
        else:
            notes.append("relax 产物缺失或哈希不符——将重算 relax")

    # ── ddg 产物可否复用 ──
    ddg = manifest.get("steps", {}).get("cartesian_ddg", {})
    result = manifest.get("result") or {}   # 键可能存在但值为 None（中断恢复中间态）
    if ddg.get("status") == "succeeded" and result.get("label_check") == "passed":
        df = ddg.get("ddg_file")
        df_hash = ddg.get("ddg_file_sha256")
        if not df_hash:
            notes.append("ddg 产物无哈希记录——不可复用，将重算")
        elif df and os.path.exists(df) and _sha256(df) == df_hash:
            reusable["ddg"] = True
            notes.append(f"ddg 产物校验通过，可复用: {os.path.basename(df)}")
        else:
            notes.append("ddg 产物缺失或哈希不符——将重算 ddg")

    ok = not fatal
    return {"ok": ok, "fatal": fatal, "reusable": reusable, "notes": notes}
