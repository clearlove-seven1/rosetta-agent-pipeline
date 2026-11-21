/* Rosetta Agent 前端逻辑：任务提交 + SSE 对话流 + 三步进度条 + 链选择
   + grill-me 对话式建档 + PyMOL 结构可视化（5 种发表级样式与位点定位） */
"use strict";

const $ = (id) => document.getElementById(id);

/* ---------- 工具 → 中文名 ---------- */
const TOOL_LABEL = {
  parameterize_ligand: "配体参数化（PyMOL + molfile_to_params）",
  run_cartesian_relax: "复合物笛卡尔弛豫（多构象并发）",
  run_saturation_mutagenesis: "饱和突变 ddG 计算（后台多核）",
  progress_poll_status: "突变计算进度查询",
  get_rosetta_numbering: "编号映射核对（只读）",
  diagnose_and_fix: "报错诊断修复",
  auto_repair: "自动修复",
  check_logs: "日志检查",
  read_file: "文件读取",
  search_rosetta_docs: "知识库检索",
  list_files: "产物清单",
};

/* 服务器资源（/api/system），给并发提示与 intake 用 */
let SYS = { cpu_cores: 0, jobs_default: 10, delay_default: 1.5 };

const thread = $("thread");
let currentTask = null;
let es = null;
let doneState = true;
const cards = {};

/* ---------- 聊天渲染 ---------- */
function el(html) {
  const t = document.createElement("template");
  t.innerHTML = html.trim();
  return t.content.firstChild;
}
function scrollBottom() { thread.scrollTop = thread.scrollHeight; }
function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
}
/* 位点 token → 可点击 chip（点击联动右侧可视化定位） */
function siteChip(chain, resi, label) {
  return `<span class="site-chip" data-chain="${chain}" data-resi="${resi}" title="点击在结构中定位">${label}</span>`;
}
function linkifySites(raw) {
  let s = escapeHtml(raw);
  // ddg 结果名：D_A76_A → 链 A 残基 76
  s = s.replace(/\b([A-Za-z]{1,2})_([A-Z])(\d{1,5})_([A-Z])\b/g,
    (m, wt, ch, resi) => siteChip(ch, resi, m));
  // A_76 形式
  s = s.replace(/\b([A-Ha-h])_(\d{1,5}[A-Za-z]?)\b/g,
    (m, ch, resi) => siteChip(ch.toUpperCase(), resi, m));
  return s;
}
function addAgentText(text) {
  const div = el(`<div class="chat-msg agent-plain"></div>`);
  div.innerHTML = linkifySites(text);
  thread.appendChild(div);
  scrollBottom();
}
function addWarn(text) {
  thread.appendChild(el(`<div class="warnline">⚠ ${escapeHtml(text)}</div>`));
  scrollBottom();
}
function addTyping(label) {
  removeTyping();
  thread.appendChild(el(`<div class="chat-msg" id="typing"><div class="typing"><i></i><i></i><i></i><span>${escapeHtml(label)}</span></div></div>`));
  scrollBottom();
}
function removeTyping() { const t = $("typing"); if (t) t.remove(); }

function addToolCard(seq, tool) {
  const label = TOOL_LABEL[tool] || tool;
  const card = el(`<div class="toolcard run" id="tc-${seq}">
    <div class="tc-head"><span class="ic">●</span><span class="tc-name">${escapeHtml(label)}</span>
    <span class="tc-state">运行中</span></div></div>`);
  removeTyping();
  thread.appendChild(card);
  scrollBottom();
  return card;
}
function finishToolCard(seq, ok, detail) {
  const card = $("tc-" + seq);
  if (!card) return;
  card.className = `toolcard ${ok ? "ok" : "bad"}`;
  card.querySelector(".tc-head .ic").textContent = ok ? "✓" : "✕";
  card.querySelector(".tc-state").textContent = ok ? "完成" : "报错";
  if (detail) {
    const body = el(`<div class="tc-body"></div>`);
    body.textContent = detail.length > 320 ? detail.slice(0, 320) + " …" : detail;
    card.appendChild(body);
  }
  scrollBottom();
}
function addDone(ok, extra) {
  removeTyping();
  const bar = el(`<div class="donebar ${ok ? "" : "bad"}">
    <b>${ok ? "✓ 三段流水线执行完成" : "✕ 任务异常终止"}</b>
    <span class="dim">${escapeHtml(extra || "")}</span>
    ${ok ? `<a href="/api/tasks/${currentTask}/download">打包下载 (.zip)</a>` : ""}</div>`);
  thread.appendChild(bar);
  scrollBottom();
}
function setUserMsg(text, chips) {
  const chipHtml = (chips || []).map(c => `· ${escapeHtml(c)}`).join("  ");
  thread.appendChild(el(`<div class="chat-msg"><div class="who">用户</div>
    <div style="color:var(--ink-2)">${escapeHtml(text)}${chipHtml ? `<div style="margin-top:4px;font-size:11.5px;color:var(--ink-3)">${chipHtml}</div>` : ""}</div></div>`));
  scrollBottom();
}

