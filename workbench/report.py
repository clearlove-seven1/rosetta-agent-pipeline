"""人读报告渲染（v0.2）：如实陈述口径、映射、结果与局限。"""

import os


def render(manifest: dict, ddg_info: dict, label_check: "tuple[bool, str]",
           numbering_note: str) -> str:
    t = manifest["task"]
    cfg = manifest["config"]
    sw = manifest["software"]
    relax = manifest["steps"].get("relax", {})
    ddg = manifest["steps"].get("cartesian_ddg", {})

    lines = [
        "=" * 64,
        "单点突变稳定性计算报告（Protein Design Workbench v0.2）",
        "=" * 64,
        "",
        f"任务: {t.get('name') or manifest['run_id']}   run_id: {manifest['run_id']}",
        f"状态: {manifest['status']}",
        f"输入: {t['protein_path']}  (sha256 {t['protein_sha256'][:12]}…)",
        f"突变: 链 {t['chain']} | PDB 编号 {t['resnum_pdb']} | "
        f"WT {t['wt_aa']} → MUT {t['mut_aa']} | 文件内顺序编号 #{t.get('seq_index')}",
        "",
        "── 口径与单位 ──",
        "计算类型: cartesian_ddg 常规稳定性模式（非界面模式）",
        "输出量  : ΔΔG = avg(MUT) - avg(WT)，单位 REU（非 kcal/mol）",
        "方向约定: ΔΔG > 0 倾向去稳定化；ΔΔG < 0 倾向稳定化",
        "声明    : 计算筛选 ≠ 实验验证；本结果不能外推为结合亲和力",
        "",
        "── 编号映射 ──",
        numbering_note,
        "",
        "── 执行信息 ──",
        f"Rosetta : {sw.get('rosetta', 'unknown')[:80]}",
        f"Git     : {sw.get('git_commit', 'unknown')[:12]}",
        f"随机种子: {cfg['seed']} (relax & ddg 均 constant_seed)",
        f"relax   : {relax.get('duration_s', '?')}s, 退出码 {relax.get('returncode', '?')}",
        f"ddg     : {ddg.get('duration_s', '?')}s, 退出码 {ddg.get('returncode', '?')}, "
        f"{ddg_info.get('n_rounds', '?')} 轮迭代",
        "",
        "── 结果 ──",
        f"WT  各轮: {[round(s, 3) for s in ddg_info['wt_scores']]}  avg={ddg_info['wt_avg']:.3f} REU",
        f"MUT 各轮: {[round(s, 3) for s in ddg_info['mut_scores']]}  avg={ddg_info['mut_avg']:.3f} REU",
        f"ΔΔG     : {ddg_info['ddg']:+.3f} REU  ({_direction(ddg_info['ddg'])})",
        "",
        "── 标签核对 ──",
        label_check[1],
        "",
        "── 局限 ──",
        "· 单次计算、单一结构参考态；未做重复采样与统计检验",
        "· ref2015_cart 对界面/配体/后修饰的描述有限",
        "· 无实验对照时，不得将本结果写入任何结论性表述",
    ]
    if manifest["task"].get("warnings"):
        lines.insert(lines.index("") + 1, "⚠️ 输入警告: " + "；".join(manifest["task"]["warnings"]))
    return "\n".join(lines)


def _direction(ddg: float) -> str:
    if ddg > 1.0:
        return "倾向去稳定化"
    if ddg > 0:
        return "弱去稳定化"
    if ddg < -1.0:
        return "倾向稳定化"
    return "弱稳定化/近中性"
