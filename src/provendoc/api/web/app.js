/* ============================================================
 * Provendoc 调参工作台 — 前端逻辑
 * 契约事实源：
 *   - POST /api/v1/evaluate        multipart(file, params Form JSON 字符串)
 *   - POST /api/v1/auto-pipeline   同上（narrative 全跑，其他域 stop-here）
 *   - 错误契约：422（参数枚举非法）/ 400（解析失败或 params 非法 JSON）/ 200 + metadata.benchmark 缺失
 *   - RSE 信封：metadata.benchmark.rse 三层（quality_profile / gate_status / capability_profile，完成度块 capability_profile.evaluation —— capability_evaluation_block_prereg_v1）
 * ============================================================ */
"use strict";

const API = {
  health: "/health",
  domains: "/api/v1/domains",
  evaluate: "/api/v1/evaluate",
  autoPipeline: "/api/v1/auto-pipeline",
};

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

const state = {
  file: null,
  running: false,
  history: [],   // localStorage 持久化
  compare: new Set(), // 勾选用于对比的历史行 id
};

/* ---------------- 健康探测 ---------------- */
async function pingHealth() {
  const dot = $("#healthDot");
  try {
    const r = await fetch(API.health);
    dot.className = "health " + (r.ok ? "ok" : "down");
  } catch {
    dot.className = "health down";
  }
}

/* ---------------- 文件上传 ---------------- */
function bindFile() {
  const dz = $("#dropZone");
  const input = $("#fileInput");

  dz.addEventListener("click", () => input.click());
  input.addEventListener("change", () => {
    if (input.files.length) setFile(input.files[0]);
  });
  dz.addEventListener("dragover", (e) => { e.preventDefault(); dz.classList.add("dragover"); });
  dz.addEventListener("dragleave", () => dz.classList.remove("dragover"));
  dz.addEventListener("drop", (e) => {
    e.preventDefault();
    dz.classList.remove("dragover");
    if (e.dataTransfer.files.length) setFile(e.dataTransfer.files[0]);
  });
}

function setFile(f) {
  state.file = f;
  const badge = $("#fileBadge");
  badge.classList.remove("hidden");
  badge.innerHTML = "";
  const name = document.createElement("span");
  name.textContent = `${f.name}（${fmtBytes(f.size)}）`;
  const clear = document.createElement("button");
  clear.className = "clear-file";
  clear.title = "移除文件";
  clear.textContent = "✕";
  clear.onclick = () => { state.file = null; badge.classList.add("hidden"); syncRunBtn(); };
  badge.append(name, clear);
  syncRunBtn();
}

function fmtBytes(n) {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}

/* ---------------- 参数表单 ---------------- */
function bindParams() {
  $$("[data-param]").forEach((el) => {
    el.addEventListener("input", () => {
      el.classList.toggle("dirty", el.value !== "");
      syncDirtyCount();
    });
  });
  $("#resetParams").addEventListener("click", () => {
    $$("[data-param]").forEach((el) => {
      if (el.disabled) return;
      if (el.type === "checkbox") el.checked = false;
      else el.value = "";
      el.classList.remove("dirty");
    });
    syncDirtyCount();
  });
}

function syncDirtyCount() {
  const n = $$("[data-param]").filter((el) => !el.disabled && (el.type === "checkbox" ? el.checked : el.value !== "")).length;
  $("#dirtyCount").textContent = n ? `${n} 个参数已修改` : "";
}

/** 只收集改过的项：number/text 取非空值，checkbox 只有勾选才提交 */
function collectParams() {
  const params = {};
  $$("[data-param]").forEach((el) => {
    if (el.disabled) return;
    const key = el.dataset.param;
    if (el.type === "checkbox") {
      if (el.checked) params[key] = true;
    } else if (el.value !== "") {
      const num = Number(el.value);
      params[key] = Number.isNaN(num) ? el.value : num;
    }
  });
  return params;
}

function syncRunBtn() {
  $("#runBtn").disabled = state.running || !state.file;
}