function setChatStatus(text, cls) {
  $("chat-status").textContent = text;
  $("chat-status").style.background = cls === "run" ? "rgba(217,164,65,.35)"
    : cls === "ok" ? "rgba(61,220,151,.4)" : cls === "bad" ? "rgba(255,122,110,.45)"
    : "rgba(255,255,255,.14)";
}
function setComposerEnabled(on) {
  $("follow-input").disabled = !on;
  $("follow-btn").disabled = !on;
  $("follow-input").placeholder = on
    ? "对智能体说点什么：无任务时=对话创建任务（如「跑一个突变」）；任务完成后=继续对话"
    : "任务运行中，完成后可继续对话";
}

/* ---------- 三步流水线进度条（SSE type=progress，服务端从任务目录推断） ---------- */
function renderProgress(steps) {
  $("pp").hidden = false;
  for (const key of ["p1", "p2", "p3"]) {
    const s = (steps && steps[key]) || { pct: 0, state: "wait", detail: "未开始" };
    const row = $("pp-" + key);
    if (!row) continue;
    row.className = "pp-step " + (s.state || "wait");
    row.querySelector(".pp-bar i").style.width = (s.pct || 0) + "%";
    row.querySelector(".pp-detail").textContent = s.detail || "";
  }
}

/* ---------- SSE 订阅 ---------- */
function subscribe(taskId) {
  if (es) es.close();
  currentTask = taskId;
  Object.keys(cards).forEach(k => delete cards[k]);
  doneState = false;
  let seq = 0;

  es = new EventSource(`/api/tasks/${taskId}/events`);
  es.onmessage = (ev) => {
    let data;
    try { data = JSON.parse(ev.data); } catch { return; }
    switch (data.type) {
      case "tool_start": {
        const card = addToolCard(++seq, data.name);
        cards[seq] = { name: data.name, el: card };
        addTyping(`${TOOL_LABEL[data.name] || data.name} 执行中…`);
        setChatStatus("运行中", "run");
        break;
      }
      case "tool_end": {
        const key = Object.keys(cards).reverse().find(k =>
          cards[k].name === data.name && cards[k].el.classList.contains("run"));
        if (key) finishToolCard(parseInt(key), data.ok, data.detail);
        if (data.ok) addTyping("Agent 决策中…");
        else addWarn(`${TOOL_LABEL[data.name] || data.name} 报错，触发自动诊断修复流程`);
        break;
      }
      case "progress":
        renderProgress(data.steps);
        break;
      case "agent":
        removeTyping();
        addAgentText(data.text);
        break;
      case "done":
        removeTyping();
        doneState = true;
        if (data.ok) { setChatStatus("已完成", "ok"); addDone(true, taskId); loadInterpretation(taskId); }
        else { setChatStatus("异常终止", "bad"); addDone(false, data.error); }
        setComposerEnabled(true);
        loadHistory();
        break;
      case "stream_end":
        if (es) { es.close(); es = null; }
        break;
    }
  };
  setComposerEnabled(false);
  setChatStatus("连接中…", "run");
  addTyping("正在接入任务事件流…");
}

