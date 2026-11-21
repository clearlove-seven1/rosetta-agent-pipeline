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
  /* 对话抽屉收起时，右缘标签钮亮琥珀点提示任务在跑 */
  document.body.classList.toggle("chat-live", cls === "run");
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
        /* 竞态兜底：流结束后任务已完成则确保续聊输入框可用 */
        if (doneState) setComposerEnabled(true);
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
  if (f && !$("viz-source").value && !$("viz-file").value) loadStructure();
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

/* ---------- 对话抽屉侧边栏（默认收起：右缘标签一键开合，ESC 也可收） ---------- */
$("chat-dock").addEventListener("click", () => document.body.classList.toggle("chat-open"));
$("chat-close").addEventListener("click", () => document.body.classList.remove("chat-open"));

/* ---------- 交互式 3D 结构工作台（PDBe Mol* + 命令行 + 全屏） ---------- */
function setVizStatus(text, cls) {
  $("viz-status").textContent = text;
  $("viz-status").style.background = cls === "run" ? "rgba(217,164,65,.35)"
    : cls === "ok" ? "rgba(61,220,151,.4)" : cls === "bad" ? "rgba(255,122,110,.45)"
    : "rgba(255,255,255,.14)";
}

const VIZ_PALETTE = ["#2fa3c7", "#e6a23c", "#7cc47f", "#c76b98", "#8f7fd1", "#d1655c", "#5aa9a2", "#b0b76b"];
const VIZ_MOL_OPTS = {
  bgColor: "#12151d", lighting: "matte", landscape: true,
  hideControls: true, /* 普通视图纯净画布；全屏工作台自动展开面板 */
  hideCanvasControls: ["expand"], sequencePanel: true, selectInteraction: true,
};

/* 查看器状态：下拉与命令行都改这里再应用 */
const VIZ = {
  v: null,            // PDBeMolstarPlugin 实例
  pdb: "", label: "", url: "",
  rep: "cartoon", color: "chain",
  hidden: { water: false, het: false, carbs: false },
  site: null,         // 位点 {chain, resi}
  src: null,          // 来源 {kind:"task"|"path"|"localfile"|"form", value?}
  file: null,
  spin: false,
  bg: "dark",
  ready: null,
};
window.VIZ = VIZ;

/* 渲染调优：双光源 + 提亮 + 链悬浮提示（比默认单光源更有层次） */
async function vizPolish() {
  if (!VIZ.v || !VIZ.v.plugin) return;
  try {
    VIZ.v.plugin.canvas3d.setProps({ renderer: {
      light: [
        { azimuth: 315, inclination: 120, intensity: 0.55, color: 0xffffff },
        { azimuth: 120, inclination: 60, intensity: 0.25, color: 0xdfeaff },
      ],
      ambientIntensity: 0.5,
      exposure: 1.05,
    } });
    const chains = vizChains();
    if (chains.length) {
      await VIZ.v.visual.tooltips({ data: chains.map((ch) => ({ struct_asym_id: ch, tooltip: "链 " + ch })) });
    }
  } catch (e) { /* 调优失败不影响主功能 */ }
}

function vizChains() {
  return [...new Set([...VIZ.pdb.matchAll(/^(?:ATOM  |HETATM).{15}([A-Za-z])/gm)].map((m) => m[1]))];
}

function ensureViewer() {
  if (VIZ.v) return Promise.resolve(VIZ.v);
  if (!VIZ.ready) {
    VIZ.ready = new Promise((resolve) => {
      const inst = new PDBeMolstarPlugin();
      inst.events.loadComplete.subscribe(() => {
        VIZ.v = inst;
        window.glViewer = inst; /* 控制台调试用 */
        $("viz-empty").hidden = true;
        resolve(inst);
      });
      VIZ.fresh = true; /* render 已直接加载当前 VIZ.url，首次 vizLoad 免重复 update */
      inst.render($("viz-canvas"), Object.assign(
        { customData: { url: VIZ.url, format: "pdb" } }, VIZ_MOL_OPTS, { visualStyle: VIZ.rep }));
    });
  }
  return VIZ.ready;
}