/* ---------------- 运行 ---------------- */
async function run() {
  if (state.running || !state.file) return;
  const method = $('input[name="method"]:checked').value;
  const params = collectParams();

  state.running = true;
  syncRunBtn();
  const btn = $("#runBtn");
  btn.classList.add("busy");
  btn.textContent = "运行中 …";
  hideError();

  renderRunning(method);

  const fd = new FormData();
  fd.append("file", state.file);
  fd.append("params", JSON.stringify(params));
  fd.append("include_text", "false"); // 结果区只展示结构与分数，不拉大文本

  try {
    const resp = await fetch(method === "evaluate" ? API.evaluate : API.autoPipeline, {
      method: "POST",
      body: fd,
    });

    if (resp.status === 422) {
      const detail = await safeJson(resp);
      throw new ApiError(422, detail, "参数校验失败（422）");
    }
    if (resp.status === 400) {
      const detail = await safeJson(resp);
      throw new ApiError(400, detail, "请求或解析失败（400）");
    }
    if (!resp.ok) {
      throw new ApiError(resp.status, null, `HTTP ${resp.status}`);
    }

    const result = await resp.json();
    renderResult(result, method, params);
    pushHistory(result, method, params);
  } catch (err) {
    showError(err);
    renderErrorResult(err);
  } finally {
    state.running = false;
    btn.classList.remove("busy");
    btn.textContent = "运行 ▸";
    syncRunBtn();
  }
}

async function safeJson(resp) {
  try { return await resp.json(); } catch { return null; }
}

function ApiError(status, detail, msg) {
  this.status = status;
  this.detail = detail;
  this.message = msg || `HTTP ${status}`;
}
ApiError.prototype = Object.create(Error.prototype);

function showError(err) {
  const box = $("#runError");
  box.classList.remove("hidden");
  let text = `${err.message || err}`;
  if (err.detail) {
    const d = typeof err.detail === "string" ? err.detail : JSON.stringify(err.detail);
    text += `\n${d}`;
  }
  box.textContent = text;
}
function hideError() { $("#runError").classList.add("hidden"); }

/* ---------------- 结果渲染 ---------------- */
function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}

function renderRunning(method) {
  const box = $("#results");
  box.innerHTML = "";
  box.append(el("div", "placeholder", `正在调用 ${method} …`));
}

function renderErrorResult(err) {
  const box = $("#results");
  box.innerHTML = "";
  const ph = el("div", "placeholder", `本次运行失败：${err.message || err}`);
  box.append(ph);
}

function renderResult(result, method, params) {
  const box = $("#results");
  box.innerHTML = "";

  const meta = result.metadata || {};
  const benchmark = meta.benchmark || {};
  const rse = benchmark.rse;
  const file = (result.file && (result.file.name || result.file.path)) || "（未知文件）";
  const domain = meta.domain || (rse && rse.domain) || "—";

  // ---- 头部：文件 / 域 / gate / evaluation（归层 capability_profile.evaluation） ----
  const evaluationStatus = (rse && rse.capability_profile && rse.capability_profile.evaluation && rse.capability_profile.evaluation.status) || null;
  const head = el("div", "result-head");
  head.append(el("span", "file", file));
  head.append(el("span", "", `domain=${domain}`));
  head.append(el("span", "", `evaluator=${benchmark.evaluator || (rse && rse.quality_profile && rse.quality_profile.profile_version) || "—"}`));
  if (rse && rse.gate_status) head.append(gateBadge(rse.gate_status.status));
  if (evaluationStatus) head.append(el("span", `badge ${evaluationStatus}`, `evaluation=${evaluationStatus}`));
  box.append(head);

  if (!rse) {
    // 错误契约：200 但 benchmark 缺失（如 parse_failed）——诚实展示
    const warn = el("div", "eval-section");
    warn.append(el("h4", "", "未生成 RSE 信封"));
    const note = el("div", "kv");
    note.append(el("span", "k", "说明"));
    note.append(el("span", "v", "本次结果没有 benchmark.rse（评估未运行或解析失败）。顶部错误框可能包含详情。"));
    warn.append(note);
    box.append(warn);
    appendRaw(box, result);
    return;
  }

  // ---- 三层：quality / gate / capability ----
  box.append(renderQuality(rse.quality_profile, benchmark));
  box.append(renderGate(rse.gate_status));
  box.append(renderCapability(rse.capability_profile));

  // ---- auto-pipeline 双基准 ----
  if (method === "auto-pipeline" && benchmark.optimized) {
    box.append(renderDuo(benchmark, benchmark.optimized));
  }

  appendRaw(box, { rse });
}

function gateBadge(status) {
  const label = `gate=${status}`;
  return el("span", `badge ${status}`, label);
}