/* ---------- 文件选择 ---------- */
$("pdb-input").addEventListener("change", () => {
  const f = $("pdb-input").files[0];
  $("pdb-name").textContent = f ? `${f.name}（${(f.size / 1024).toFixed(1)} KB）` : "未选择文件";
  // 表单文件也可作为可视化来源：自动渲染整体结构预览
  if (f && !$("viz-source").value) loadStructure();
});
$("ligand-input").addEventListener("change", () => {
  const f = $("ligand-input").files[0];
  $("ligand-name").textContent = f ? `${f.name}（${(f.size / 1024).toFixed(1)} KB）` : "未选择文件";
});

/* ---------- 提交任务 ---------- */
/* 按链选择组装位点串：选了链且未手写前缀时补 "A_76" */
function buildMutation() {
  const raw = $("mutation").value.trim().replace(/，/g, ",");
  const chain = $("chain-sel").value;
  if (!chain) return raw;
  return raw.split(",").map(t => {
    t = t.trim();
    if (!t) return "";
    if (/^[A-Za-z]{1,3}_/.test(t)) return t;   // 已带链前缀
    return `${chain}_${t}`;
  }).filter(Boolean).join(",");
}

$("task-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = $("pdb-input").files[0];
  if (!f) { alert("请先选择蛋白复合物 PDB 文件"); return; }
  const mut = buildMutation();
  if (!mut) { alert("请填写目标突变位点（PDB 晶体学编号）"); return; }
  if (es) es.close();
  submitBtnLoading(true);
  intake.active = false;

  const jobs = $("jobs").value.trim();
  const delay = $("delay").value.trim();
  const extra = $("params").value.trim();
  const paramsText = [
    jobs ? `并发数 ${jobs}` : "",
    delay ? `延迟 ${delay}` : "",
    extra,
  ].filter(Boolean).join(", ");

  const fd = new FormData();
  fd.append("file", f);
  const lig = $("ligand-input").files[0];
  if (lig) fd.append("ligand", lig);
  fd.append("mutation", mut);
  fd.append("params", paramsText);
  fd.append("msg", $("msg").value.trim());

  try {
    const res = await fetch("/api/tasks", { method: "POST", body: fd });
    if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || `HTTP ${res.status}`);
    const data = await res.json();
    thread.innerHTML = "";
    setUserMsg("提交了新的饱和突变计算任务，请开始执行三段流水线。",
      [f.name, `位点 ${mut}`,
       lig ? `配体 ${lig.name}` : "配体自动分离",
       paramsText ? `参数 ${paramsText}` : null].filter(Boolean));
    renderProgress(null);   // 重置进度条
    $("pp").hidden = false;
    subscribe(data.task_id);
    loadHistory();
  } catch (err) {
    thread.innerHTML = "";
    addWarn(`任务提交失败：${err.message}`);
  } finally {
    submitBtnLoading(false);
  }
});

function submitBtnLoading(on) {
  $("submit-btn").disabled = on;
  $("submit-btn").classList.toggle("loading", on);
  $("submit-btn").querySelector(".btn-label").textContent = on ? "任务提交中…" : "开始突变计算";
}

/* ---------- grill-me 对话式建档（无任务时在对话里逐项问清关键参数） ---------- */
const intake = { active: false, step: "idle", draft: {} };

function fileAttached() { return !!$("pdb-input").files[0]; }

function parseMutationText(t) {
  const parts = String(t).replace(/，/g, ",").split(",").map(x => x.trim()).filter(Boolean);
  const out = [];
  for (const p of parts) {
    if (/^\d{1,5}[A-Za-z]?$/.test(p)) out.push(p);
    else if (/^[A-Za-z]{1,3}_\d{1,5}[A-Za-z]?$/.test(p)) out.push(p);
  }
  return out.join(",");
}
function parseNum(t, dft) {
  const m = String(t).match(/[\d.]+/);
  if (!m) return dft;
  const v = parseFloat(m[0]);
  return isNaN(v) ? dft : v;
}

function intakeBegin(seedText) {
  intake.active = true;
  intake.step = "idle";
  intake.draft = { mutation: "", chain: null, jobs: null, delay: null, msg: null };
  addAgentText("好的，我来带你创建突变任务：像 checklist 一样逐项确认关键参数，全部齐了以后由你点「确认提交」才真正执行。（随时发「取消」退出）");
  if (seedText) intakeHandle(seedText); else intakeNext();
}

function intakeAsk(text) { addAgentText(text); }

