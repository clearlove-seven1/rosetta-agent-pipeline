/* BioPlot 前端逻辑（原生 JS，无依赖） */
"use strict";

const $ = (id) => document.getElementById(id);
const DTYPE_ZH = {
  numeric: "数值", categorical: "类别", binary: "0/1",
  datetime: "时间", sequence: "序列", text: "文本",
};

const state = {
  ds: null, profile: null,
  plots: [], catIds: [], cat: null, type: null,
  tab: "png", outputs: [], current: null,
};

const api = async (url, opts) => {
  const r = await fetch(url, opts);
  if (!r.ok) {
    let msg = `HTTP ${r.status}`;
    try { const j = await r.json(); msg = j.detail || msg; } catch { /* keep */ }
    throw new Error(msg);
  }
  return r.json();
};

/* 嵌入模式：被「个人总部」面板 iframe 内嵌时自动启用（也可 ?embed=1 手动指定），
   隐藏顶栏并套用总部配色；顶层窗口打开（新窗口打开按钮）仍是完整 UI。 */
if (new URLSearchParams(location.search).get("embed") === "1") {
  document.body.classList.add("embed");
} else if (window.self !== window.top) {
  document.body.classList.add("embed");
}

/* ---------------- 初始化 ---------------- */
init();
async function init() {
  bind();
  await Promise.all([loadHealth(), loadPlots(), refreshDatasets(), loadSamples()]);
  const first = state.catIds[0];
  if (first) setCat(first);
}

function bind() {
  const dz = $("dropzone"), fi = $("file-input");
  dz.addEventListener("click", () => fi.click());
  dz.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") { e.preventDefault(); fi.click(); }
  });
  ["dragover", "dragenter"].forEach((ev) =>
    dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.add("drag"); }));
  ["dragleave", "drop"].forEach((ev) =>
    dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.remove("drag"); }));
  dz.addEventListener("drop", (e) => {
    const f = e.dataTransfer.files && e.dataTransfer.files[0];
    if (f) uploadFile(f);
  });
  fi.addEventListener("change", () => { if (fi.files[0]) uploadFile(fi.files[0]); fi.value = ""; });

  $("btn-sample").addEventListener("click", loadSample);
  $("ds-select").addEventListener("change", () => selectDataset($("ds-select").value));
  $("btn-ds-del").addEventListener("click", deleteDataset);

  $("cat-tabs").addEventListener("click", (e) => {
    const b = e.target.closest(".cat"); if (b) setCat(b.dataset.cat);
  });
  $("plot-list").addEventListener("click", (e) => {
    const b = e.target.closest(".plot-item"); if (b) setType(b.dataset.id);
  });
  $("suggestions").addEventListener("click", (e) => {
    const b = e.target.closest(".sug"); if (b) setType(b.dataset.id);
  });

  $("tab-png").addEventListener("click", () => setTab("png"));
  $("tab-html").addEventListener("click", () => setTab("html"));
  $("btn-render").addEventListener("click", render);
  $("gallery").addEventListener("click", (e) => {
    const g = e.target.closest(".g-item");
    if (g && state.outputs[+g.dataset.idx]) showOutput(state.outputs[+g.dataset.idx]);
  });
}

/* ---------------- 数据加载 ---------------- */
async function loadHealth() {
  try {
    const j = await api("/api/health");
    const chip = $("health-chip");
    chip.textContent = `${j.plots} 种图就绪`;
    chip.classList.add("ok");
  } catch (e) {
    const chip = $("health-chip");
    chip.textContent = "服务异常";
    chip.classList.add("err");
  }
}

async function loadPlots() {
  const cats = await api("/api/plots");
  state.plots = cats.flatMap((c) => c.plots.map((p) => ({ ...p, cat: c.cat, cat_name: c.cat_name })));
  state.catIds = cats.map((c) => c.cat);
  window._catNames = Object.fromEntries(cats.map((c) => [c.cat, c.cat_name]));
  $("type-count").textContent = `${state.plots.length} 种`;
  const tabs = $("cat-tabs");
  tabs.innerHTML = cats.map((c) =>
    `<button class="cat" data-cat="${c.cat}" role="tab">${c.cat_name} · ${c.plots.length}</button>`).join("");
}

async function loadSamples() {
  const list = await api("/api/samples");
  $("sample-select").innerHTML =
    `<option value="">选择…</option>` +
    list.map((s) => `<option value="${s.id}">${s.name}</option>`).join("");
}

async function loadSample() {
  const sid = $("sample-select").value;
  if (!sid) return toast("先在下拉框里挑一个示例");
  try {
    onDataset(await api(`/api/samples/${sid}`, { method: "POST" }));
  } catch (e) { toast(e.message); }
}

