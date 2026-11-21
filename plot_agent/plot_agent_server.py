"""BioPlot 作图智能体：独立 FastAPI 服务（计算生物学自动作图）。

  POST /api/upload                上传 CSV/TSV/XLSX → 剖析 + 智能推荐
  GET  /api/datasets              已上传数据集列表
  POST /api/samples/{sid}         加载内置示例数据
  GET  /api/plots                 图类型注册表（7 大类 35 种）
  POST /api/render                渲染指定图类型 → PNG + 交互 HTML + 自动解读
  GET  /api/datasets/{id}/head    数据预览（前 N 行）
"""
import io
import json
import os
import re
import sys
import time
import uuid
import shutil
import traceback
from typing import Optional

import pandas as pd
from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
os.chdir(BASE)

import data_profiler as dp
import plot_registry as reg
import samples as S

UPLOADS = os.path.join(BASE, "uploads")
OUTPUTS = os.path.join(BASE, "outputs")
os.makedirs(UPLOADS, exist_ok=True)
os.makedirs(OUTPUTS, exist_ok=True)
MAX_UPLOAD = 60 * 1024 * 1024  # 60MB
ALLOWED_EXT = {".csv", ".tsv", ".txt", ".xlsx", ".xlsm", ".xls"}
DS_RE = re.compile(r"^ds[a-z0-9]{6,24}$")

app = FastAPI(title="BioPlot 作图智能体")
_DF_CACHE = {}


def _ds_ok(dsid: str) -> bool:
    if not DS_RE.match(dsid):
        raise HTTPException(400, "非法数据集 ID")
    return True


def _csv_path(dsid):
    return os.path.join(UPLOADS, dsid + ".csv")


def _meta_path(dsid):
    return os.path.join(UPLOADS, dsid + ".meta.json")


def _profile_path(dsid):
    return os.path.join(UPLOADS, dsid + ".profile.json")


def load_df(dsid: str) -> pd.DataFrame:
    _ds_ok(dsid)
    path = _csv_path(dsid)
    if not os.path.exists(path):
        raise HTTPException(404, "数据集不存在")
    mt = os.path.getmtime(path)
    hit = _DF_CACHE.get(dsid)
    if hit and hit[0] == mt:
        return hit[1]
    df = dp.load_table(path)
    if len(_DF_CACHE) > 12:
        _DF_CACHE.clear()
    _DF_CACHE[dsid] = (mt, df)
    return df


def load_profile(dsid: str) -> dict:
    path = _profile_path(dsid)
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    df = load_df(dsid)
    prof = dp.profile_df(df)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(prof, f, ensure_ascii=False)
    return prof


def save_dataset(df: pd.DataFrame, name: str, source: str) -> dict:
    dsid = "ds" + uuid.uuid4().hex[:10]
    path = _csv_path(dsid)
    df.to_csv(path, index=False, encoding="utf-8-sig")
    meta = {"id": dsid, "name": name[:60], "source": source,
            "n_rows": int(len(df)), "n_cols": int(df.shape[1]),
            "created": time.strftime("%Y-%m-%d %H:%M:%S")}
    with open(_meta_path(dsid), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False)
    prof = dp.profile_df(df)
    prof["suggestions"] = reg.suggest_plots(prof)
    with open(_profile_path(dsid), "w", encoding="utf-8") as f:
        json.dump(prof, f, ensure_ascii=False)
    out = dict(meta)
    out["profile"] = prof
    out["suggestions"] = prof["suggestions"]
    return out


class RenderBody(BaseModel):
    ds: str
    type: str
    params: dict = {}
    embed_js: bool = False
    title: Optional[str] = None


@app.get("/api/health")
def health():
    return {"ok": True, "plots": len(reg.PLOTS), "font": __import__("common").CJK_FONT}


@app.get("/api/plots")
def plots():
    return reg.plot_meta()


@app.get("/api/samples")
def sample_list():
    return [{"id": k, "name": v[0], "desc": v[1]} for k, v in S.SAMPLES.items()]


@app.post("/api/samples/{sid}")
def load_sample(sid: str):
    if sid not in S.SAMPLES:
        raise HTTPException(404, "未知示例")
    name, desc, fn = S.SAMPLES[sid]
    try:
        df = fn()
    except Exception as e:
        raise HTTPException(500, f"示例生成失败: {e}")
    return save_dataset(df, f"示例·{name}", "sample")


@app.post("/api/upload")
async def upload(file: UploadFile = File(...), name: str = ""):
    ext = os.path.splitext(file.filename or "")[1].lower()
    if ext not in ALLOWED_EXT:
        raise HTTPException(400, f"不支持的格式 {ext}（支持 csv/tsv/txt/xlsx）")
    tmp = os.path.join(UPLOADS, "tmp_" + uuid.uuid4().hex + ext)
    size = 0
    try:
        with open(tmp, "wb") as f:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_UPLOAD:
                    raise HTTPException(413, "文件超过 60MB 上限")
                f.write(chunk)
        df = dp.load_table(tmp)
        if df.empty or df.shape[1] == 0:
            raise HTTPException(400, "表格为空或无法解析出列")
        disp = (name or os.path.splitext(os.path.basename(file.filename))[0])[:60]
        return save_dataset(df, disp, "upload")
    except HTTPException:
        raise
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(400, f"解析失败：{e}")
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