function intakeNext() {
  const d = intake.draft;
  if (!fileAttached()) {
    intake.step = "file";
    intakeAsk("① 请先在左侧表单选择蛋白复合物 PDB 文件（必需）。选好后在这里回复「好了」。");
    return;
  }
  if (!d.mutation) {
    intake.step = "mutation";
    intakeAsk("② 要突变哪些位点？填 PDB 晶体学编号，多个用逗号分隔，如 76,73（也可直接写 A_76 带链号）。");
    return;
  }
  const allPrefixed = d.mutation.split(",").every(x => /^[A-Za-z]{1,3}_/.test(x.trim()));
  if (d.chain === null && !allPrefixed) {
    intake.step = "chain";
    intakeAsk("③ 这些位点在哪条链？回复 A–H 单个字母；不确定或多链同号才需要区分时选「自动」。");
    return;
  }
  if (d.chain === null) d.chain = "";   // 全部带前缀 → 视为已答
  if (d.jobs === null) {
    intake.step = "jobs";
    intakeAsk(`④ 并发数（同时运行的 Rosetta 进程数）？服务器共 ${SYS.cpu_cores} 逻辑核、与他人共享，建议 ≤ 48；回车或「默认」= ${SYS.jobs_default}。`);
    return;
  }
  if (d.delay === null) {
    intake.step = "delay";
    intakeAsk(`⑤ 延迟秒数（每个突变计算提交后的错峰间隔，防止 位点×19 个进程瞬时拉起）？回车或「默认」= ${SYS.delay_default}s。`);
    return;
  }
  if (d.msg === null) {
    intake.step = "msg";
    intakeAsk("⑥ 其他要求？（如：口袋周围位点优先。回车或「无」跳过。）");
    return;
  }
  intakeConfirm();
}

function intakeHandle(text) {
  const t = (text || "").trim();
  if (/^(取消|退出|算了)$/.test(t)) {
    intake.active = false; intake.step = "idle";
    addAgentText("已取消任务创建。可随时再发消息重新开始。");
    return;
  }
  const d = intake.draft;
  switch (intake.step) {
    case "idle": {
      const nums = t.match(/\d{1,5}/g);
      if (nums && nums.length) {
        d.mutation = parseMutationText(nums.join(","));
        addAgentText(`从你的话里解析到位点：${d.mutation}（稍后可修改）。`);
      }
      intakeNext();
      return;
    }
    case "file":
      if (/^(好了?|完成|已选|ok|yes|是)/i.test(t)) intakeNext();
      else intakeAsk("还没检测到文件：请在左侧「蛋白复合物结构」处选择 .pdb 文件，然后回复「好了」。");
      return;
    case "mutation": {
      const m = parseMutationText(t);
      if (!m) { intakeAsk("没解析到位点：请填数字编号（如 76,73 或 A_76）。"); return; }
      d.mutation = m;
      intakeNext();
      return;
    }
    case "chain": {
      if (/^自?动|^不指定|^all$/i.test(t)) d.chain = "";
      else {
        const m = t.toUpperCase().match(/[A-H]/);
        if (!m) { intakeAsk("请回复 A–H 之间的单个链号，或「自动」。"); return; }
        d.chain = m[0];
      }
      intakeNext();
      return;
    }
    case "jobs":
      d.jobs = parseNum(t, SYS.jobs_default);
      intakeNext();
      return;
    case "delay":
      d.delay = parseNum(t, SYS.delay_default);
      intakeNext();
      return;
    case "msg":
      d.msg = (/^(无|跳过|没有|不需要|-|。)$/.test(t)) ? "" : t;
      intakeNext();
      return;
  }
}