async function uploadFile(f) {
  const fd = new FormData();
  fd.append("file", f);
  toast(`正在上传解析「${f.name}」…`, true, 8000);
  try {
    onDataset(await api("/api/upload", { method: "POST", body: fd }));
  } catch (e) { toast("上传失败：" + e.message, false, 8000); }
}

async function refreshDatasets() {
  let list = [];
  try { list = await api("/api/datasets"); } catch { /* ignore */ }
  const sel = $("ds-select");
  sel.innerHTML = list.length
    ? list.map((d) => `<option value="${d.id}">${d.name}（${d.n_rows}行）</option>`).join("")
    : `<option value="">暂无数据集</option>`;
  if (state.ds) sel.value = state.ds;
  return list;
}

async function selectDataset(dsid) {
  if (!dsid) return;
  try {
    const profile = await api(`/api/datasets/${dsid}/profile`);
    const meta = {
      id: dsid, profile,
      name: $("ds-select").selectedOptions[0]?.textContent || dsid,
      suggestions: profile.suggestions || [],
    };
    onDatasetCommon(meta);
  } catch (e) { toast(e.message); }
}

function onDataset(j) {
  refreshDatasets();
  onDatasetCommon(j);
}

function onDatasetCommon(j) {
  state.ds = j.id;
  state.profile = j.profile;
  renderDsMeta(j);
  if (j.suggestions) renderSuggestions(j.suggestions);
  loadHead(j.id);
  updateRenderBtn();
  if (state.type) prefillForm();
}

function renderDsMeta(j) {
  const p = j.profile;
  const stat = {};
  for (const c of p.columns) stat[c.dtype] = (stat[c.dtype] || 0) + 1;
  const statStr = Object.entries(stat).map(([k, v]) => `${DTYPE_ZH[k] || k}×${v}`).join(" · ");
  $("ds-meta").innerHTML =
    `<strong>${escapeHtml(j.name || "数据集")}</strong>　${p.n_rows} 行 × ${p.n_cols} 列<br>` +
    `<span class="muted">${statStr}</span>`;
}

function renderSuggestions(list) {
  const box = $("suggestions");
  if (!list || !list.length) { box.innerHTML = ""; return; }
  box.innerHTML =
    `<span class="chip subtle" style="align-self:center">推荐</span>` +
    list.map((s) => {
      const p = state.plots.find((x) => x.id === s.type);
      return `<button class="sug" data-id="${s.type}" title="${escapeHtml(s.reason)}">⭐ ${p ? p.name : s.type}<span class="why">${escapeHtml(truncate(s.reason, 26))}</span></button>`;
    }).join("");
}

async function loadHead(ds) {
  try {
    const j = await api(`/api/datasets/${ds}/head?rows=30`);
    const thead = $("ds-table").querySelector("thead");
    const tbody = $("ds-table").querySelector("tbody");
    thead.innerHTML = "<tr>" + j.columns.map((c) => `<th>${escapeHtml(c)}</th>`).join("") + "</tr>";
    tbody.innerHTML = j.rows.map((row) =>
      "<tr>" + j.columns.map((c) => {
        let v = row[c];
        if (v === null || v === undefined) v = "";
        if (typeof v === "number") v = Number.isInteger(v) ? v : +v.toFixed(4);
        return `<td>${escapeHtml(String(v).slice(0, 24))}</td>`;
      }).join("") + "</tr>").join("");
    $("ds-preview-wrap").open = false;
  } catch (e) { /* ignore */ }
}

/* ---------------- 图类型选择 ---------------- */
function setCat(catId) {
  state.cat = catId;
  document.querySelectorAll("#cat-tabs .cat").forEach((b) =>
    b.classList.toggle("on", b.dataset.cat === catId));
  renderPlotList();
}

function renderPlotList() {
  const items = state.plots.filter((p) => p.cat === state.cat);
  $("plot-list").innerHTML = items.map((p) =>
    `<button class="plot-item${state.type === p.id ? " on" : ""}" data-id="${p.id}" role="option">
      <span class="pi-name">${p.name}<span class="pi-eng">${p.id}</span></span>
      <span class="pi-desc">${escapeHtml(p.desc)}</span>
    </button>`).join("");
}

function setType(id) {
  const p = state.plots.find((x) => x.id === id);
  if (!p) return;
  state.type = id;
  if (p.cat !== state.cat) setCat(p.cat);
  document.querySelectorAll(".plot-item").forEach((b) =>
    b.classList.toggle("on", b.dataset.id === id));
  $("param-type-name").innerHTML = `<strong>${p.name}</strong> · ${window._catNames[p.cat] || ""}`;
  updateRenderBtn();
  prefillForm();
}