/* 链配色（逐链色板） */
async function vizApplyColors() {
  const chains = vizChains();
  if (!chains.length || !VIZ.v) return;
  await VIZ.v.visual.select({ data: chains.map((ch, i) => ({ struct_asym_id: ch, color: VIZ_PALETTE[i % VIZ_PALETTE.length] })) });
}

/* 隐藏状态应用（配体/水/糖） */
function vizApplyHidden() {
  if (!VIZ.v) return;
  VIZ.v.visual.visibility({ polymer: true, het: !VIZ.hidden.het, water: !VIZ.hidden.water, carbs: !VIZ.hidden.carbs });
}

/* HSL→HEX（spectrum 渐变用） */
function vizHsl(h) {
  const f = (n) => {
    const k = (n + h * 12) % 12;
    const c = 0.55 - 0.85 * Math.min(0.55, 0.45) * Math.max(-1, Math.min(k - 3, 9 - k, 1));
    return Math.round(255 * c).toString(16).padStart(2, "0");
  };
  return "#" + f(0) + f(8) + f(4);
}

/* 从 PDB 文本收集每链残基（有序去重） */
function vizResidues() {
  const map = new Map();
  for (const line of VIZ.pdb.split("\n")) {
    if (!line.startsWith("ATOM") && !line.startsWith("HETATM")) continue;
    const ch = line[21];
    if (!/[A-Za-z0-9]/.test(ch || "")) continue;
    const ri = parseInt(line.slice(22, 26), 10);
    if (isNaN(ri)) continue;
    if (!map.has(ch)) map.set(ch, []);
    const arr = map.get(ch);
    if (!arr.includes(ri)) arr.push(ri);
  }
  return map;
}

/* 配色方案应用：chain=逐链 / spectrum=残基渐变 / ss=二级结构 / byelement=默认主题 / 其余=逐链单色 */
async function vizApplyColor() {
  if (!VIZ.v) return;
  const c = VIZ.color;
  if (c === "chain") { await vizApplyColors(); return; }
  if (c === "spectrum") {
    const map = vizResidues();
    const items = [];
    let total = 0;
    for (const arr of map.values()) total += arr.length;
    let k = 0;
    for (const [ch, arr] of map) {
      for (const ri of arr) {
        items.push({ struct_asym_id: ch, start_residue_number: ri, end_residue_number: ri, color: vizHsl(total > 1 ? k / (total - 1) : 0) });
        k++;
      }
    }
    await VIZ.v.visual.select({ data: items });
    return;
  }
  if (c === "ss") {
    const helix = new Set(), sheet = new Set();
    for (const line of VIZ.pdb.split("\n")) {
      if (line.startsWith("HELIX ")) {
        const ch = line[19], a = parseInt(line.slice(20, 25), 10), b = parseInt(line.slice(32, 37), 10);
        if (ch && !isNaN(a) && !isNaN(b)) for (let r = a; r <= b; r++) helix.add(ch + ":" + r);
      } else if (line.startsWith("SHEET ")) {
        const ch = line[21], a = parseInt(line.slice(22, 26), 10), b = parseInt(line.slice(33, 37), 10);
        if (ch && !isNaN(a) && !isNaN(b)) for (let r = a; r <= b; r++) sheet.add(ch + ":" + r);
      }
    }
    const items = [];
    for (const [ch, arr] of vizResidues()) {
      for (const ri of arr) {
        const key = ch + ":" + ri;
        items.push({ struct_asym_id: ch, start_residue_number: ri, end_residue_number: ri,
          color: helix.has(key) ? "#f2c14e" : sheet.has(key) ? "#e25fb0" : "#63c7d4" });
      }
    }
    await VIZ.v.visual.select({ data: items });
    return;
  }
  if (c === "byelement") { await VIZ.v.visual.reset({ theme: true }); return; }
  await VIZ.v.visual.select({ data: vizChains().map((ch) => ({ struct_asym_id: ch, color: c })) });
}

