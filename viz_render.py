"""PyMOL 发表级结构渲染（供 rosetta_server.py 的 /api/viz/render 调用）。

5 种"发表级"样式取自用户指定参考文章《你的PyMOL图为什么不够高级？试试这5种风格
（附直接运行代码）》(微信公众号, 2026-11-14 提供)，并在本项目语境下做了适配：
  - 原文针对单链蛋白 fetch 8b4u；本模块加载用户自己的复合物 PDB；
  - 位点聚焦：select site → sticks 高亮 + 标注 → zoom 到该残基附近；
  - 支持快速预览（OpenGL 直绘，秒级）与发表级渲染（ray 光追，数十秒）。

独立测试（rosetta-agent env，项目根目录）：
  python viz_render.py <pdb路径> [style] [chain] [resi] [fast|ray] [输出png]
"""
import base64
import datetime
import os
import subprocess
import time


def _resolve_pymol() -> str:
    """渲染优先用开源版 PyMOL（pymol-open-source，无授权水印）。

    /usr/local/bin/pymol 是 Schrödinger 评估版 3.1.8，导出 PNG 会盖
    "No License File - For Evaluation Only" 水印，不可用于发表级出图。
    """
    cand = os.getenv("PYMOL_VIZ_BIN", "")
    if cand and os.path.exists(cand):
        return cand
    env_pymol = os.path.expanduser("~/.conda/envs/rosetta-agent/bin/pymol")
    if os.path.exists(env_pymol):
        return env_pymol
    return os.getenv("PYMOL_BIN", "/usr/local/bin/pymol")


PYMOL_BIN = _resolve_pymol()

# 各样式的几何/配色/材质命令块（忠实转录原文，"chain A" 适配为整个复合物）
_STYLE_BLOCKS = {
    # 01 柔美漫射：CNS 最主流，柔光、无生硬高光
    "soft": [
        "set cartoon_fancy_helices, 1",
        "set cartoon_highlight_color, grey50",
        "color teal, complex",
    ],
    # 02 扁平矢量插画：无三维光影，黑轮廓线，适合机制图背景
    "flat": [
        "set cartoon_flat_sheets, 1",
        "set cartoon_rect_length, 1.4",
        "set cartoon_smooth_loops, 0",
        "set_color deep_teal, [0.0, 0.5, 0.5]",
        "color deep_teal, complex",
        "set ray_trace_mode, 1",
        "set ray_trace_color, black",
        "set ray_trace_gain, 0.5",
        "set ray_shadows, 0",
        "set ambient, 0.5",
        "set specular, 0",
    ],
    # 03 高亮琉璃：彩虹配色 + 强反光玻璃质感
    "glass": [
        "set cartoon_oval_length, 1.0",
        "set cartoon_oval_width, 0.3",
        "spectrum count, rainbow, complex",
        "set ray_trace_mode, 1",
        "set ray_shadows, 1",
        "set light_count, 8",
        "set spec_reflect, 1.0",
        "set specular, 1.0",
        "set shininess, 100",
        "set direct, 0.8",
    ],
    # 04 拓扑结构：圆柱螺旋/箭头折叠的教科书风格
    "topo": [
        "set cartoon_cylindrical_helices, 1",
        "set cartoon_rect_width, 0.5",
        "set cartoon_rect_length, 1.5",
        "set cartoon_loop_radius, 0.2",
        "color grey80, all",
        "color tv_red, ss h",
        "color tv_yellow, ss s",
        "set ray_trace_mode, 1",
        "set ray_trace_gain, 0.01",
        "set ambient, 0.3",
        "set specular, 0.5",
        "set shininess, 50",
    ],
    # 05 流体/液态金属：putty 管径 + 金属渐变（原文 palette 名不可移植，用自定义三色近似）
    "metal": [
        "cartoon putty",
        "set cartoon_putty_radius, 0.6",
        "set cartoon_ladder_mode, 0",
        "set_color m_teal, [0.10, 0.45, 0.55]",
        "set_color m_blue, [0.05, 0.25, 0.60]",
        "set_color m_deep, [0.02, 0.10, 0.35]",
        "spectrum count, m_teal m_blue m_deep, complex",
        "set ray_trace_mode, 1",
        "set light_count, 5",
        "set specular, 1.5",
        "set shininess, 100",
        "set direct, 0.6",
        "set reflect, 0.5",
        "set ray_shadows, 1",
        "set ray_shadow_decay_factor, 0.1",
    ],
}

STYLE_NAMES = {
    "soft": "柔美漫射（CNS 主流）",
    "flat": "扁平矢量插画",
    "glass": "高亮琉璃",
    "topo": "拓扑结构（教科书）",
    "metal": "流体/液态金属",
}

# 表示方法 / 配色（联动交互面板的两个下拉；rep+color 非空时优先于具名样式）
_REP_BLOCKS = {
    "cartoon": ["show cartoon"],
    "cartoon_surf": ["show cartoon",
                     "set transparency, 0.55",
                     "show surface"],
    "stick": ["hide cartoon", "show sticks", "set stick_radius, 0.25"],
    "line": ["hide cartoon", "show lines"],
    "sphere": ["hide cartoon", "show spheres"],
    "ball_stick": ["hide cartoon", "show sticks", "set stick_radius, 0.22",
                   "show spheres", "set sphere_scale, 0.28"],
}