function intakeConfirm() {
  const d = intake.draft;
  const mut = d.chain
    ? d.mutation.split(",").map(x => /^[A-Za-z]{1,3}_/.test(x.trim()) ? x.trim() : `${d.chain}_${x.trim()}`).join(",")
    : d.mutation;
  const fname = fileAttached() ? $("pdb-input").files[0].name : "（未选择！）";
  const card = el(`<div class="confirm-card">
    <b>📋 任务确认单</b>
    <div>蛋白：${escapeHtml(fname)}</div>
    <div>位点：${escapeHtml(mut)}</div>
    <div>并发：${escapeHtml(String(d.jobs))}　延迟：${escapeHtml(String(d.delay))}s</div>
    ${d.msg ? `<div>附加：${escapeHtml(d.msg)}</div>` : ""}
    <div class="confirm-actions">
      <button type="button" class="btn-send" id="intake-go">✅ 确认提交</button>
      <button type="button" class="btn-cancel" id="intake-no">取消</button>
    </div></div>`);
  removeTyping();
  thread.appendChild(card);
  scrollBottom();
  card.querySelector("#intake-no").onclick = () => {
    card.remove();
    intake.active = false;
    addAgentText("已取消。需要时再发消息即可重新开始。");
  };
  card.querySelector("#intake-go").onclick = () => {
    if (!fileAttached()) { addWarn("还未选择 PDB 文件：请先在左侧表单选择后再点确认。"); return; }
    $("mutation").value = mut;
    $("chain-sel").value = d.chain || "";
    $("jobs").value = d.jobs;
    $("delay").value = d.delay;
    $("params").value = d.msg || "";
    intake.active = false;
    addAgentText("参数已写入左侧表单，开始提交任务…");
    if ($("task-form").requestSubmit) $("task-form").requestSubmit();
    else $("task-form").dispatchEvent(new Event("submit", { cancelable: true }));
  };
}

/* ---------- 继续对话 / 对话建档 ---------- */
async function sendFollow() {
  const input = $("follow-input");
  const text = input.value.trim();
  if (!text || !doneState) return;
  input.value = "";

  /* 无任务 → 对话式建档（grill-me）；有任务 → 任务续聊 */
  if (!currentTask) {
    setUserMsg(text);
    addTyping("Agent 处理中…");
    setChatStatus("运行中", "run");
    removeTyping();
    if (intake.active) intakeHandle(text);
    else intakeBegin(text);
    return;
  }

  setUserMsg(text);
  addTyping("Agent 处理中…");
  setComposerEnabled(false);
  setChatStatus("运行中", "run");
  try {
    const res = await fetch(`/api/tasks/${currentTask}/message`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message: text }),
    });
    if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || `HTTP ${res.status}`);
    subscribe(currentTask);
  } catch (err) {
    removeTyping();
    addWarn(`发送失败：${err.message}`);
    setComposerEnabled(true);
  }
}
$("follow-btn").addEventListener("click", sendFollow);
$("follow-input").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); sendFollow(); }
});

/* ---------- 交互式 3D 结构视图（3Dmol WebGL）+ PyMOL 发表图 ---------- */
function setVizStatus(text, cls) {
  $("viz-status").textContent = text;
  $("viz-status").style.background = cls === "run" ? "rgba(217,164,65,.35)"
    : cls === "ok" ? "rgba(61,220,151,.4)" : cls === "bad" ? "rgba(255,122,110,.45)"
    : "rgba(255,255,255,.14)";
}

let glViewer = null;
let glPdbText = "";
let glSpin = false;
let lastSite = null;

function ensureViewer() {
  if (glViewer) return glViewer;
  const el = $("viz-canvas");
  glViewer = $3Dmol.createViewer(el, { backgroundColor: "#ffffff" });
  window.glViewer = glViewer; /* 便于控制台调试 */
  $("viz-empty").hidden = true;
  return glViewer;
}

/* 按当前表示方法/配色重画（ssPyMOL=二级结构配色、chain=按链、spectrum=彩虹；teal 为单色青） */
function applyRep(keepFocus) {
  const v = glViewer;
  if (!v || !glPdbText) return;
  const rep = $("viz-rep").value;
  const col = $("viz-color").value;
  v.clear();
  v.addModel(glPdbText, "pdb");
  const mk = (o) => col === "teal"
    ? Object.assign(o, { color: "#0e7c86" })
    : Object.assign(o, { colorscheme: col });
  if (rep === "cartoon") v.setStyle({}, { cartoon: mk({}) });
  else if (rep === "cartoon_surf") {
    v.setStyle({}, { cartoon: mk({}) });
    v.addSurface($3Dmol.SurfaceType.VDW, { opacity: 0.55, colorscheme: col });
  } else if (rep === "stick") v.setStyle({}, { stick: mk({ radius: 0.25 }) });
  else if (rep === "line") v.setStyle({}, { line: mk({}) });
  else if (rep === "sphere") v.setStyle({}, { sphere: mk({}) });
  if (keepFocus && lastSite) focusSite(lastSite.chain, lastSite.resi, true);
  else v.zoomTo();
  v.render();
}