/* 加载/重载当前 URL 的结构并应用配色 */
async function vizLoad(label) {
  VIZ.label = label;
  VIZ.site = null;
  VIZ.hidden = { water: false, het: false, carbs: false };
  syncToggles();
  setVizStatus("加载中…", "run");
  try {
    const v = await ensureViewer();
    if (VIZ.v !== v) throw new Error("查看器初始化失败");
    if (!VIZ.fresh) {
      await v.visual.update(Object.assign(
        { customData: { url: VIZ.url, format: "pdb" } }, VIZ_MOL_OPTS, { visualStyle: VIZ.rep }), true);
    } else {
      VIZ.fresh = false;
    }
    /* 抓一份 PDB 文本用于链/残基解析（链配色/spectrum/ss） */
    try { const r = await fetch(VIZ.url); VIZ.pdb = await r.text(); } catch (e2) { VIZ.pdb = ""; }
    await vizApplyColors();
    await vizPolish();
    setVizStatus(`✓ 已加载 ${label}（${vizChains().length} 链）— 左键旋转 · 右键平移 · 点击残基聚焦 · 右侧面板测量/样式`, "ok");
  } catch (e) {
    setVizStatus("加载失败：" + e.message, "bad");
  }
}

/* 位点定位：黄色高亮 + 侧链展示 + 镜头聚焦（等价 PyMOL 的 zoom site） */
async function focusSite(chain, resi, silent) {
  if (!VIZ.v) { if (!silent) setVizStatus("请先加载结构（选来源或左侧选 PDB）", "bad"); return; }
  const r = parseInt(resi, 10);
  if (isNaN(r)) { if (!silent) setVizStatus("位点格式：残基号（如 76）", "bad"); return; }
  const ch = chain || vizChains()[0] || "A";
  VIZ.site = { chain: ch, resi: r };
  try {
    await VIZ.v.visual.select({ keepColors: true, data: [{ struct_asym_id: ch, start_residue_number: r, end_residue_number: r, color: "#f5d020", sideChain: true, focus: true }] });
    if (!silent) setVizStatus("已定位 " + (chain ? chain + "_" : "") + resi, "ok");
  } catch (e) {
    if (!silent) setVizStatus("定位失败：" + e.message, "bad");
  }
}

async function resetView() {
  if (!VIZ.v) return;
  VIZ.site = null;
  await VIZ.v.visual.clearSelection();
  await VIZ.v.visual.reset({ camera: true });
  await vizApplyColors();
  setVizStatus("已复位", "");
}

function toggleSpin() {
  if (!VIZ.v) return;
  VIZ.spin = !VIZ.spin;
  VIZ.v.visual.toggleSpin(VIZ.spin);
  $("viz-spin").textContent = VIZ.spin ? "自旋:开" : "自旋";
}

async function loadStructure() {   /* viz-source：表单文件 / 历史任务 */
  const src = $("viz-source").value;
  const f = $("pdb-input").files[0];
  if (src) {
    VIZ.src = { kind: "task", value: src };
    VIZ.url = `/api/structure?task_id=${encodeURIComponent(src)}`;
    await vizLoad(src);
    return;
  }
  if (!f) { setVizStatus("请先选 PDB 或历史任务", "bad"); return; }
  VIZ.src = { kind: "form" };
  VIZ.file = f;
  VIZ.url = URL.createObjectURL(f);
  await vizLoad(f.name);
}