function updateRenderBtn() {
  $("btn-render").disabled = !(state.ds && state.type);
}

/* ---------------- 参数表单 ---------------- */
async function prefillForm() {
  if (!state.type || !state.ds) return;
  const pdef = state.plots.find((x) => x.id === state.type);
  $("param-form").innerHTML = `<p class="muted">正在按数据自动选列…</p>`;
  try {
    const j = await api("/api/prefill", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ds: state.ds, type: state.type, params: {} }),
    });
    buildParamForm(pdef, j.params);
  } catch (e) {
    buildParamForm(pdef, {});
    toast("预填失败，请手动选列：" + e.message);
  }
}

function buildParamForm(pdef, filled) {
  const form = $("param-form");
  form.innerHTML = "";
  const cols = (state.profile ? state.profile.columns : []) || [];
  for (const schema of pdef.params) {
    const wrap = document.createElement("div");
    wrap.className = "field";
    const lab = document.createElement("label");
    lab.textContent = schema.label + (schema.required ? " *" : "");
    lab.htmlFor = `pf-${schema.key}`;
    wrap.appendChild(lab);
    const v = filled[schema.key];

    if (schema.kind === "col") {
      const opts = cols.filter((c) => (schema.accept || []).includes(c.dtype));
      wrap.innerHTML += `<select id="pf-${schema.key}" data-key="${schema.key}">
        <option value="">（不使用）</option>
        ${opts.map((c) => `<option value="${escapeHtml(c.name)}" ${v === c.name ? "selected" : ""}>${escapeHtml(c.name)} · ${DTYPE_ZH[c.dtype] || c.dtype}</option>`).join("")}
      </select>`;
    } else if (schema.kind === "cols_multi") {
      const opts = cols.filter((c) => (schema.accept || []).includes(c.dtype));
      const chosen = Array.isArray(v) ? v : [];
      wrap.innerHTML += `<div class="chips" data-key="${schema.key}">
        ${opts.map((c) => `<button type="button" class="cchip${chosen.includes(c.name) ? " on" : ""}" data-key="${schema.key}" data-col="${escapeHtml(c.name)}" aria-pressed="${chosen.includes(c.name)}">${escapeHtml(c.name)}</button>`).join("")}
      </div>`;
    } else if (schema.kind === "number") {
      wrap.innerHTML += `<input id="pf-${schema.key}" data-key="${schema.key}" type="number" step="any" value="${v ?? ""}">`;
    } else if (schema.kind === "select") {
      wrap.innerHTML += `<select id="pf-${schema.key}" data-key="${schema.key}">
        ${(schema.options || []).map((o) => `<option value="${escapeHtml(o)}" ${v === o ? "selected" : ""}>${escapeHtml(o)}</option>`).join("")}
      </select>`;
    } else if (schema.kind === "bool") {
      wrap.innerHTML += `<label class="check"><input type="checkbox" data-key="${schema.key}" ${v ? "checked" : ""}> 是</label>`;
    }
    if (schema.hint) wrap.innerHTML += `<p class="hint">${escapeHtml(schema.hint)}</p>`;
    form.appendChild(wrap);
  }
  form.querySelectorAll(".cchip").forEach((chip) =>
    chip.addEventListener("click", () => {
      chip.classList.toggle("on");
      chip.setAttribute("aria-pressed", chip.classList.contains("on"));
    }));
  $("autofill-info") && ($("autofill-info").textContent = "");
}

function collectParams() {
  const pdef = state.plots.find((x) => x.id === state.type);
  const out = {};
  for (const schema of pdef.params) {
    if (schema.kind === "cols_multi") {
      const chips = document.querySelectorAll(`.cchip[data-key="${schema.key}"].on`);
      if (chips.length) out[schema.key] = Array.from(chips).map((c) => c.dataset.col);
    } else {
      const el = $("param-form").querySelector(`[data-key="${schema.key}"]`);
      if (!el) continue;
      if (schema.kind === "number") {
        if (el.value !== "") out[schema.key] = parseFloat(el.value);
      } else if (schema.kind === "bool") {
        out[schema.key] = el.checked;
      } else {
        if (el.value !== "") out[schema.key] = el.value;
      }
    }
  }
  return out;
}