@app.get("/api/datasets")
def datasets():
    out = []
    for fn in sorted(os.listdir(UPLOADS)):
        if fn.endswith(".meta.json"):
            try:
                with open(os.path.join(UPLOADS, fn), encoding="utf-8") as f:
                    meta = json.load(f)
                out.append({k: meta[k] for k in ("id", "name", "source", "n_rows", "n_cols", "created")})
            except Exception:
                continue
    out.sort(key=lambda m: m["created"], reverse=True)
    return out


@app.delete("/api/datasets/{dsid}")
def delete_dataset(dsid: str):
    _ds_ok(dsid)
    removed = 0
    for suf in (".csv", ".meta.json", ".profile.json"):
        p = os.path.join(UPLOADS, dsid + suf)
        if os.path.exists(p):
            os.remove(p)
            removed += 1
    out = os.path.join(OUTPUTS, dsid)
    if os.path.isdir(out):
        shutil.rmtree(out, ignore_errors=True)
    _DF_CACHE.pop(dsid, None)
    return {"removed": removed}


@app.get("/api/datasets/{dsid}/head")
def dataset_head(dsid: str, rows: int = 25):
    _ds_ok(dsid)
    rows = max(1, min(rows, 60))
    df = load_df(dsid).head(rows)
    return {"columns": [str(c) for c in df.columns],
            "rows": json.loads(df.to_json(orient="records", force_ascii=False))}


@app.post("/api/prefill")
def prefill(body: RenderBody):
    """按数据集 profile 返回某图类型的自动填参结果（供参数表单预填）。"""
    _ds_ok(body.ds)
    if body.type not in reg.PLOTS:
        raise HTTPException(400, f"未知图类型 {body.type}")
    profile = load_profile(body.ds)
    return {"params": reg.autofill(profile, body.type, {})}


@app.get("/api/datasets/{dsid}/profile")
def dataset_profile(dsid: str):
    _ds_ok(dsid)
    prof = load_profile(dsid)
    if "suggestions" not in prof:  # 旧数据集即时补算
        prof["suggestions"] = reg.suggest_plots(prof)
    return prof


@app.post("/api/render")
def render(body: RenderBody):
    _ds_ok(body.ds)
    pid = body.type
    if pid not in reg.PLOTS:
        raise HTTPException(400, f"未知图类型 {pid}")
    try:
        df = load_df(body.ds)
        profile = load_profile(body.ds)
    except HTTPException:
        raise
    params = dict(body.params or {})
    if body.title:
        params["_title"] = body.title
    t0 = time.time()
    try:
        png_bytes, html_str, insight, warnings, P = reg.render_plot(
            df, profile, pid, params, embed_js=body.embed_js)
    except HTTPException:
        raise
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(422, f"渲染失败：{e}")
    out_dir = os.path.join(OUTPUTS, body.ds)
    os.makedirs(out_dir, exist_ok=True)
    stamp = f"{int(time.time())}{uuid.uuid4().hex[:4]}"
    png_url = html_url = None
    if png_bytes:
        fname = f"{pid}_{stamp}.png"
        with open(os.path.join(out_dir, fname), "wb") as f:
            f.write(png_bytes)
        png_url = f"/outputs/{body.ds}/{fname}"
    if html_str:
        fname = f"{pid}_{stamp}.html"
        with open(os.path.join(out_dir, fname), "w", encoding="utf-8") as f:
            f.write(html_str)
        html_url = f"/outputs/{body.ds}/{fname}"
    if not png_url and not html_url:
        raise HTTPException(422, "该图类型两种引擎均未产出")
    return {"png": png_url, "html": html_url, "insight": insight,
            "warnings": warnings, "params_used": P,
            "elapsed_ms": int((time.time() - t0) * 1000)}


@app.exception_handler(500)
async def err500(request, exc):
    return JSONResponse(status_code=500, content={"detail": "服务器内部错误，详见 plot_agent.log"})


static_dir = os.path.join(BASE, "static")
app.mount("/static", StaticFiles(directory=static_dir), name="static")
app.mount("/outputs", StaticFiles(directory=OUTPUTS), name="outputs")


@app.get("/")
def index():
    return FileResponse(os.path.join(static_dir, "index.html"), headers={"Cache-Control": "no-cache"})


if __name__ == "__main__":
    import argparse
    import uvicorn
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=7796)
    ap.add_argument("--host", default="0.0.0.0")
    args = ap.parse_args()
    uvicorn.run(app, host=args.host, port=args.port)