/* 位点定位：黄色棍棒 + 标注 + 镜头聚焦（等价 PyMOL 的 zoom site） */
function focusSite(chain, resi, silent) {
  const v = glViewer;
  if (!v || !glPdbText) { if (!silent) setVizStatus("请先加载结构（选来源或左侧选 PDB）", "bad"); return; }
  const r = parseInt(resi, 10);
  if (!chain || isNaN(r)) { if (!silent) setVizStatus("位点格式：链 + 残基号（如 A / 76）", "bad"); return; }
  const sel = { chain: chain, resi: r };
  v.addStyle(sel, { stick: { color: "#F5D020", radius: 0.35 } });
  v.addLabel(chain + "_" + resi, { fontSize: 13, fontColor: "#ffffff",
    backgroundColor: "rgba(15,18,26,0.85)", borderThickness: 0, showBackground: true }, sel);
  v.zoomTo(sel, 800);
  v.render();
  lastSite = { chain: chain, resi: resi };
  if (!silent) setVizStatus("已定位 " + chain + "_" + resi, "ok");
}

function resetView() {
  if (!glViewer) return;
  lastSite = null;
  applyRep(false);
  setVizStatus("已复位", "");
}

function toggleSpin() {
  if (!glViewer) return;
  glSpin = !glSpin;
  glViewer.spin(glSpin ? "y" : false);
  $("viz-spin").textContent = glSpin ? "自旋:开" : "自旋";
}

/* 加载结构：表单文件（本地读取）或历史任务（/api/structure 返回 PDB 文本） */
async function loadStructure() {
  const src = $("viz-source").value;
  const f = $("pdb-input").files[0];
  if (!src && !f) { setVizStatus("请先选 PDB 或历史任务", "bad"); return; }
  setVizStatus("加载中…", "run");
  try {
    let text, label;
    if (src) {
      const res = await fetch(`/api/structure?task_id=${encodeURIComponent(src)}`);
      if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || `HTTP ${res.status}`);
      text = await res.text();
      label = src;
    } else {
      text = await f.text();
      label = f.name;
    }
    glPdbText = text;
    ensureViewer();
    applyRep(false);
    setVizStatus("✓ 已加载 " + label + " — 左键旋转 · 右键平移 · 滚轮缩放", "ok");
  } catch (e) {
    setVizStatus("加载失败：" + e.message, "bad");
  }
}

/* PyMOL 发表图：服务器光追渲染（5 种发表级样式出高清静态图） */
async function pubRender() {
  const src = $("viz-source").value;
  const f = $("pdb-input").files[0];
  if (!src && !f) { setVizStatus("请先选 PDB 或历史任务", "bad"); return; }
  setVizStatus("光追渲染中（可达数十秒）…", "run");
  try {
    const fd = new FormData();
    if (src) fd.append("task_id", src); else fd.append("file", f);
    fd.append("chain", $("viz-chain").value);
    fd.append("resi", $("viz-resi").value.trim());
    fd.append("style", "metal");
    fd.append("mode", "ray");
    const res = await fetch("/api/viz/render", { method: "POST", body: fd });
    const data = await res.json();
    if (!data.ok) throw new Error(data.error || "渲染失败");
    $("viz-ray-img").src = data.image;
    $("viz-ray-dl").href = data.image;
    $("viz-ray-out").hidden = false;
    setVizStatus("✓ 发表图 " + data.elapsed_s + "s" + (data.site ? " · " + data.site : ""), "ok");
  } catch (e) {
    setVizStatus("发表图失败：" + e.message, "bad");
  }
}

function savePng() {
  if (!glViewer) { setVizStatus("尚未加载结构", "bad"); return; }
  const a = document.createElement("a");
  a.href = glViewer.pngURI();
  a.download = "structure_view.png";
  a.click();
  setVizStatus("已保存交互视图 PNG", "ok");
}