/* ---------------- 渲染 ---------------- */
async function render() {
  if (!state.ds || !state.type) return toast("先加载数据并选择图类型");
  const btn = $("btn-render");
  btn.classList.add("loading");
  btn.disabled = true;
  $("empty-state").hidden = true;
  $("preview").hidden = false;
  $("pv-loading").hidden = false;
  const pdef = state.plots.find((x) => x.id === state.type);
  try {
    const body = {
      ds: state.ds, type: state.type,
      params: collectParams(),
      embed_js: $("embed-js").checked,
      title: $("pv-input-title").value.trim() || null,
    };
    const j = await api("/api/render", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const out = {
      png: j.png, html: j.html, insight: j.insight, warnings: j.warnings || [],
      type: state.type, name: pdef.name,
      title: body.title || pdef.name,
      sub: `${pdef.name}（${pdef.id}） · ${j.elapsed_ms} ms`,
    };
    state.outputs.unshift(out);
    renderGallery();
    showOutput(out, true);
    if ((j.warnings || []).length) toast(j.warnings.join("；"), false, 6000);
    else toast(`渲染完成 · ${j.elapsed_ms} ms`, true);
  } catch (e) {
    toast("渲染失败：" + e.message, false, 9000);
  } finally {
    btn.classList.remove("loading");
    btn.disabled = false;
    $("pv-loading").hidden = true;
    updateRenderBtn();
  }
}

function showOutput(out, preferPng) {
  state.current = out;
  $("empty-state").hidden = true;
  $("preview").hidden = false;
  $("pv-title").textContent = out.title;
  $("pv-sub").textContent = out.sub;
  $("dl-png").hidden = !out.png;
  $("dl-html").hidden = !out.html;
  $("open-new").href = out.html || out.png || "#";
  if (out.png) $("dl-png").href = out.png;
  if (out.html) { $("dl-html").href = out.html; $("open-new").href = out.html; }
  const ins = $("insight");
  ins.hidden = !out.insight;
  ins.textContent = out.insight || "";
  const wn = $("warnings");
  wn.hidden = !out.warnings.length;
  wn.textContent = out.warnings.join("；");
  setTab((preferPng && out.png) ? "png" : (out.html ? "html" : "png"));
}

function setTab(t) {
  const out = state.current;
  if (!out) return;
  if (t === "html" && !out.html) t = "png";
  if (t === "png" && !out.png) t = "html";
  state.tab = t;
  $("tab-png").classList.toggle("active", t === "png");
  $("tab-html").classList.toggle("active", t === "html");
  $("tab-png").setAttribute("aria-selected", t === "png");
  $("tab-html").setAttribute("aria-selected", t === "html");
  const img = $("pv-img"), fr = $("pv-frame");
  if (t === "png") {
    img.hidden = false; fr.hidden = true;
    if (img.dataset.src !== out.png) { img.src = out.png; img.dataset.src = out.png; }
  } else {
    img.hidden = true; fr.hidden = false;
    if (fr.dataset.src !== out.html) { fr.src = out.html; fr.dataset.src = out.html; }
  }
}

function renderGallery() {
  const g = $("gallery");
  if (!state.outputs.length) { g.innerHTML = `<p class="muted g-empty">还没有渲染过图。</p>`; return; }
  g.innerHTML = state.outputs.map((o, i) => `
    <div class="g-item" data-idx="${i}" title="${escapeHtml(o.title)}">
      ${o.png ? `<img src="${o.png}" alt="${escapeHtml(o.title)} 缩略图">` : `<div style="height:110px;display:grid;place-items:center;background:var(--surface-2);font-size:1.6rem">📈</div>`}
      <div class="gi-meta"><strong>${escapeHtml(o.title)}</strong><span>${escapeHtml(o.sub || "")}</span></div>
    </div>`).join("");
}

async function deleteDataset() {
  if (!state.ds) return toast("当前没有选中的数据集");
  if (!confirm("删除当前数据集及其产物？")) return;
  try {
    await api(`/api/datasets/${state.ds}`, { method: "DELETE" });
    state.ds = null; state.profile = null;
    $("ds-meta").textContent = "未加载数据";
    $("ds-table").querySelector("thead").innerHTML = "";
    $("ds-table").querySelector("tbody").innerHTML = "";
    toast("已删除", true);
    refreshDatasets();
  } catch (e) { toast(e.message); }
}

/* ---------------- 工具 ---------------- */
let toastTimer = null;
function toast(msg, ok = false, ms = 4000) {
  const t = $("toast");
  t.textContent = msg;
  t.classList.toggle("ok", !!ok);
  t.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.remove("show"), ms);
}

function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function truncate(s, n) {
  s = String(s || "");
  return s.length <= n ? s : s.slice(0, n - 1) + "…";
}