function renderQuality(qp, benchmark) {
  const sec = el("div", "eval-section");
  sec.append(el("h4", "", "Quality Profile（维度分数 · 唯一计分）"));

  if (!qp || !qp.dimensions) {
    sec.append(el("div", "", "（无维度数据）"));
    return sec;
  }

  const overall = qp.overall;
  const dims = el("div", "dims");

  const entries = Object.entries(qp.dimensions)
    .sort((a, b) => (b[1] ? b[1].score : -1) - (a[1] ? a[1].score : -1));

  for (const [name, d] of entries) {
    if (!d) continue;
    const row = el("div", "dim-row");
    row.append(el("div", "dim-name", name));
    const track = el("div", "dim-bar-track");
    const pct = Math.max(0, Math.min(100, (d.score ?? 0) * 100));
    const bar = el("div", `dim-bar ${pct >= 70 ? "high" : pct >= 40 ? "mid" : "low"}`);
    bar.style.width = `${pct}%`;
    track.append(bar);
    row.append(track);
    const val = el("div", "dim-val");
    const sc = el("span", "score", fmtScore(d.score));
    val.append(sc);
    if (d.n !== undefined) val.append(el("span", "n", `  n=${d.n}`));
    row.append(val);
    dims.append(row);
  }

  // 总分行
  if (overall !== undefined) {
    const row = el("div", "dim-row");
    row.append(el("div", "dim-name", "overall"));
    const track = el("div", "dim-bar-track");
    const pct = Math.max(0, Math.min(100, (overall ?? 0) * 100));
    const bar = el("div", `dim-bar ${pct >= 70 ? "high" : pct >= 40 ? "mid" : "low"}`);
    bar.style.width = `${pct}%`;
    track.append(bar);
    row.append(track);
    const val = el("div", "dim-val");
    val.append(el("span", "score", fmtScore(overall)));
    row.append(val);
    dims.append(row);
  }
  sec.append(dims);

  const profileVersion = (qp && qp.profile_version) || benchmark.profile_version;
  if (profileVersion) {
    sec.append(el("div", "kv", `profile_version = ${profileVersion}`));
  }
  if (qp.confidence !== undefined) {
    sec.append(el("div", "kv", `confidence = ${fmtScore(qp.confidence)}`));
  }
  return sec;
}

function fmtScore(s) {
  return typeof s === "number" ? s.toFixed(3) : String(s);
}

function renderGate(gs) {
  const sec = el("div", "eval-section");
  sec.append(el("h4", "", "Gate Status（控制流）"));
  if (!gs) { sec.append(el("div", "", "（无 gate 数据）")); return sec; }

  const grid = el("div", "kv-grid");
  const k1 = el("div", "kv");
  k1.append(el("span", "k", "status"), el("span", "v", String(gs.status)));
  grid.append(k1);
  if (gs.version) {
    const k2 = el("div", "kv");
    k2.append(el("span", "k", "version"), el("span", "v", gs.version));
    grid.append(k2);
  }
  if (gs.blocking_reasons && gs.blocking_reasons.length) {
    const k3 = el("div", "kv");
    k3.append(el("span", "k", "blocking_reasons"), el("span", "v", gs.blocking_reasons.join("; ")));
    grid.append(k3);
  }
  sec.append(grid);

  if (gs.failure_modes) {
    const ul = el("ul", "failure-modes");
    for (const [k, v] of Object.entries(gs.failure_modes)) {
      ul.append(el("li", "", `${k} = ${v}`));
    }
    sec.append(ul);
  }
  return sec;
}

function renderCapability(cp) {
  const sec = el("div", "eval-section");
  sec.append(el("h4", "", "Capability Profile（能力审计）"));
  if (!cp) { sec.append(el("div", "", "（无 capability 数据）")); return sec; }

  const grid = el("div", "kv-grid");
  const entries = Object.entries(cp.capabilities || cp);
  for (const [name, c] of entries) {
    if (!c || typeof c !== "object") continue;
    const kv = el("div", "kv");
    kv.append(el("span", "k", name));
    let line = `status=${c.status}`;
    if (c.limitations && c.limitations.length) line += ` · limitations: ${c.limitations.join(", ")}`;
    kv.append(el("span", "v", line));
    grid.append(kv);
  }
  sec.append(grid);
  return sec;
}

function renderDuo(defaultBm, optBm) {
  const sec = el("div", "eval-section");
  sec.append(el("h4", "", "策略对比（default vs optimized）"));
  const duo = el("div", "duo");

  const dScore = defaultBm.rse && defaultBm.rse.quality_profile ? defaultBm.rse.quality_profile.overall : null;
  const oScore = optBm.rse && optBm.rse.quality_profile ? optBm.rse.quality_profile.overall : null;

  duo.append(duoCol("default", dScore, defaultBm, oScore !== null && dScore >= oScore));
  duo.append(duoCol("optimized", oScore, optBm, oScore !== null && oScore >= dScore));
  sec.append(duo);
  return sec;
}