# 与前端 viz-color 下拉的 value 一一对应（语义同交互 3D 的 applyRep）
_COLOR_BLOCKS = {
    "spectrum": ["spectrum count, rainbow, complex"],   # 彩虹渐变
    "chain": ['util.cbc("complex")'],                    # 按链配色
    "ssPyMOL": ["color tv_red, complex and ss h",        # 二级结构配色
                "color tv_yellow, complex and ss s",
                "color orange, complex and ss l"],
    "teal": ["set_color viz_teal, [0.055, 0.486, 0.525]",  # 单色青
             "color viz_teal, complex"],
    "byelement": ["set_color e_c, [0.200, 0.300, 0.320]", "color e_c, elem C",   # 按元素配色
                  "set_color e_o, [0.906, 0.204, 0.220]", "color e_o, elem O",
                  "set_color e_n, [0.145, 0.443, 0.937]", "color e_n, elem N",
                  "set_color e_s, [1.000, 0.820, 0.240]", "color e_s, elem S",
                  "set_color e_h, [0.850, 0.850, 0.850]", "color e_h, elem H"],
}
_COLOR_BLOCKS["ss"] = _COLOR_BLOCKS["ssPyMOL"]   # 前端新取值别名


def build_script(pdb_path: str, out_png: str, style: str = "soft",
                 chain: str = "", resi: str = "",
                 width: int = 900, height: int = 640, ray: bool = False,
                 rep: str = "", color: str = "") -> str:
    """组装 PyMOL -d 脚本：加载结构 → 样式 → 位点聚焦 → 渲染落盘。

    color 非空且已知时走"表示方法×配色"动态块（与交互面板联动），
    否则回退到 5 种具名发表级样式。
    """
    base = [
        "reinitialize",
        f"load {pdb_path}, complex",
        "hide everything",
        "bg_color white",
    ]
    color = (color or "").strip()
    rep = (rep or "").strip()
    if color and color in _COLOR_BLOCKS:
        cmds = base + [
            *_REP_BLOCKS.get(rep, _REP_BLOCKS["cartoon"]),
            *_COLOR_BLOCKS[color],
            "set ray_shadows, 0",
            "set specular, 0.15",
            "set ambient, 0.5",
            "set antialias, 2",
        ]
    else:
        style = style if style in _STYLE_BLOCKS else "soft"
        cmds = base + ["show cartoon", *_STYLE_BLOCKS[style]]

    chain = (chain or "").strip()
    resi = (resi or "").strip()
    if chain and resi:
        sel = f"complex and chain {chain} and resi {resi}"
        cmds += [
            f"select site, {sel}",
            "show sticks, site",
            "color yellow, site",
            "set label_font_id, 2",
            "set label_size, 24",
            "set label_color, black",
            # 只给 CA 原子打标签：site 含残基全部原子，直接 label 会每个原子叠一个标签
            f'label site and name CA, "{chain}_{resi}"',
            "zoom site, 15",   # 以位点为中心、留 15Å 缓冲（保持整体朝向，实现"转移过去"）
        ]
    else:
        cmds.append("orient complex")

    if ray:
        cmds += [
            "set antialias, 2",
            "set ray_opaque_background, 1",  # 开源版 ray 默认透明底，显式白底与界面卡片一致
            f"ray {width}, {height}",
            f"png {out_png}, dpi=150",
        ]
    else:
        cmds += [
            f"viewport {width}, {height}",
            f"png {out_png}, dpi=150",
        ]
    return "\n".join(cmds)


def render(pdb_path: str, out_png: str, style: str = "soft", chain: str = "",
           resi: str = "", width: int = 900, height: int = 640,
           ray: bool = False, timeout: int = 240,
           rep: str = "", color: str = "") -> dict:
    """调 PyMOL 无头渲染。返回 {"ok", "image_b64"(成功时), "elapsed_s", "script"} 或 {"ok":False,"error"}。"""
    if not os.path.exists(pdb_path):
        return {"ok": False, "error": f"PDB 不存在: {pdb_path}"}
    script = build_script(pdb_path, out_png, style=style, chain=chain,
                          resi=resi, width=width, height=height, ray=ray,
                          rep=rep, color=color)
    if os.path.exists(out_png):
        os.remove(out_png)
    t0 = time.time()
    try:
        proc = subprocess.run([PYMOL_BIN, "-c", "-d", script],
                              capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"渲染超时（>{timeout}s）", "script": script}
    elapsed = round(time.time() - t0, 1)
    if not os.path.exists(out_png) or os.path.getsize(out_png) == 0:
        tail = "\n".join((proc.stderr or "").strip().splitlines()[-6:])
        return {"ok": False, "error": f"PyMOL 未生成 PNG（{elapsed}s）。{tail}", "script": script}
    with open(out_png, "rb") as f:
        return {"ok": True, "image_b64": base64.b64encode(f.read()).decode(),
                "elapsed_s": elapsed, "script": script}


if __name__ == "__main__":
    import sys
    pdb = sys.argv[1]
    style = sys.argv[2] if len(sys.argv) > 2 else "soft"
    chain = sys.argv[3] if len(sys.argv) > 3 else ""
    resi = sys.argv[4] if len(sys.argv) > 4 else ""
    mode = sys.argv[5] if len(sys.argv) > 5 else "fast"
    out = sys.argv[6] if len(sys.argv) > 6 else f"/tmp/viz_test_{style}.png"
    r = render(pdb, out, style=style, chain=chain, resi=resi,
               ray=(mode == "ray"))
    print("elapsed:", r.get("elapsed_s"), "ok:", r["ok"], r.get("error", ""))
    if r["ok"]:
        print("png bytes:", os.path.getsize(out))
    if not r["ok"]:
        print("script was:\n", r.get("script", ""))