$("viz-source").addEventListener("change", loadStructure);
$("viz-rep").addEventListener("change", () => applyRep(!!lastSite));
$("viz-color").addEventListener("change", () => applyRep(!!lastSite));
$("viz-btn").addEventListener("click", () => focusSite($("viz-chain").value, $("viz-resi").value.trim()));
$("viz-resi").addEventListener("keydown", (e) => {
  if (e.key === "Enter") { e.preventDefault(); focusSite($("viz-chain").value, $("viz-resi").value.trim()); }
});
$("viz-clear").addEventListener("click", resetView);
$("viz-spin").addEventListener("click", toggleSpin);
$("viz-ray").addEventListener("click", pubRender);
$("viz-png").addEventListener("click", savePng);
window.addEventListener("resize", () => { if (glViewer) glViewer.resize(); });

/* ---------- 历史任务 ---------- */
function populateVizSources(tasks) {
  const sel = $("viz-source");
  const cur = sel.value;
  sel.innerHTML = `<option value="">表单文件（左侧选择的 PDB）</option>`;
  (tasks || []).forEach(t => {
    const o = document.createElement("option");
    o.value = t.task_id;
    o.textContent = t.task_id + (t.final_pdb ? "（有结果）" : "");
    sel.appendChild(o);
  });
  if ([...sel.options].some(o => o.value === cur)) sel.value = cur;
}

async function loadHistory() {
  const body = $("history-body");
  try {
    const res = await fetch("/api/tasks");
    const data = await res.json();
    if (!data.tasks.length) {
      body.innerHTML = `<tr><td colspan="4" class="dim">暂无任务，在右侧提交第一个突变计算任务吧。</td></tr>`;
      populateVizSources([]);
      return;
    }
    populateVizSources(data.tasks);
    body.innerHTML = "";
    data.tasks.forEach(t => {
      const tr = el(`<tr>
        <td class="mono">${escapeHtml(t.task_id)}</td>
        <td>${t.live ? '<span class="badge run">运行中</span>'
             : t.final_pdb ? '<span class="badge ok">已完成</span>' : '<span class="badge">已结束</span>'}</td>
        <td>${t.final_pdb ? "ddg_results.txt ✓" : '<span class="dim">—</span>'}</td>
        <td style="text-align:right">
          ${t.live ? `<a href="#" data-attach="${escapeHtml(t.task_id)}">接入对话</a>` : ""}
          <a href="/api/tasks/${escapeHtml(t.task_id)}/download">下载</a>
        </td></tr>`);
      tr.querySelector("[data-attach]")?.addEventListener("click", (e) => {
        e.preventDefault();
        thread.innerHTML = "";
        setUserMsg(`接入正在运行的任务 ${t.task_id}，查看实时执行情况。`);
        subscribe(t.task_id);
      });
      body.appendChild(tr);
    });
  } catch {
    body.innerHTML = `<tr><td colspan="4" class="dim">历史任务加载失败</td></tr>`;
  }
}
loadHistory();

/* ---------- 服务器资源信息（并发提示等） ---------- */
(async () => {
  try {
    const res = await fetch("/api/system");
    SYS = await res.json();
    $("jobs-hint").textContent =
      `服务器 ${SYS.cpu_cores} 逻辑核（共享，请留余量）· 默认 ${SYS.jobs_default}`;
    $("jobs").placeholder = SYS.jobs_default;
    $("delay").placeholder = SYS.delay_default;
  } catch {
    $("jobs-hint").textContent = "并发 = 同时运行的 Rosetta 进程数 · 默认 10";
  }
})();

/* 页面加载时自动接入运行中的任务 */
(async () => {
  try {
    const res = await fetch("/api/tasks");
    const data = await res.json();
    const live = data.tasks.find(t => t.live);
    if (live) {
      thread.innerHTML = "";
      setUserMsg(`检测到正在运行的任务 ${live.task_id}，已自动接入实时对话。`);
      subscribe(live.task_id);
    }
  } catch {}
})();

/* 初始提示 */
addAgentText("💡 两种开始方式：① 左侧表单直接填写并提交；② 不填任务、直接在这里说「跑一个突变」，我会像 checklist 一样逐项问清关键参数，最后由你确认提交。结果中出现 A_76 这类位点时点击可在中部「结构可视化」定位。");
setComposerEnabled(true);
setChatStatus("待命", "");
