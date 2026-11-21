"""表格读取与列类型剖析：numeric / categorical / datetime / sequence / text / binary。"""
from __future__ import annotations

import os
import re
import numpy as np
import pandas as pd

AA_SET = set("ACDEFGHIKLMNPQRSTVWYXBZUJ")
NT_SET = set("ACGTUNRYKMSWBDHV-")

_NUM_RE = re.compile(r"^[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?$")


def _guess_sep(path: str, enc: str) -> str:
    """按数据行里各分隔符的平均出现密度选分隔符。

    避免 csv.Sniffer 被表头里的逗号（如「ΔΔG(REU, min(MUT)-min(WT))」）带偏：
    只有数据行也高频出现该字符才算候选，否则退到空白分隔。
    """
    import collections
    counts = collections.Counter()
    n = 0
    with open(path, encoding=enc, errors="replace") as f:
        for line in f:
            line = line.rstrip("\n\r")
            if not line.strip():
                continue
            n += 1
            counts["\t"] += line.count("\t")
            counts[","] += line.count(",")
            counts[";"] += line.count(";")
            counts["|"] += line.count("|")
            if n >= 50:
                break
    if not n:
        return ","
    best, c = max(counts.items(), key=lambda kv: kv[1])
    if c / n >= 0.5:
        return best
    return r"\s+"


def load_table(path: str, sheet: str | None = None) -> pd.DataFrame:
    """按扩展名读 CSV/TSV/TXT/XLSX；分隔符与编码自动探测。"""
    ext = os.path.splitext(path)[1].lower()
    if ext in (".xlsx", ".xlsm", ".xls"):
        return pd.read_excel(path, sheet_name=sheet or 0)
    errors = []
    for enc in ("utf-8-sig", "utf-8", "gbk", "latin-1"):
        try:
            sep = _guess_sep(path, enc)
            df = pd.read_csv(path, sep=sep, engine="python", encoding=enc)
            if df.shape[1] > 0:
                return df
        except Exception as e:  # 换下一个编码
            errors.append(f"{enc}: {e}")
    raise ValueError("无法解析该表格（已尝试 utf-8/gbk/latin-1 与自动分隔符）：" +
                     "; ".join(errors[-2:]))


def _is_seqish(values: pd.Series) -> bool:
    """列是否像生物序列（氨基酸或核酸）。"""
    vals = values.dropna().astype(str)
    if len(vals) == 0:
        return False
    lens = vals.str.len()
    med = float(lens.median())
    if med < 8 or med > 5000:
        return False
    sample = vals.head(200)
    chars = "".join(sample)
    if not chars:
        return False
    known = sum(1 for c in chars.upper() if c in AA_SET or c in NT_SET)
    if known / max(len(chars), 1) < 0.92:
        return False
    if set(chars.upper()) <= NT_SET:   # 核酸也算序列
        return True
    aa_frac = sum(1 for c in chars.upper() if c in AA_SET) / len(chars)
    return aa_frac >= 0.95


def profile_df(df: pd.DataFrame) -> dict:
    cols = []
    for c in df.columns:
        s = df[c]
        dtype = None
        nu = int(s.nunique(dropna=True))
        info = {"name": str(c), "missing": int(s.isna().sum()), "n_unique": nu}
        # 数值列：非空值大多可转数值
        snum = pd.to_numeric(s, errors="coerce")
        nonmiss = int(s.notna().sum())
        if nonmiss > 0 and snum.notna().sum() / nonmiss >= 0.8:
            dtype = "numeric"
            fin = snum.dropna()
            info.update(min=float(fin.min()), max=float(fin.max()),
                        mean=float(fin.mean()), std=float(fin.std()))
            if nu <= 2 and set(fin.dropna().unique()) <= {0, 1}:
                dtype = "binary"
        elif pd.api.types.is_datetime64_any_dtype(s):
            dtype = "datetime"
        else:
            st = s.dropna().astype(str)
            if len(st) and _is_seqish(s):
                dtype = "sequence"
            elif len(st):
                import warnings
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore")
                    parsed = pd.to_datetime(st.head(50), errors="coerce")
                if parsed.notna().sum() / max(len(parsed), 1) >= 0.8 and \
                        st.head(50).str.contains(r"\d").all():
                    dtype = "datetime"
                else:
                    dtype = "categorical" if nu <= max(40, 0.12 * len(df)) else "text"
            else:
                dtype = "categorical"
        if dtype in ("categorical", "text", "sequence", "binary"):
            info["sample"] = [shorter(v) for v in s.dropna().astype(str).unique()[:3]]
        else:
            info["sample"] = [shorter(v) for v in s.dropna().unique()[:3]]
        info["dtype"] = dtype
        cols.append(info)
    return {"n_rows": int(len(df)), "n_cols": len(df.columns), "columns": cols}


def shorter(v, n=40):
    s = str(v)
    return s if len(s) <= n else s[: n - 1] + "…"


def cols_of(profile: dict, *types) -> list[str]:
    want = set(types)
    return [c["name"] for c in profile["columns"] if c["dtype"] in want]


def col_meta(profile: dict, name: str) -> dict | None:
    for c in profile["columns"]:
        if c["name"] == name:
            return c
    return None