function duoCol(label, score, bm, isWinner) {
  const col = el("div", "duo-col" + (isWinner ? " winner" : ""));
  col.append(el("h5", "", `${label}${isWinner ? "  ✓ 更优" : ""}`));
  const big = el("div", "score-big", score !== null ? fmtScore(score) : "—");
  col.append(big);
  const conf = bm.rse && bm.rse.quality_profile ? bm.rse.quality_profile.confidence : null;
  col.append(el("div", "conf", `confidence=${conf !== null ? fmtScore(conf) : "—"}`));
  if (bm.strategy_name || bm.strategy) {
    col.append(el("div", "conf", `strategy=${bm.strategy_name || bm.strategy}`));
  }
  return col;
}

function appendRaw(box, obj) {
  const det = el("details", "raw-toggle");
  det.append(el("summary", "", "查看原始 JSON"));
  det.append(el("pre", "raw-json", JSON.stringify(obj, null, 2)));
  box.append(det);
}

/* ---------------- 运行历史 ---------------- */
const HKEY = "provendoc.tuning.history";
const HMAX = 50;

function loadHistory() {
  try { state.history = JSON.parse(localStorage.getItem(HKEY) || "[]"); }
  catch { state.history = []; }
}

function saveHistory() {
  localStorage.setItem(HKEY, JSON.stringify(state.history.slice(0, HMAX)));
}

function pushHistory(result, method, params) {
  const meta = result.metadata || {};
  const benchmark = meta.benchmark || {};
  const rse = benchmark.rse || {};
  const qp = rse.quality_profile || {};

  state.history.unshift({
    id: Date.now() + Math.random().toString(36).slice(2, 6),
    ts: new Date().toISOString(),
    file: (result.file && (result.file.name || result.file.path)) || "—",
    method,
    domain: meta.domain || rse.domain || "—",
    profile_version: qp.profile_version || "—",
    // representations 在响应信封顶层 (result.representations), 不在 metadata
    chunks: meta.chunk_count ?? (result.representations
      ? Object.values(result.representations).reduce(
        (n, r) => n + ((r.units && r.units.length)
          || (r.granularity && r.granularity.base
            ? r.granularity.base.length : 0)
          || 0), 0)
      : null),
    overall: qp.overall ?? null,
    confidence: qp.confidence ?? null,
    gate: rse.gate_status ? rse.gate_status.status : null,
    evaluation_status: evaluationStatus,
    params,
    dimensions: qp.dimensions || null,
  });
  state.history = state.history.slice(0, HMAX);
  saveHistory();
  renderHistory();
}

function renderHistory() {
  const panel = $("#historyPanel");
  const tbody = $("#historyTable tbody");
  tbody.innerHTML = "";
  if (!state.history.length) { panel.classList.add("hidden"); return; }
  panel.classList.remove("hidden");

  state.history.forEach((h, i) => {
    const tr = document.createElement("tr");
    tr.dataset.id = h.id;

    const sel = document.createElement("td");
    sel.className = "col-sel";
    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.checked = state.compare.has(h.id);
    cb.disabled = state.compare.size >= 2 && !state.compare.has(h.id);
    cb.addEventListener("change", () => {
      if (cb.checked) state.compare.add(h.id); else state.compare.delete(h.id);
      renderHistory();
      renderCompare();
    });
    sel.append(cb);
    tr.append(sel);

    tr.append(el("td", "num", String(state.history.length - i)));
    tr.append(el("td", "", short(h.file)));
    tr.append(el("td", "", h.method));
    tr.append(el("td", "", h.domain));
    tr.append(el("td", "", h.profile_version));
    tr.append(el("td", "num", h.chunks !== null && h.chunks !== undefined ? String(h.chunks) : "—"));
    tr.append(el("td", "num", h.overall !== null ? fmtScore(h.overall) : "—"));
    tr.append(el("td", "num", h.confidence !== null ? fmtScore(h.confidence) : "—"));

    const gateTd = el("td");
    if (h.gate) gateTd.append(el("span", `badge ${h.gate}`, h.gate));
    else gateTd.textContent = "—";
    tr.append(gateTd);

    tr.append(el("td", "col-note", summarizeParams(h.params)));

    const act = document.createElement("td");
    const view = el("button", "view-btn", "查看");
    view.onclick = () => viewHistory(h);
    const del = el("button", "view-btn del-btn", "删");
    del.title = "删除此行";
    del.onclick = () => {
      state.history = state.history.filter((x) => x.id !== h.id);
      state.compare.delete(h.id);
      saveHistory();
      renderHistory();
      renderCompare();
    };
    act.append(view, " ", del);
    tr.append(act);

    tbody.append(tr);
  });
}