/* PyMOL 发表图：服务器光追渲染（与当前面板联动；支持全部来源） */
async function pubRender() {
  const fd = new FormData();
  const s = VIZ.src || {};
  if (s.kind === "task") fd.append("task_id", s.value);
  else if (s.kind === "path") fd.append("pdb_path", s.value);
  else if (s.kind === "localfile" && VIZ.file) fd.append("file", VIZ.file);
  else {
    const f = $("pdb-input").files[0];
    if (!f) { setVizStatus("请先加载结构", "bad"); return; }
    fd.append("file", f);
  }
  setVizStatus("光追渲染中（可达数十秒）…", "run");
  try {
    fd.append("chain", $("viz-chain").value);
    fd.append("resi", $("viz-resi").value.trim());
    /* 发表图与交互面板联动：真实传递当前表示方法/配色 */
    fd.append("rep", $("viz-rep").value === "ball-and-stick" ? "stick"
      : $("viz-rep").value === "spacefill" ? "sphere"
      : $("viz-rep").value === "putty" || $("viz-rep").value.startsWith("molecular") || $("viz-rep").value.startsWith("gaussian") ? "" : $("viz-rep").value);
    const col = $("viz-color").value;
    fd.append("color", col === "ss" ? "ssPyMOL" : (col === "byelement" ? "" : col));
    if (!fd.get("color")) fd.set("style", "soft");
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
  if (!VIZ.v) { setVizStatus("尚未加载结构", "bad"); return; }
  const cv = document.querySelector("#viz-canvas canvas");
  if (!cv) { setVizStatus("未找到画布", "bad"); return; }
  const a = document.createElement("a");
  a.href = cv.toDataURL("image/png");
  a.download = "structure_view.png";
  a.click();
  setVizStatus("已保存视图 PNG", "ok");
}

function syncToggles() {
  const w = $("viz-t-w"), l = $("viz-t-l"), c = $("viz-t-c");
  if (!w || !l || !c) return;
  w.textContent = "水:" + (VIZ.hidden.water ? "隐" : "显");
  l.textContent = "配体:" + (VIZ.hidden.het ? "隐" : "显");
  c.textContent = "糖:" + (VIZ.hidden.carbs ? "隐" : "显");
  w.classList.toggle("ton", VIZ.hidden.water);
  l.classList.toggle("ton", VIZ.hidden.het);
  c.classList.toggle("ton", VIZ.hidden.carbs);
}
function vizSetHidden(k, val) {
  VIZ.hidden[k] = val;
  syncToggles();
  vizApplyHidden();
}

/* ---------- 全屏工作台 ---------- */
function vizFullscreen() {
  const card = $("viz-card");
  const on = !card.classList.contains("viz-fs");
  card.classList.toggle("viz-fs", on);
  document.body.classList.toggle("viz-lock", on);
  $("viz-fs").textContent = on ? "✕ 退出全屏" : "⛶ 全屏工作台";
  setTimeout(() => {
    if (!VIZ.v) return;
    if (VIZ.v.canvas && VIZ.v.canvas.toggleControls) VIZ.v.canvas.toggleControls(on); /* 全屏展开面板 */
    if (VIZ.v.plugin && VIZ.v.plugin.canvas3d && VIZ.v.plugin.canvas3d.handleResize) VIZ.v.plugin.canvas3d.handleResize();
  }, 120);
}
document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape") return;
  if ($("viz-card").classList.contains("viz-fs")) { vizFullscreen(); return; }
  if (document.body.classList.contains("chat-open")) document.body.classList.remove("chat-open");
});

/* ---------- Mol* 版命令行（多条用 ; 分隔） ---------- */
const VIZ_REP_ALIAS = { cartoon: "cartoon", sticks: "ball-and-stick", stick: "ball-and-stick",
  ball_stick: "ball-and-stick", spheres: "spacefill", sphere: "spacefill", balls: "spacefill",
  surface: "molecular-surface", gaussian: "gaussian-surface", putty: "putty" };
const VIZ_NAMED = { red: "#e2493b", blue: "#3b72e2", yellow: "#f2c14e", green: "#63b463", orange: "#e28a3b",
  magenta: "#c74fb0", cyan: "#3bc2d4", teal: "#0e7c86", pink: "#e88aa0", gray: "#8a93a6", grey: "#8a93a6",
  white: "#ffffff", black: "#15181f", purple: "#8f7fd1", salmon: "#f0937a", wheat: "#e8d08a",
  violet: "#7f5fd1", lime: "#a6d65c", chocolate: "#8a5a3b" };

function vizTokenize(s) {
  const toks = [];
  const re = /("[^"]*"|\(|\)|,|[^\s(),]+)/g;
  let m;
  while ((m = re.exec(s))) toks.push(m[1]);
  return toks;
}

