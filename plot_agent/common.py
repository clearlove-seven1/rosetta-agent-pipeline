"""公共工具：全局 matplotlib 风格、调色板、plotly HTML 包装、数值/序列辅助。"""
from __future__ import annotations

import html as _html

import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib import font_manager as fm

SAVE_DPI = 160

# 优先可用中文字体（fc-list 已确认 amax 有 Noto Sans CJK SC）
def _pick_cjk_font() -> str:
    for fam in ("Noto Sans CJK SC", "Noto Sans CJK JP", "Droid Sans Fallback",
                "WenQuanYi Micro Hei", "Source Han Sans SC", "SimHei", "Microsoft YaHei"):
        try:
            path = fm.findfont(fm.FontProperties(family=fam), fallback_to_default=False)
            if path and "DejaVu" not in path:
                return fam
        except Exception:
            continue
    return "DejaVu Sans"

CJK_FONT = _pick_cjk_font()

plt.rcParams.update({
    "figure.facecolor": "white", "axes.facecolor": "white", "savefig.facecolor": "white",
    "axes.edgecolor": "#B9C2D0", "axes.linewidth": 1.0,
    "axes.grid": True, "grid.color": "#E4E9F1", "grid.linewidth": 0.8,
    "axes.spines.top": False, "axes.spines.right": False,
    "font.size": 11, "axes.titlesize": 13, "axes.labelsize": 11.5,
    "legend.frameon": False, "legend.fontsize": 10,
    "axes.unicode_minus": False,
    "font.sans-serif": [CJK_FONT, "DejaVu Sans"],
})

PALETTE = ["#4C6FE8", "#E8743C", "#3FA66A", "#B057D6", "#D6A83C", "#3BB3C3",
           "#E8546D", "#7A8699", "#8FC93A", "#C46BE8", "#5A72B5", "#E89C3C"]

# 氨基酸化学分类配色（序列 logo / 组成图用）
AA_CLASSES = {
    "hydrophobic": ("AVLIFWMP", "#2E7D32"),
    "polar":       ("STNQ", "#E8912A"),
    "positive":    ("KRH", "#1565C0"),
    "negative":    ("DE", "#C62828"),
    "special":     ("G", "#8E24AA"),
    "aromatic":    ("Y", "#AD1457"),
}
AA20 = "ACDEFGHIKLMNPQRSTVWY"
NT = "ACGTUN"

def aa_color(aa: str) -> str:
    for _, (letters, color) in AA_CLASSES.items():
        if aa in letters:
            return color
    return "#7A8699"


def prep_fig(w: float = 8.4, h: float = 5.0):
    return plt.subplots(figsize=(w, h))


def save_png(fig, path: str) -> None:
    fig.tight_layout()
    fig.savefig(path, dpi=SAVE_DPI, bbox_inches="tight")
    plt.close(fig)


def num(s) -> pd.Series:
    return pd.to_numeric(s, errors="coerce")


def cat_colors(values) -> dict:
    uniq = list(pd.unique(pd.Series(values).dropna()))
    return {v: PALETTE[i % len(PALETTE)] for i, v in enumerate(uniq)}


def short(v, n: int = 26) -> str:
    s = str(v)
    return s if len(s) <= n else s[: n - 1] + "…"


def fmt(v, nd: int = 3) -> str:
    try:
        v = float(v)
    except (TypeError, ValueError):
        return str(v)
    if abs(v) >= 1e5 or (abs(v) < 1e-3 and v != 0):
        return f"{v:.2e}"
    return f"{v:.{nd}g}"


def corr(x, y):
    x = np.asarray(x, float); y = np.asarray(y, float)
    ok = np.isfinite(x) & np.isfinite(y)
    if ok.sum() < 3:
        return None
    return float(np.corrcoef(x[ok], y[ok])[0, 1])


def size_scale(vals, lo=24.0, hi=260.0) -> np.ndarray:
    """把数值列映射为散点大小。"""
    v = pd.to_numeric(pd.Series(vals), errors="coerce").to_numpy(float)
    fin = np.isfinite(v)
    out = np.full(len(v), lo, float)
    if fin.any():
        mn, mx = v[fin].min(), v[fin].max()
        span = (mx - mn) or 1.0
        out[fin] = lo + (v[fin] - mn) / span * (hi - lo)
    return out


def kde_grid(x, y, bins=90, sample=20000):
    """二维高斯核密度网格；样本过多自动抽样。返回 (xi, yi, Z) 或 None。"""
    from scipy.stats import gaussian_kde
    x = np.asarray(x, float); y = np.asarray(y, float)
    ok = np.isfinite(x) & np.isfinite(y)
    x, y = x[ok], y[ok]
    if len(x) < 8:
        return None
    if len(x) > sample:
        idx = np.random.default_rng(0).choice(len(x), sample, replace=False)
        x, y = x[idx], y[idx]
    xi = np.linspace(x.min(), x.max(), bins)
    yi = np.linspace(y.min(), y.max(), bins)
    X, Y = np.meshgrid(xi, yi)
    try:
        Z = gaussian_kde(np.vstack([x, y]))(np.vstack([X.ravel(), Y.ravel()])).reshape(X.shape)
    except Exception:
        return None
    return xi, yi, Z


def wrap_html(fig, title: str, embed_js: bool = False) -> str:
    """plotly Figure → 自包含 HTML（默认引 CDN，可内嵌 JS 离线可用）。"""
    import plotly.io as pio
    inner = pio.to_html(
        fig, full_html=False, include_plotlyjs=False,
        config={"displaylogo": False, "responsive": True,
                "modeBarButtonsToRemove": ["lasso2d", "select2d"]})
    if embed_js:
        js = "<script>" + pio.get_plotlyjs() + "</script>"
    else:
        js = '<script src="https://cdn.plot.ly/plotly-2.35.2.min.js" charset="utf-8"></script>'
    t = _html.escape(title)
    return ("<!doctype html><html lang=\"zh\"><head><meta charset=\"utf-8\">"
            f"<title>{t}</title><style>html,body{{margin:0;background:#fff}}"
            ".pc{max-width:1150px;margin:0 auto;padding:18px 22px;box-sizing:border-box;"
            "font-family:system-ui,-apple-system,'Segoe UI','PingFang SC','Microsoft YaHei',sans-serif}"
            "h1{font-size:17px;color:#28324a;margin:0 0 10px}</style></head><body>"
            f"{js}<div class=\"pc\"><h1>{t}</h1>{inner}</div></body></html>")