function short(name) {
  return name.length > 28 ? "…" + name.slice(-27) : name;
}

function summarizeParams(p) {
  const keys = Object.keys(p || {});
  if (!keys.length) return "（默认）";
  return keys.map((k) => `${k}=${p[k]}`).join(" ");
}

function viewHistory(h) {
  // 历史行只保留了摘要 + 维度分数；展开成只读维度视图
  const box = $("#results");
  box.innerHTML = "";
  const head = el("div", "result-head");
  head.append(el("span", "file", h.file));
  head.append(el("span", "", `method=${h.method}`));
  head.append(el("span", "", `domain=${h.domain}`));
  head.append(el("span", "", `ts=${h.ts}`));
  if (h.gate) head.append(el("span", `badge ${h.gate}`, `gate=${h.gate}`));
  if (h.evaluation_status) head.append(el("span", `badge ${h.evaluation_status}`, `evaluation=${h.evaluation_status}`));
  box.append(head);

  if (h.dimensions) {
    box.append(renderQuality({ dimensions: h.dimensions, overall: h.overall, confidence: h.confidence }, { profile_version: h.profile_version }));
  } else {
    box.append(el("div", "placeholder", "此历史记录没有维度详情（只保留了摘要）。"));
  }
  box.append(el("div", "kv", `params = ${summarizeParams(h.params)}`));
}

function renderCompare() {
  const panel = $("#comparePanel");
  const ids = [...state.compare];
  if (ids.length !== 2) { panel.classList.add("hidden"); return; }
  const a = state.history.find((h) => h.id === ids[0]);
  const b = state.history.find((h) => h.id === ids[1]);
  if (!a || !b) { panel.classList.add("hidden"); return; }

  panel.classList.remove("hidden");
  panel.innerHTML = "";
  panel.append(el("h4", "", "对比（较新 vs 较旧）"));

  const dimNames = new Set([
    ...Object.keys((a && a.dimensions) || {}),
    ...Object.keys((b && b.dimensions) || {}),
  ]);

  const table = el("table", "cmp-table");
  const thead = el("thead");
  const hr = el("tr");
  hr.append(el("th", "", "维度"));
  hr.append(el("th", "", shortA(a)));
  hr.append(el("th", "", shortA(b)));
  hr.append(el("th", "", "Δ"));
  thead.append(hr);
  table.append(thead);
  const tbody = el("tbody");

  const r1 = dimRowVal(a, "overall");
  const r2 = dimRowVal(b, "overall");
  tbody.append(cmpRow("overall", r1, r2));
  for (const name of dimNames) {
    tbody.append(cmpRow(name, dimRowVal(a, name), dimRowVal(b, name)));
  }
  table.append(tbody);
  panel.append(table);
}

function shortA(h) { return h ? short(h.file) : "—"; }

function dimRowVal(h, name) {
  if (!h) return null;
  if (name === "overall") return h.overall;
  const d = h.dimensions && h.dimensions[name];
  return d ? (d.score ?? null) : null;
}

function cmpRow(name, v1, v2) {
  const tr = el("tr");
  tr.append(el("td", "", name));
  tr.append(el("td", "num", v1 !== null ? fmtScore(v1) : "—"));
  tr.append(el("td", "num", v2 !== null ? fmtScore(v2) : "—"));
  const d = (v1 !== null && v2 !== null) ? v1 - v2 : null;
  const td = el("td", "num");
  if (d === null || d === 0) td.classList.add("cmp-delta-same"), td.textContent = d === 0 ? "0" : "—";
  else if (d > 0) td.classList.add("cmp-delta-up"), td.textContent = `+${d.toFixed(3)}`;
  else td.classList.add("cmp-delta-down"), td.textContent = d.toFixed(3);
  tr.append(td);
  return tr;
}

/* ---------------- 启动 ---------------- */
function init() {
  bindFile();
  bindParams();
  $("#runBtn").addEventListener("click", run);
  $("#clearHistory").addEventListener("click", () => {
    state.history = [];
    state.compare.clear();
    saveHistory();
    renderHistory();
    renderCompare();
  });
  loadHistory();
  renderHistory();
  renderCompare();
  pingHealth();
  setInterval(pingHealth, 30000);
}

document.addEventListener("DOMContentLoaded", init);