function vizSetRep(rep) {
  VIZ.rep = rep;
  if ([...$("viz-rep").options].some((o) => o.value === rep)) $("viz-rep").value = rep;
}
async function vizSwitchRep(mapped, labelTxt) {
  vizSetRep(mapped);
  await VIZ.v.visual.update(Object.assign(
    { customData: { url: VIZ.url, format: "pdb" } }, VIZ_MOL_OPTS, { visualStyle: mapped }), true);
  await vizApplyColors();
  if (VIZ.site) await focusSite(VIZ.site.chain, VIZ.site.resi, true);
  return "表示 → " + labelTxt;
}

/* 执行一条命令，返回反馈文本 */
async function vizOneCmd(line) {
  const toks = vizTokenize(line);
  if (!toks.length) return "";
  const verb = toks[0].toLowerCase();
  switch (verb) {
    case "show": {
      if (!toks[1]) throw new Error("show 需要表示方法，如 show cartoon / show ligand");
      const t = toks[1].toLowerCase();
      if (t === "ligand") { vizSetHidden("het", false); return "已显示配体"; }
      if (t === "water") { vizSetHidden("water", false); return "已显示水分子"; }
      if (t === "carbs" || t === "carbohydrate") { vizSetHidden("carbs", false); return "已显示糖类"; }
      if (t === "everything" || t === "all") { VIZ.hidden = { water: false, het: false, carbs: false }; syncToggles(); vizApplyHidden(); return "已全部显示"; }
      const rep = VIZ_REP_ALIAS[t];
      if (!rep) throw new Error(`未知表示 "${toks[1]}"（cartoon/sticks/spheres/surface/putty）`);
      if (rep === VIZ.rep) return `已是 ${toks[1]}`;
      return vizSwitchRep(rep, toks[1]);
    }
    case "hide": {
      if (!toks[1]) throw new Error("hide 需要对象，如 hide water / hide ligand / hide everything");
      const t = toks[1].toLowerCase();
      if (t === "water") { vizSetHidden("water", true); return "已隐藏水分子"; }
      if (t === "ligand" || t === "het") { vizSetHidden("het", true); return "已隐藏配体"; }
      if (t === "carbs" || t === "carbohydrate") { vizSetHidden("carbs", true); return "已隐藏糖类"; }
      if (t === "everything" || t === "all") {
        VIZ.v.visibility({ polymer: false, het: false, water: false, carbs: false });
        return "已隐藏全部（reset 或 show everything 恢复）";
      }
      const rep = VIZ_REP_ALIAS[t];
      if (rep && rep === VIZ.rep) return vizSwitchRep("cartoon", "cartoon（切回飘带）");
      throw new Error("hide 仅支持 water/ligand/carbs/everything 或当前表示；详细控制用右侧面板");
    }
    case "color": case "colour": {
      if (!toks[1]) throw new Error("color 需要配色：spectrum/chain/ss/byelement/颜色名");
      const cRaw = String(toks[1]);
      const lowC = cRaw.toLowerCase();
      const special = ["spectrum", "chain", "ss", "byelement"].includes(lowC);
      if (!special && !VIZ_NAMED[lowC] && !/^#?[0-9a-fA-F]{6}$/.test(cRaw)) throw new Error(`未知配色 "${cRaw}"`);
      const c = VIZ_NAMED[lowC] || lowC;
      /* color <色> resi <n> [chain <c>]：单残基着色 */
      if (toks[2] === "resi") {
        const r = parseInt(toks[3], 10);
        if (isNaN(r)) throw new Error("resi 后需要残基号");
        const chM = toks[4] === "chain" ? toks[5] : (vizChains()[0] || "A");
        return VIZ.v.visual.select({ keepColors: true, data: [{ struct_asym_id: chM, start_residue_number: r, end_residue_number: r, color: c }] })
          .then(() => `已对 ${chM}/${r} 上色 ${cRaw}`);
      }
      VIZ.color = c;
      if ([...$("viz-color").options].some((o) => o.value === c)) $("viz-color").value = c;
      return vizApplyColor().then(() => `配色 → ${cRaw}`);
    }
    case "zoom": {
      const ri = toks[1] === "resi" ? parseInt(toks[2], 10) : NaN;
      if (!isNaN(ri)) {
        const ch = toks[3] === "chain" ? toks[4] : (vizChains()[0] || "A");
        return VIZ.v.visual.focus([{ struct_asym_id: ch, start_residue_number: ri, end_residue_number: ri }])
          .then(() => `已聚焦 ${ch}/${ri}`);
      }
      return VIZ.v.visual.reset({ camera: true }).then(() => "已复位视角（全结构）");
    }
    case "orient": case "center": {
      const ri = toks[1] === "resi" ? parseInt(toks[2], 10) : NaN;
      if (isNaN(ri)) return VIZ.v.visual.reset({ camera: true }).then(() => "已归位（全结构）");
      const ch = toks[3] === "chain" ? toks[4] : (vizChains()[0] || "A");
      return VIZ.v.visual.focus([{ struct_asym_id: ch, start_residue_number: ri, end_residue_number: ri }])
        .then(() => `已聚焦 ${ch}/${ri}`);
    }
    case "reset": return VIZ.v.visual.reset({ camera: true, theme: true }).then(async () => { await vizApplyColors(); return "已复位"; });
    case "bg": case "background": {
      const c = VIZ_NAMED[String(toks[1] || "white").toLowerCase()] || toks[1] || "white";
      return VIZ.v.canvas.setBgColor(c).then(() => `背景 → ${toks[1] || "white"}`);
    }
    case "spin": {
      const a = String(toks[1] || (VIZ.spin ? "off" : "on")).toLowerCase();
      const on = a === "on" || a === "true";
      VIZ.spin = on;
      VIZ.v.visual.toggleSpin(on);
      $("viz-spin").textContent = VIZ.spin ? "自旋:开" : "自旋";
      return VIZ.spin ? "自旋开" : "自旋关";
    }
    case "water": {
      const on = !/^(off|hide|隐|隐藏)$/i.test(toks[1] || "off");
      vizSetHidden("water", !on);
      return on ? "显示水分子" : "隐藏水分子";
    }
    case "ligand": {
      const on = !/^(off|hide|隐|隐藏)$/i.test(toks[1] || "off");
      vizSetHidden("het", !on);
      return on ? "显示配体" : "隐藏配体";
    }
    case "interactions": {
      const r = parseInt(toks[1], 10);
      if (isNaN(r)) throw new Error("interactions 用法：interactions <残基号>（显示该残基周围非共价相互作用）");
      const ch = toks[2] === "chain" ? toks[3] : (vizChains()[0] || "A");
      return VIZ.v.visual.interactivityFocus({ data: [{ struct_asym_id: ch, residue_number: r }] })
        .then(() => `已显示 ${ch}/${r} 周围相互作用`);
    }
    case "preset": {
      const p = String(toks[1] || "pretty").toLowerCase();
      if (p === "pretty") { VIZ.color = "chain"; $("viz-color").value = "chain"; return vizSwitchRep("cartoon", "pretty").then(() => vizApplyColor().then(() => "preset pretty")); }
      if (p === "surface") return vizSwitchRep("molecular-surface", "surface");
      if (p === "ball_stick" || p === "ballstick") return vizSwitchRep("ball-and-stick", "ball_stick");
      if (p === "spacefill") return vizSwitchRep("spacefill", "spacefill");
      if (p === "illustrative" || p === "sketch") {
        const plugin = VIZ.v.plugin;
        const st = plugin.managers.structure.hierarchy.current.structures;
        if (!st.length) throw new Error("还没有加载结构");
        const ref = st[st.length - 1];
        const tryApply = async (id) => {
          try { return await plugin.managers.structure.presets.apply(ref, id); }
          catch (e) { return null; }
        };
        const done = await tryApply("preset-structure-representation-illustrative") || await tryApply("illustrative");
        if (done) return "已应用 Illustrative 素描风（内置预设）";
        /* 本版本无内置预设 → 等效组合：spacefill + 平面光照 + 彩虹渐变 */
        await vizSwitchRep("spacefill", "spacefill");
        VIZ.v.plugin.canvas3d.setProps({ renderer: { light: [], ambientIntensity: 1, exposure: 1.15 } });
        VIZ.color = "spectrum";
        if ([...$("viz-color").options].some((o) => o.value === "spectrum")) $("viz-color").value = "spectrum";
        await vizApplyColor();
        return "已应用 Illustrative 素描风（等效组合）";
      }
      throw new Error("未知 preset（pretty/surface/ball_stick/spacefill/illustrative）");
    }
    case "style": {
      const s = String(toks[1] || "flat").toLowerCase();
      if (s === "flat" || s === "illustrative") {
        VIZ.v.plugin.canvas3d.setProps({ renderer: { light: [], ambientIntensity: 1, exposure: 1.1 } });
        return "已切换平面素描光照";
      }
      if (s === "pretty" || s === "default") { await vizPolish(); return "已恢复双光源质感"; }
      throw new Error("style flat|pretty");
    }
    case "help": {
      const d = $("viz-help");
      if (d) d.open = true;
      return "命令速查已在下方展开";
    }
    case "distance": case "label": case "rotate": case "move": case "hydrogen": case "select":
      throw new Error(`此命令请用右侧 Structure Tools 面板（Measurements 可测距/加标签）`);
    default:
      throw new Error(`未知命令 "${toks[0]}"（输入 help 看速查）`);
  }
}

/* 执行一行（可含 ; 分隔的多条命令），返回反馈文本 */
async function vizExec(line) {
  const outs = [];
  for (let c of String(line).split(";")) {
    c = c.trim();
    if (!c) continue;
    try { outs.push(String(await vizOneCmd(c))); }
    catch (e) { outs.push("✕ " + e.message); }
  }
  return outs.filter(Boolean).join("；");
}
window.vizExec = vizExec; /* 控制台/自动化测试钩子 */

/* ---------- 查看器交互接线 ---------- */
const vizCmdHist = [];
let vizCmdIdx = -1;
function vizRunLine() {
  const inp = $("viz-cmd");
  const line = inp.value.trim();
  if (!line) return;
  vizCmdHist.unshift(line);
  if (vizCmdHist.length > 60) vizCmdHist.pop();
  vizCmdIdx = -1;
  inp.value = "";
  if (!VIZ.v) { setVizStatus("请先加载结构，再执行命令", "bad"); return; }
  const msg = vizExec(line);
  setVizStatus(msg || "✓ 已执行", msg && msg.includes("✕") ? "bad" : "");
}
$("viz-cmd").addEventListener("keydown", (e) => {
  if (e.key === "Enter") { e.preventDefault(); vizRunLine(); }
  else if (e.key === "ArrowUp") {
    e.preventDefault();
    if (vizCmdHist.length) {
      vizCmdIdx = Math.min(vizCmdIdx + 1, vizCmdHist.length - 1);
      $("viz-cmd").value = vizCmdHist[vizCmdIdx];
    }
  } else if (e.key === "ArrowDown") {
    e.preventDefault();
    vizCmdIdx = Math.max(vizCmdIdx - 1, -1);
    $("viz-cmd").value = vizCmdIdx < 0 ? "" : vizCmdHist[vizCmdIdx];
  }
});
$("viz-cmd-go").addEventListener("click", vizRunLine);

$("viz-source").addEventListener("change", loadStructure);
$("viz-file").addEventListener("change", () => {
  const p = $("viz-file").value;
  if (p) {
    VIZ.src = { kind: "path", value: p };
    VIZ.url = `/api/viewer/pdb?path=${encodeURIComponent(p)}`;
    vizLoad(p);
  }
});
$("viz-open").addEventListener("click", () => $("viz-local").click());
$("viz-local").addEventListener("change", async () => {
  const f = $("viz-local").files[0];
  if (!f) return;
  VIZ.src = { kind: "localfile" };
  VIZ.file = f;
  VIZ.url = URL.createObjectURL(f);
  await vizLoad(f.name);
});
$("viz-rep").addEventListener("change", async () => {
  VIZ.rep = $("viz-rep").value;
  if (!VIZ.v) return;
  setVizStatus("切换表示中…", "run");
  await VIZ.v.visual.update(Object.assign(
    { customData: { url: VIZ.url, format: "pdb" } }, VIZ_MOL_OPTS, { visualStyle: VIZ.rep }), true);
  await vizApplyColors();
  setVizStatus("表示方法 → " + $("viz-rep").selectedOptions[0].textContent, "");
});
$("viz-color").addEventListener("change", () => {
  VIZ.color = $("viz-color").value;
  if (!VIZ.v) return;
  vizApplyColor().then(() => setVizStatus("配色 → " + $("viz-color").selectedOptions[0].textContent, ""));
});
$("viz-btn").addEventListener("click", () => focusSite($("viz-chain").value, $("viz-resi").value.trim()));
$("viz-resi").addEventListener("keydown", (e) => {
  if (e.key === "Enter") { e.preventDefault(); focusSite($("viz-chain").value, $("viz-resi").value.trim()); }
});
$("viz-clear").addEventListener("click", resetView);
$("viz-spin").addEventListener("click", toggleSpin);
$("viz-t-w").addEventListener("click", () => vizSetHidden("water", !VIZ.hidden.water));
$("viz-t-l").addEventListener("click", () => vizSetHidden("het", !VIZ.hidden.het));
$("viz-t-c").addEventListener("click", () => vizSetHidden("carbs", !VIZ.hidden.carbs));
$("viz-fs").addEventListener("click", vizFullscreen);
$("viz-bg").addEventListener("click", async () => {
  if (!VIZ.v) { setVizStatus("请先加载结构", "bad"); return; }
  VIZ.bg = VIZ.bg === "dark" ? "white" : "dark";
  $("viz-bg").textContent = VIZ.bg === "dark" ? "背景:暗" : "背景:白";
  try { await VIZ.v.canvas.setBgColor(VIZ.bg === "dark" ? "#12151d" : "#ffffff"); } catch (e) { setVizStatus("背景切换失败：" + e.message, "bad"); }
});
$("viz-ray").addEventListener("click", pubRender);
$("viz-png").addEventListener("click", savePng);
window.addEventListener("resize", () => { /* Mol* 自适应容器尺寸 */ });

/* 滚轮归属：无修饰键的滚轮放行给页面/全屏卡滚动，Ctrl/⌘+滚轮才缩放结构 */
let vizWheelTipShown = false;
$("viz-card").addEventListener("wheel", (e) => {
  if (e.ctrlKey || e.metaKey || e.altKey) return;
  e.stopPropagation();
  if (!vizWheelTipShown && VIZ.pdb) {
    vizWheelTipShown = true;
    setVizStatus("滚轮已用于滚动页面 · 缩放请按住 Ctrl + 滚轮（触控板直接捏合）", "");
  }
}, { capture: true, passive: true });

/* 聊天里的 A_76 / D_A76_A chip → 点击定位到结构 */
thread.addEventListener("click", (e) => {
  const chip = e.target.closest(".site-chip");
  if (!chip) return;
  focusSite(chip.dataset.chain, chip.dataset.resi);
  $("viz-card").scrollIntoView({ behavior: "smooth", block: "start" });
});

/* 项目内 PDB 清单 → viz-file 下拉 */
(async () => {
  try {
    const res = await fetch("/api/viewer/files");
    const data = await res.json();
    const sel = $("viz-file");
    sel.innerHTML = `<option value="">项目内 PDB…</option>`;
    (data.files || []).forEach((f) => {
      const o = document.createElement("option");
      o.value = f.path;
      o.textContent = `${f.path}（${(f.size / 1024).toFixed(0)}KB）`;
      sel.appendChild(o);
    });
  } catch { /* 清单加载失败不阻塞查看器主功能 */ }
})();


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
        try {
          thread.innerHTML = "";
          setUserMsg(`接入正在运行的任务 ${t.task_id}，查看实时执行情况。`);
        } catch (err) { /* 清空/渲染失败不得阻塞订阅 */ }
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
