/* ===================== 基础工具 ===================== */
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

const CHAT_KEY = "ds.chat.v1";
const MODEL_KEY = "ds.chatModel";
const MAX_HISTORY = 60;

const S = {
  state: null,
  selected: null,
  catalog: [],
};

// 待确认的 Ollama 程序（选好后需点「确定使用」才切换）
let pendingOllama = "";

const Chat = {
  messages: [],
  model: "",
  memory: "",
  busy: false,
  controller: null,
  renderQueued: false,
  thinkLevel: localStorage.getItem("ds.thinkLevel") || "normal",
  webSearch: localStorage.getItem("ds.webSearch") !== "0",
};

const MEMORY_KEY = "ds.chatMemory";

const ACTIVE_STATUS = new Set([
  "downloading", "extracting", "installing", "installed",
  "pulling", "importing", "preparing", "starting",
]);

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

function fmtSize(n) {
  n = Number(n) || 0;
  if (n <= 0) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
  return `${n >= 100 || i === 0 ? Math.round(n) : n.toFixed(1)} ${units[i]}`;
}

function prettyName(name) {
  let n = String(name || "").replace(/^modelscope\.cn\//, "");
  if (n.endsWith(":latest")) n = n.slice(0, -7);
  return n;
}

function translateError(message) {
  const text = String(message || "").trim();
  if (!text) return "操作失败，请重试";
  if (/failed to fetch|network ?error|load failed|fetch failed/i.test(text)) {
    return "无法连接本地服务，请重新启动应用";
  }
  if (/timed? ?out|timeout/i.test(text)) return "操作超时，请重试";
  return text;
}

function toast(message, type = "") {
  const wrap = $("#toast-wrap");
  const el = document.createElement("div");
  el.className = `toast ${type}`;
  el.textContent = message;
  wrap.appendChild(el);
  setTimeout(() => {
    el.style.transition = "opacity .3s, transform .3s";
    el.style.opacity = "0";
    el.style.transform = "translateY(10px)";
    setTimeout(() => el.remove(), 320);
  }, 3200);
}

function tokenHeaders() {
  const token = window.__DS_TOKEN__ || "";
  return token ? { "X-DS-Token": token } : {};
}

async function api(path, body) {
  const isRead = body === undefined
    && (path.startsWith("/api/fs") || path === "/api/state"
      || path === "/api/models" || path === "/api/vram");
  const options = isRead ? {} : {
    method: "POST",
    headers: { "Content-Type": "application/json", ...tokenHeaders() },
    body: JSON.stringify(body || {}),
  };
  let resp;
  try {
    resp = await fetch(path, options);
  } catch (e) {
    throw new Error("无法连接本地服务，请重新启动应用");
  }
  let data = null;
  try { data = await resp.json(); } catch (e) { /* ignore */ }
  if (!resp.ok || (data && data.ok === false && !data.canceled)) {
    throw new Error(translateError((data && data.error) || `请求失败（${resp.status}）`));
  }
  return data || {};
}

/* ===================== 弹窗 ===================== */
function confirmDialog(title, text) {
  return new Promise((resolve) => {
    const mask = $("#modal-mask");
    $("#modal-title").textContent = title;
    $("#modal-text").textContent = text;
    mask.classList.remove("hidden");
    const done = (val) => {
      mask.classList.add("hidden");
      $("#modal-ok").onclick = null;
      $("#modal-cancel").onclick = null;
      resolve(val);
    };
    $("#modal-ok").onclick = () => done(true);
    $("#modal-cancel").onclick = () => done(false);
  });
}

/* ===================== 视图切换 ===================== */
let vramTimer = null;
function showView(name) {
  $$(".tab").forEach((t) => t.classList.toggle("active", t.dataset.view === name));
  $$(".view").forEach((v) => v.classList.toggle("active", v.id === `view-${name}`));
  if (name === "chat") {
    setTimeout(() => scrollToBottom(), 30);
  }
  if (vramTimer) {
    clearInterval(vramTimer);
    vramTimer = null;
  }
  if (name === "settings") {
    refreshVram();
    vramTimer = setInterval(refreshVram, 5000);
  }
}

/* ===================== 状态刷新 ===================== */
let refreshTimer = null;
function debouncedRefresh(delay = 500) {
  clearTimeout(refreshTimer);
  refreshTimer = setTimeout(refresh, delay);
}

async function refresh() {
  try {
    const data = await api("/api/state");
    S.state = data;
    S.catalog = data.catalog || [];
    render();
  } catch (err) {
    setStatus("error", "后端未连接");
  }
}

function setStatus(state, text) {
  const pill = $("#status-pill");
  pill.dataset.state = state;
  $("span", pill).textContent = text;
}

function render() {
  if (!S.state) return;
  const ol = S.state.ollama;
  const models = S.state.models || [];

  if (!ol.installed) setStatus("off", "未安装 Ollama");
  else if (ol.serveRunning) setStatus("ok", "Ollama 运行中");
  else setStatus("off", "服务未启动");

  $("#hero-missing").classList.toggle("hidden", ol.installed);

  renderModels(models);
  renderCatalog();
  renderHardware();
  renderSelectors(models);
  renderMemories(S.state.memories || []);
  renderSettings();

  const tasks = S.state.tasks || {};
  if (tasks.ollama) {
    paintProgress($("#install-progress"), tasks.ollama);
    paintProgress($("#settings-progress"), tasks.ollama);
  }
  if (tasks.model) paintProgress($("#model-progress"), tasks.model);
}

function renderModels(models) {
  const grid = $("#model-grid");
  grid.innerHTML = "";
  $("#models-empty").classList.toggle("hidden", models.length > 0);

  const total = models.reduce((sum, m) => sum + (m.size || 0), 0);
  $("#model-summary").textContent = models.length
    ? `${models.length} 个模型 · 共占用 ${fmtSize(total)}`
    : "尚未安装模型";

  models.forEach((m) => {
    const card = document.createElement("article");
    card.className = "model-card";
    const family = m.paramSize || m.family || "本地模型";
    card.innerHTML = `
      <div class="model-top">
        <div class="model-icon">DS</div>
        <span class="tag good">已就绪</span>
      </div>
      <h3>${escapeHtml(prettyName(m.name))}</h3>
      <p class="meta">${escapeHtml(family)} · ${fmtSize(m.size)}</p>
      <div class="card-actions">
        <button class="btn primary" data-chat-model="${escapeHtml(m.name)}">开始对话</button>
        <button class="btn ghost danger" data-del-model="${escapeHtml(m.name)}">删除</button>
      </div>`;
    grid.appendChild(card);
  });
}

function modelVramGB(item) {
  const match = String(item && item.vram ? item.vram : "").match(/([\d.]+)\s*GB/i);
  return match ? Number(match[1]) : 0;
}

function bestFitModel(maxVram) {
  if (!maxVram) return null;
  let fit = null;
  S.catalog.forEach((m) => {
    const need = modelVramGB(m);
    if (need && need <= maxVram && (!fit || need > modelVramGB(fit))) fit = m;
  });
  return fit;
}

function renderHardware() {
  const banner = $("#hw-banner");
  if (!banner) return;
  const hw = (S.state && S.state.hardware) || { gpus: [], maxVramGB: 0 };
  const gpu = (hw.gpus || [])[0];
  if (!gpu) {
    banner.classList.add("hidden");
    return;
  }
  banner.classList.remove("hidden");
  const fit = bestFitModel(hw.maxVramGB || 0);
  const vram = gpu.vramGB ? ` · 约 ${gpu.vramGB} GB 显存` : "";
  $("#hw-text").innerHTML = `检测到 <b>${escapeHtml(gpu.name)}</b>${escapeHtml(vram)}`
    + (fit
      ? `，按显存推荐 <b>${escapeHtml(prettyName(fit.name))}</b>（建议显存 ${escapeHtml(fit.vram)}）`
      : "，显存信息不足，建议从较小的模型开始试");
}

function fmtBytes(bytes) {
  const n = Number(bytes) || 0;
  if (n >= 1024 ** 3) return (n / 1024 ** 3).toFixed(2) + " GB";
  if (n >= 1024 ** 2) return (n / 1024 ** 2).toFixed(0) + " MB";
  if (n >= 1024) return (n / 1024).toFixed(0) + " KB";
  return n + " B";
}

async function refreshVram() {
  const box = $("#vram-list");
  if (!box) return;
  try {
    const data = await api("/api/vram");
    const total = Number(data.total) || 0;
    const used = Number(data.used) || 0;
    const ours = Number(data.appUsed) || 0;
    $("#vram-total").textContent = total ? fmtBytes(total) : "—";
    $("#vram-used").textContent = total
      ? `${fmtBytes(used)}（${Math.round((used * 100) / total)}%）`
      : fmtBytes(used);
    $("#vram-ours").textContent = fmtBytes(ours);
    const rows = (data.processes || []).slice(0, 10);
    box.innerHTML = rows.length
      ? rows.map((p) => {
        const name = String(p.name || "unknown").replace(/\.exe$/i, "");
        const tag = p.ours ? '<span class="v-tag">本应用</span>' : "";
        return `<div class="vram-row${p.ours ? " ours" : ""}">`
          + `<span class="v-name">${escapeHtml(name)}${tag}`
          + `<span class="muted"> · PID ${p.pid}</span></span>`
          + `<span class="v-bytes">${fmtBytes(p.bytes)}</span>`
          + "</div>";
      }).join("")
      : '<div class="muted">暂无进程占用显存</div>';
  } catch (err) {
    box.innerHTML = `<div class="muted">读取失败：${escapeHtml(err.message)}</div>`;
  }
}

function renderCatalog() {
  const box = $("#catalog-list");
  box.innerHTML = "";
  const hw = (S.state && S.state.hardware) || {};
  const fit = bestFitModel(hw.maxVramGB || 0);
  S.catalog.forEach((m) => {
    const card = document.createElement("div");
    card.className = "catalog-card" + (S.selected === m.name ? " selected" : "");
    const fitTag = fit && fit.name === m.name
      ? '<div class="c-fit">适合你的显卡</div>'
      : "";
    card.innerHTML = `
      <div class="c-check">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><path d="m5 12 5 5 9-11"/></svg>
      </div>
      <div class="c-name">${escapeHtml(m.name)}</div>
      ${fitTag}
      <div class="c-note">${escapeHtml(m.note || "")}</div>
      <div class="c-foot">
        <span class="c-left">
          <span class="c-size">${escapeHtml(m.size || "")}</span>
          <span class="c-src">${m.repo ? "国内镜像" : "官方源"}</span>
        </span>
        <span class="c-req">
          <span class="c-vram">建议显存 ${escapeHtml(m.vram || "—")}</span>
          <span class="c-ram">内存 ${escapeHtml(m.ram || "8 GB")}</span>
        </span>
      </div>`;
    card.onclick = () => {
      S.selected = m.name;
      $("#custom-model").value = "";
      renderCatalog();
    };
    box.appendChild(card);
  });
  $("#catalog-count").textContent = `${S.catalog.length} 个可选`;
}

function renderSelectors(models) {
  const select = $("#chat-model");
  const preferred = Chat.model || localStorage.getItem(MODEL_KEY) || "";
  select.innerHTML = "";
  if (!models.length) {
    const opt = document.createElement("option");
    opt.value = "";
    opt.textContent = "暂无可用模型";
    select.appendChild(opt);
    Chat.model = "";
    return;
  }
  models.forEach((m) => {
    const opt = document.createElement("option");
    opt.value = m.name;
    opt.textContent = prettyName(m.name);
    select.appendChild(opt);
  });
  const exists = models.some((m) => m.name === preferred);
  select.value = exists ? preferred : models[0].name;
  Chat.model = select.value;
  localStorage.setItem(MODEL_KEY, Chat.model);
}

function renderMemories(memories) {
  // 对话页记忆下拉
  const sel = $("#chat-memory");
  if (sel) {
    const preferred = Chat.memory || localStorage.getItem(MEMORY_KEY) || "";
    sel.innerHTML = '<option value="">不使用记忆</option>';
    memories.forEach((m) => {
      const opt = document.createElement("option");
      opt.value = m.name;
      opt.textContent = m.name.replace(/\.md$/i, "");
      sel.appendChild(opt);
    });
    const exists = memories.some((m) => m.name === preferred);
    sel.value = exists ? preferred : "";
    Chat.memory = sel.value;
    localStorage.setItem(MEMORY_KEY, Chat.memory);
  }
  // 设置页记忆列表
  const list = $("#memory-list");
  if (!list) return;
  if (!memories.length) {
    list.innerHTML = '<div class="muted">还没有记忆文件，点击「新建记忆」创建一个。</div>';
    return;
  }
  list.innerHTML = "";
  memories.forEach((m) => {
    const row = document.createElement("div");
    row.className = "memory-item";
    row.innerHTML = `
      <div class="memory-item-info">
        <span class="memory-name">${escapeHtml(m.name.replace(/\.md$/i, ""))}</span>
        <span class="memory-meta">${fmtSize(m.size || 0)}</span>
      </div>
      <div class="memory-item-actions">
        <button class="btn small" data-memory-edit="${escapeHtml(m.name)}">编辑</button>
        <button class="btn small ghost danger" data-memory-del="${escapeHtml(m.name)}">删除</button>
      </div>`;
    list.appendChild(row);
  });
}

function renderSettings() {
  const ol = S.state.ollama;
  const cfg = S.state.config;
  $("#set-status").textContent = !ol.installed
    ? "未安装"
    : ol.serveRunning ? "运行中" : "已停止";
  $("#set-kind").textContent = ol.isIntel ? "Intel 优化版"
    : ol.installed ? "标准版" : "未安装";
  $("#set-kind").className = "tag" + (ol.isIntel ? " good" : "");
  $("#set-version").textContent = ol.version || "—";
  $("#set-path").textContent = ol.path || "—";
  $("#set-host").textContent = ol.host || "—";
  $("#set-dir").textContent = cfg.modelDir || "—";
  $("#app-version").textContent = S.state.version || "1.0.0";
  const runtimeDir = $("#ollama-runtime-dir");
  if (runtimeDir) runtimeDir.value = ol.runtimeRoot || "—";
  renderOllamaChooser(ol, S.state.ollamaCandidates || []);
  if (document.activeElement !== $("#num-ctx")) {
    $("#num-ctx").value = cfg.numCtx || 8192;
  }
  $("#model-dir").value = cfg.modelDir || "";
  $("#intel-toggle").checked = !!cfg.intelMode;
  const memoryDir = $("#memory-dir");
  if (memoryDir) memoryDir.value = cfg.memoryDir || "";
  const relSwitch = $("#release-switch-toggle");
  if (relSwitch) relSwitch.checked = !!cfg.releaseOnSwitch;
  const relStop = $("#release-stop-toggle");
  if (relStop) relStop.checked = !!cfg.releaseOnStop;
  const restartSwitch = $("#restart-switch-toggle");
  if (restartSwitch) restartSwitch.checked = !!cfg.restartOnSwitch;
  const autoMem = $("#auto-memory-toggle");
  if (autoMem) {
    autoMem.checked = !!cfg.autoMemory;
    const tag = $("#auto-mem-tag");
    if (tag) {
      tag.textContent = cfg.autoMemory ? "开启" : "关闭";
      tag.className = "tag" + (cfg.autoMemory ? " good" : "");
    }
  }
  const updateToggle = $("#check-update-toggle");
  if (updateToggle) updateToggle.checked = !!cfg.checkUpdate;
  const updateCurrent = $("#update-current");
  if (updateCurrent) updateCurrent.textContent = S.state.version || "—";

  const notice = $("#mode-notice");
  if (ol.installed && ol.modeMismatch) {
    const need = ol.wantedKind === "intel" ? "Intel 优化版" : "标准版";
    notice.classList.remove("hidden");
    notice.textContent = `当前运行的是${ol.isIntel ? " Intel 优化版" : "标准版"}，与 Intel 模式设置不一致。请在下方安装${need}，安装完成后会自动切换。`;
  } else {
    notice.classList.add("hidden");
  }
}

function renderOllamaChooser(ol, candidates) {
  const pathInput = $("#ollama-exe-path");
  const select = $("#ollama-candidates");
  if (!pathInput || !select) return;
  pathInput.value = ol.path || "（未检测到）";
  const current = (ol.path || "").toLowerCase();
  select.innerHTML = "";
  if (!candidates.length) {
    const opt = document.createElement("option");
    opt.value = "";
    opt.textContent = "未检测到其它 Ollama";
    select.appendChild(opt);
    select.disabled = true;
    pendingOllama = "";
    renderApplyHint(ol);
    return;
  }
  select.disabled = false;
  candidates.forEach((c) => {
    const opt = document.createElement("option");
    const kindLabel = c.kind === "intel" ? "Intel 优化版" : "标准版";
    opt.value = c.path;
    opt.textContent = `${kindLabel} · ${c.path}`;
    select.appendChild(opt);
  });
  const pendingHit = pendingOllama
    ? candidates.find((c) => c.path.toLowerCase() === pendingOllama.toLowerCase())
    : null;
  if (pendingHit) {
    select.value = pendingHit.path;
  } else {
    const hit = candidates.find((c) => c.path.toLowerCase() === current);
    select.value = hit ? hit.path : "";
    if (!hit) {
      const opt = document.createElement("option");
      opt.value = "";
      opt.textContent = "（当前程序不在候选列表中）";
      select.insertBefore(opt, select.firstChild);
      select.value = "";
    }
  }
  renderApplyHint(ol);
}

function renderApplyHint(ol) {
  const hint = $("#ollama-apply-hint");
  if (!hint) return;
  const current = ((ol && ol.path) || "").toLowerCase();
  if (!pendingOllama) {
    hint.textContent = "选好后点「确定使用」才会切换（服务会自动重启）。";
    return;
  }
  if (pendingOllama.toLowerCase() === current) {
    hint.textContent = "所选版本就是当前正在使用的程序。";
    return;
  }
  hint.textContent = `待确认：${pendingOllama}，点「确定使用」后切换。`;
}

/* ===================== 进度显示 ===================== */
function paintProgress(container, p) {
  if (!container) return;
  const active = p && ACTIVE_STATUS.has(p.status);
  container.classList.toggle("hidden", !active);
  if (!active) return;

  const bar = $(".bar", container);
  const fill = $(".bar i", container);
  const pct = $(".progress-pct", container);
  const text = $(".ptext", container);
  const title = $(".progress-title", container);
  const percent = Math.max(0, Math.min(100, Number(p.percent) || 0));

  bar.classList.toggle("indeterminate", !percent);
  fill.style.width = percent ? `${percent}%` : "38%";
  pct.textContent = percent ? `${percent}%` : "处理中";
  text.textContent = p.message || "正在处理…";

  if (title) {
    if (p.task === "model" || p.model) {
      title.textContent = `正在下载 ${prettyName(p.model || "")}`;
    } else {
      title.textContent = "正在准备 Ollama 运行时";
    }
  }
}

/* ===================== 事件流 ===================== */
function connectEvents() {
  const es = new EventSource("/api/events");
  es.onmessage = (e) => {
    let ev;
    try { ev = JSON.parse(e.data); } catch (err) { return; }
    handleEvent(ev);
  };
}

function handleEvent(ev) {
  if (ev.task === "ollama") {
    paintProgress($("#install-progress"), ev);
    paintProgress($("#settings-progress"), ev);
    if (ev.status === "ready") debouncedRefresh(600);
    if (ev.status === "done" || ev.status === "error") {
      toast(ev.message, ev.status === "error" ? "error" : "ok");
      setTimeout(() => {
        $("#install-progress").classList.add("hidden");
        $("#settings-progress").classList.add("hidden");
      }, 2600);
      debouncedRefresh(900);
    }
  }
  if (ev.task === "model") {
    paintProgress($("#model-progress"), ev);
    if (ev.status === "done") {
      toast(ev.message, "ok");
      $("#btn-download").disabled = false;
      $("#model-progress").classList.add("hidden");
      debouncedRefresh(600);
    } else if (ev.status === "error") {
      toast(ev.message, "error");
      $("#btn-download").disabled = false;
    }
  }
  if (ev.task === "update") {
    paintProgress($("#update-progress"), ev);
    if (ev.status === "installing") toast(ev.message, "ok");
    if (ev.status === "error") {
      toast(ev.message, "error");
      $("#update-ok").disabled = false;
      $("#update-cancel").disabled = false;
    }
  }
}

/* ===================== 在线更新 ===================== */
let updateInfo = null;

async function checkUpdate(manual) {
  if (manual) toast("正在检查更新…");
  try {
    const res = await api("/api/update/check", {});
    if (!res.hasUpdate) {
      if (manual) toast(`已是最新版本 v${res.current}`, "ok");
      return;
    }
    updateInfo = res;
    $("#update-title").textContent = `发现新版本 v${res.latest}`;
    $("#update-text").innerHTML =
      `当前版本 v${escapeHtml(res.current)} → 新版本 <b>v${escapeHtml(res.latest)}</b><br>`
      + `安装包会通过国内加速节点下载，下载完成后自动启动安装程序，应用会退出。`;
    $("#update-progress").classList.add("hidden");
    $("#update-ok").disabled = false;
    $("#update-cancel").disabled = false;
    $("#update-cancel").textContent = "稍后再说";
    $("#update-mask").classList.remove("hidden");
  } catch (err) {
    if (manual) toast(err.message, "error");
  }
}

async function installUpdate() {
  if (!updateInfo) return;
  $("#update-ok").disabled = true;
  $("#update-cancel").disabled = true;
  $("#update-cancel").textContent = "下载中…";
  $("#update-progress").classList.remove("hidden");
  $(".progress-title", $("#update-progress")).textContent = `正在下载 v${updateInfo.latest}`;
  try {
    await api("/api/update/install", { version: updateInfo.latest });
  } catch (err) {
    toast(err.message, "error");
    $("#update-ok").disabled = false;
    $("#update-cancel").disabled = false;
    $("#update-cancel").textContent = "稍后再说";
  }
}

/* ===================== 首次启动引导 ===================== */
function maybeShowGuide() {
  if (!S.state || !S.state.config) return;
  if (S.state.config.firstRunDone) return;
  $("#guide-mask").classList.remove("hidden");
  api("/api/config", { firstRunDone: true }).catch(() => {});
}

/* ===================== 对话导出 ===================== */
function exportChat() {
  if (!Chat.messages.length) {
    toast("当前还没有对话内容", "error");
    return;
  }
  const lines = ["# DeepSeek 本地对话", "", `> 模型：${Chat.model || "未知"}`, ""];
  Chat.messages.forEach((m) => {
    const role = m.role === "user" ? "我" : "助手";
    const parsed = m.role === "assistant"
      ? splitThinking(m.content || "")
      : { thinking: "", content: m.content || "" };
    lines.push(`## ${role}`, "");
    if (parsed.thinking) lines.push("> 思考过程：", "", parsed.thinking, "");
    lines.push(parsed.content || "", "");
  });
  const stamp = new Date().toISOString().slice(0, 19).replace(/[:T]/g, "-");
  const blob = new Blob([lines.join("\n")], { type: "text/markdown;charset=utf-8" });
  const link = document.createElement("a");
  link.href = URL.createObjectURL(blob);
  link.download = `deepseek-chat-${stamp}.md`;
  link.click();
  setTimeout(() => URL.revokeObjectURL(link.href), 2000);
  toast("已导出当前对话", "ok");
}

/* ===================== 安装 / 下载 ===================== */
async function startInstall(mode) {
  if (!S.state) return;
  const ol = S.state.ollama;
  if (mode === "intel" && ol.installed && ol.isIntel) {
    toast("已经在使用 Intel 优化版");
    return;
  }
  if (mode === "standard" && ol.installed && !ol.isIntel) {
    toast("已经在使用标准版");
    return;
  }
  try {
    await api("/api/ollama/install", { mode });
    toast(`已开始下载${mode === "intel" ? " Intel 优化版" : "标准版"}，请稍候`, "ok");
    paintProgress($("#install-progress"), {
      status: "downloading", percent: 0, message: "正在连接国内镜像…",
    });
    showView("models");
  } catch (err) {
    toast(err.message, "error");
  }
}

async function startDownload() {
  const custom = $("#custom-model").value.trim();
  const model = custom || S.selected;
  if (!model) { toast("请先选择一个模型", "error"); return; }
  if (!S.state || !S.state.ollama.installed) {
    showView("models");
    toast("请先安装 Ollama 运行时", "error");
    return;
  }
  const btn = $("#btn-download");
  btn.disabled = true;
  try {
    await api("/api/models/pull", { model });
    paintProgress($("#model-progress"), {
      status: "pulling", percent: 0, model, message: "正在连接 ModelScope 国内镜像…",
    });
  } catch (err) {
    toast(err.message, "error");
    btn.disabled = false;
  }
}

/* ===================== 目录选择器 ===================== */
const DirPicker = { path: "", parent: "", drives: [], callback: null };

async function openDirPicker(start, callback) {
  DirPicker.callback = callback || null;
  $("#dir-mask").classList.remove("hidden");
  $("#dir-newrow").classList.add("hidden");
  $("#dir-newname").value = "";
  const fallback = S.state && S.state.config ? S.state.config.modelDir : "";
  await loadDir(start || fallback);
}

function closeDirPicker() {
  DirPicker.callback = null;
  $("#dir-mask").classList.add("hidden");
}

async function loadDir(path) {
  $("#dir-list").innerHTML = '<div class="dir-empty">正在读取…</div>';
  try {
    const data = await api(`/api/fs?path=${encodeURIComponent(path || "")}`);
    DirPicker.path = data.path || "";
    DirPicker.parent = data.parent || "";
    DirPicker.drives = data.drives || [];
    $("#dir-path").textContent = data.path || "—";
    $("#dir-free").textContent = data.total
      ? `可用空间 ${fmtSize(data.free)} / 共 ${fmtSize(data.total)}`
      : "";
    renderDrives();
    renderDirList(data);
  } catch (err) {
    $("#dir-list").innerHTML = `<div class="dir-empty">${escapeHtml(err.message)}</div>`;
  }
}

function renderDrives() {
  const box = $("#dir-drives");
  box.innerHTML = "";
  DirPicker.drives.forEach((d) => {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.textContent = d.name;
    btn.classList.toggle(
      "active",
      DirPicker.path.toUpperCase().startsWith(d.path.toUpperCase())
    );
    btn.onclick = () => loadDir(d.path);
    box.appendChild(btn);
  });
}

function renderDirList(data) {
  const box = $("#dir-list");
  box.innerHTML = "";
  const dirs = data.dirs || [];
  if (data.error) {
    const err = document.createElement("div");
    err.className = "dir-empty";
    err.textContent = data.error;
    box.appendChild(err);
  }
  if (!dirs.length && !data.error) {
    box.innerHTML = '<div class="dir-empty">该目录下没有子文件夹</div>';
    return;
  }
  dirs.forEach((d) => {
    const row = document.createElement("div");
    row.className = "dir-item";
    row.innerHTML = `
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7Z"/></svg>
      <span class="grow ellipsis">${escapeHtml(d.name)}</span>
      <span class="muted">进入 ›</span>`;
    row.onclick = () => loadDir(d.path);
    box.appendChild(row);
  });
}

async function confirmDirPicker() {
  const target = DirPicker.path;
  if (!target) return;
  const cb = DirPicker.callback;
  closeDirPicker();
  if (cb) {
    try {
      await cb(target);
    } catch (err) {
      toast(err.message, "error");
    }
    return;
  }
  try {
    await api("/api/config", { modelDir: target });
    toast("模型目录已更新，Ollama 服务正在重启", "ok");
    debouncedRefresh(1800);
  } catch (err) {
    toast(err.message, "error");
  }
}

async function createFolder() {
  const name = $("#dir-newname").value.trim();
  if (!name) { toast("请输入文件夹名称", "error"); return; }
  if (/[\\/:*?"<>|]/.test(name)) { toast("名称不能包含 \\ / : * ? \" < > |", "error"); return; }
  const base = DirPicker.path.replace(/[\\/]+$/, "");
  const sep = DirPicker.path.includes("\\") ? "\\" : "/";
  const target = `${base}${sep}${name}`;
  try {
    await api("/api/mkdir", { path: target });
    $("#dir-newrow").classList.add("hidden");
    $("#dir-newname").value = "";
    toast("文件夹已创建", "ok");
    await loadDir(target);
  } catch (err) {
    toast(err.message, "error");
  }
}

/* ===================== 记忆管理 ===================== */
const MemoryEdit = { name: "" };

async function refreshMemories() {
  try {
    const data = await api("/api/memories");
    S.state.memories = data.memories || [];
    renderMemories(S.state.memories);
  } catch (err) { /* ignore */ }
}

async function newMemory() {
  const name = prompt("请输入记忆名称", "我的记忆");
  if (!name) return;
  try {
    const res = await api("/api/memories", { action: "create", name, content: "" });
    if (res.name) {
      toast("记忆已创建", "ok");
      S.state.memories = res.memories || [];
      renderMemories(S.state.memories);
      openMemoryEditor(res.name);
    }
  } catch (err) {
    toast(err.message, "error");
  }
}

async function openMemoryEditor(name) {
  MemoryEdit.name = name;
  $("#memory-edit-title").textContent = `编辑：${name.replace(/\.md$/i, "")}`;
  try {
    const res = await api("/api/memories", { action: "read", name });
    $("#memory-edit-content").value = res.content || "";
  } catch (err) {
    $("#memory-edit-content").value = "";
  }
  $("#memory-mask").classList.remove("hidden");
}

function closeMemoryEditor() {
  MemoryEdit.name = "";
  $("#memory-edit-content").value = "";
  $("#memory-mask").classList.add("hidden");
}

async function saveMemoryEditor() {
  if (!MemoryEdit.name) return;
  const content = $("#memory-edit-content").value;
  try {
    await api("/api/memories", { action: "update", name: MemoryEdit.name, content });
    toast("记忆已保存", "ok");
    closeMemoryEditor();
    refreshMemories();
  } catch (err) {
    toast(err.message, "error");
  }
}

async function deleteMemory(name) {
  const ok = await confirmDialog("删除记忆", `确定要删除记忆「${name.replace(/\.md$/i, "")}」吗？`);
  if (!ok) return;
  try {
    const res = await api("/api/memories", { action: "delete", name });
    toast("记忆已删除", "ok");
    S.state.memories = res.memories || [];
    renderMemories(S.state.memories);
    if (Chat.memory === name) {
      Chat.memory = "";
      localStorage.setItem(MEMORY_KEY, "");
    }
  } catch (err) {
    toast(err.message, "error");
  }
}

/* ===================== 对话存储 ===================== */
function loadChat() {
  try {
    const raw = localStorage.getItem(CHAT_KEY);
    if (!raw) return;
    const data = JSON.parse(raw);
    if (data && Array.isArray(data.messages)) {
      Chat.messages = data.messages
        .filter((m) => m && (m.role === "user" || m.role === "assistant") && typeof m.content === "string")
        .slice(-MAX_HISTORY);
    }
    if (data && typeof data.model === "string") Chat.model = data.model;
  } catch (e) { /* ignore */ }
}

function saveChat() {
  try {
    const messages = Chat.messages
      .filter((m) => m.content || m.thinking)
      .slice(-MAX_HISTORY);
    localStorage.setItem(CHAT_KEY, JSON.stringify({ model: Chat.model, messages }));
  } catch (e) { /* ignore */ }
}

/* ===================== 对话渲染 ===================== */
function welcomeHtml() {
  return `
    <div class="chat-welcome">
      <div class="welcome-logo">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3c4.5 0 8 3.2 8 7.2 0 3.5-2.3 6.4-5.6 7.1L12 21l-2.4-3.7C6.3 16.6 4 13.7 4 10.2 4 6.2 7.5 3 12 3Z"/><path d="M9.2 9.6h.01M14.8 9.6h.01"/><path d="M9.5 13.2c1.6 1 3.4 1 5 0"/></svg>
      </div>
      <h3>开始和本地 DeepSeek 对话</h3>
      <p>模型完全运行在你的电脑上，对话内容不会上传网络。对话会自动保存在本机。</p>
      <div class="suggest">
        <button data-prompt="用通俗易懂的方式解释一下什么是量子纠缠">解释量子纠缠</button>
        <button data-prompt="帮我写一个 Python 脚本，批量重命名文件夹里的图片">写一段 Python 脚本</button>
        <button data-prompt="给我制定一个一周的健身计划，适合上班族">制定健身计划</button>
      </div>
    </div>`;
}

function isNearBottom() {
  const box = $("#chat-messages");
  return box.scrollHeight - box.scrollTop - box.clientHeight < 160;
}

function scrollToBottom(force = false) {
  const box = $("#chat-messages");
  if (force || isNearBottom()) box.scrollTop = box.scrollHeight;
}

function renderChatAll() {
  const box = $("#chat-messages");
  box.innerHTML = "";
  if (!Chat.messages.length) {
    box.innerHTML = welcomeHtml();
    return;
  }
  Chat.messages.forEach((m) => box.appendChild(buildMessageEl(m)));
  refreshMsgActions();
  box.scrollTop = box.scrollHeight;
}

function buildMessageEl(m) {
  const wrap = document.createElement("div");
  wrap.className = `msg ${m.role === "user" ? "user" : "ai"}`;
  const avatar = document.createElement("div");
  avatar.className = "avatar";
  avatar.textContent = m.role === "user" ? "我" : "DS";

  const main = document.createElement("div");
  main.className = "msg-main";
  const bubble = document.createElement("div");
  bubble.className = "bubble";
  main.appendChild(bubble);

  if (m.role === "assistant") {
    const foot = document.createElement("div");
    foot.className = "msg-foot";
    foot.innerHTML = `<button type="button" data-copy>复制</button>`
      + `<button type="button" data-regen>重新生成</button>`
      + `<span class="stats"></span>`;
    main.appendChild(foot);
    $(".stats", foot).textContent = m.stats ? `约 ${m.stats} tokens/s` : "";
  }

  wrap.appendChild(avatar);
  wrap.appendChild(main);
  const isLast = m === Chat.messages[Chat.messages.length - 1];
  updateBubble(bubble, m, Chat.busy && isLast);
  return wrap;
}

function refreshMsgActions() {
  const box = $("#chat-messages");
  const msgs = Array.from(box.querySelectorAll(".msg"));
  msgs.forEach((el, index) => {
    const btn = el.querySelector("[data-regen]");
    if (!btn) return;
    const isLast = index === msgs.length - 1
      && Chat.messages[index]
      && Chat.messages[index].role === "assistant";
    btn.style.display = isLast && !Chat.busy ? "" : "none";
  });
}

function appendMessageEl(m) {
  const box = $("#chat-messages");
  const stick = isNearBottom();
  const welcome = $(".chat-welcome", box);
  if (welcome) welcome.remove();
  const el = buildMessageEl(m);
  box.appendChild(el);
  refreshMsgActions();
  if (stick) box.scrollTop = box.scrollHeight;
  return el;
}

function splitThinking(text) {
  let content = String(text || "");
  let thinking = "";
  const openTag = "<" + "think>";
  const closeTag = "<" + "/think>";
  while (true) {
    const openIdx = content.indexOf(openTag);
    const closeIdx = content.indexOf(closeTag);
    if (openIdx >= 0 && closeIdx > openIdx) {
      thinking += (thinking ? "\n" : "") + content.slice(openIdx + openTag.length, closeIdx);
      content = content.slice(0, openIdx) + content.slice(closeIdx + closeTag.length);
      continue;
    }
    // 有时开头标签被模板吞掉，只剩闭合标签，此时前面都是思考内容
    if (openIdx < 0 && closeIdx >= 0) {
      thinking += (thinking ? "\n" : "") + content.slice(0, closeIdx);
      content = content.slice(closeIdx + closeTag.length);
      continue;
    }
    break;
  }
  // 流式输出中尚未闭合的思考内容
  const pending = content.indexOf(openTag);
  if (pending >= 0) {
    thinking += (thinking ? "\n" : "") + content.slice(pending + openTag.length);
    content = content.slice(0, pending);
  }
  return { thinking: thinking.trim(), content: content.trim() };
}

function updateBubble(bubble, m, showTyping) {
  if (m.role === "user") {
    bubble.textContent = m.content;
    return;
  }
  const parsed = splitThinking(m.content || "");
  let thinking = m.thinking || "";
  if (parsed.thinking) thinking = thinking ? `${thinking}\n${parsed.thinking}` : parsed.thinking;
  const content = parsed.content;

  let html = "";
  if (!content && !thinking && m.statusText) {
    html += `<span class="search-status"><i class="dot"></i>${escapeHtml(m.statusText)}</span>`;
  }
  if (thinking) {
    const open = !content && showTyping ? " open" : "";
    const label = content ? "思考过程" : "思考中…";
    html += `<details class="think"${open}><summary>${label}</summary>`
      + `<div class="think-body">${escapeHtml(thinking)}</div></details>`;
  }
  if (content) html += md(content);
  else if (showTyping && !m.statusText) html += '<span class="typing"><i></i><i></i><i></i></span>';
  if (m.sources && m.sources.length) html += sourcesHtml(m.sources);
  if (m.notice) html += `<p class="notice-line">${escapeHtml(m.notice)}</p>`;
  if (!html) html = '<span class="muted">…</span>';
  bubble.innerHTML = html;
}

function sourcesHtml(list) {
  const links = list.map((item, index) => {
    const url = String(item.url || "");
    if (!/^https?:\/\//i.test(url)) return "";
    let host = "";
    try { host = new URL(url).hostname.replace(/^www\./, ""); } catch (e) { /* ignore */ }
    return `<a href="${escapeHtml(url)}" target="_blank" rel="noreferrer">`
      + `[${index + 1}] ${escapeHtml(item.title || url)}`
      + (host ? `<span class="src-host">${escapeHtml(host)}</span>` : "")
      + "</a>";
  }).join("");
  return `<div class="sources"><div class="sources-title">联网参考来源</div>${links}</div>`;
}

function scheduleBubbleUpdate(bubble, m) {
  if (Chat.renderQueued) return;
  Chat.renderQueued = true;
  requestAnimationFrame(() => {
    Chat.renderQueued = false;
    const box = $("#chat-messages");
    const stick = box.scrollHeight - box.scrollTop - box.clientHeight < 180;
    updateBubble(bubble, m, true);
    if (stick) box.scrollTop = box.scrollHeight;
  });
}

function md(text) {
  if (!text) return "";
  const parts = String(text).split(/```/);
  let html = "";
  parts.forEach((part, i) => {
    if (i % 2 === 1) {
      const nl = part.indexOf("\n");
      const code = nl >= 0 ? part.slice(nl + 1) : part;
      html += `<pre><code>${escapeHtml(code.replace(/\n$/, ""))}</code></pre>`;
    } else {
      html += mdInline(part);
    }
  });
  return html;
}

function mdInline(text) {
  let s = escapeHtml(text);
  s = s.replace(/`([^`\n]+)`/g, "<code>$1</code>");
  s = s.replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>");
  s = s.replace(/^#{1,4}\s+(.*)$/gm, "<h4>$1</h4>");
  s = s.replace(/^&gt;\s?(.*)$/gm, "<blockquote>$1</blockquote>");
  s = s.replace(/((?:^|\n)[-*]\s+[^\n]*(?:\n[-*]\s+[^\n]*)*)/g, (block) => {
    const items = block.trim().split("\n")
      .map((line) => `<li>${line.replace(/^[-*]\s+/, "")}</li>`).join("");
    return `\n<ul>${items}</ul>\n`;
  });
  return s.split(/\n{2,}/).map((block) => {
    const b = block.trim();
    if (!b) return "";
    if (/^<(h4|ul|ol|pre|blockquote)/.test(b)) return b;
    return `<p>${b.replace(/\n/g, "<br>")}</p>`;
  }).join("");
}

function updateSendBtn() {
  const btn = $("#btn-send");
  btn.classList.toggle("stop", Chat.busy);
  $("span", btn).textContent = Chat.busy ? "停止" : "发送";
}

function renderChatPrefs() {
  $$("#think-seg [data-think]").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.think === Chat.thinkLevel);
  });
  $("#web-toggle").classList.toggle("on", Chat.webSearch);
}

function autoGrow(el) {
  el.style.height = "auto";
  el.style.height = `${Math.min(el.scrollHeight, 168)}px`;
}

/* ===================== 对话流程 ===================== */
function historyPayload(upTo) {
  const mapped = Chat.messages.slice(0, upTo).map((m) => ({
    role: m.role,
    content: m.role === "assistant" ? splitThinking(m.content || "").content : m.content,
  })).filter((m) => m.content);
  // 按上下文长度裁剪历史，避免长对话把 num_ctx 撑爆导致模型报错
  const numCtx = Number((S.state && S.state.config && S.state.config.numCtx) || 8192);
  const budget = Math.max(1500, Math.floor(numCtx * 0.75));
  const out = [];
  let used = 0;
  for (let i = mapped.length - 1; i >= 0; i--) {
    const len = (mapped[i].content || "").length;
    if (out.length >= 2 && used + len > budget) break;
    used += len;
    out.unshift(mapped[i]);
  }
  return out;
}

async function streamReply(history) {
  const assistant = { role: "assistant", content: "", thinking: "", stats: 0 };
  Chat.messages.push(assistant);
  const el = appendMessageEl(assistant);
  const bubble = $(".bubble", el);
  const statsEl = $(".stats", el);

  Chat.busy = true;
  updateSendBtn();
  refreshMsgActions();
  updateBubble(bubble, assistant, true);

  const controller = new AbortController();
  Chat.controller = controller;
  const startedAt = performance.now();

  try {
    const resp = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json", ...tokenHeaders() },
      body: JSON.stringify({
        model: Chat.model,
        messages: history,
        thinkLevel: Chat.thinkLevel,
        webSearch: Chat.webSearch,
        memory: Chat.memory,
      }),
      signal: controller.signal,
    });
    if (!resp.ok) {
      let message = await resp.text();
      try { message = JSON.parse(message).error || message; } catch (e) { /* ignore */ }
      throw new Error(translateError(message));
    }
    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buf = "";
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      let idx;
      while ((idx = buf.indexOf("\n\n")) >= 0) {
        const chunk = buf.slice(0, idx);
        buf = buf.slice(idx + 2);
        const dataLine = chunk.split("\n").find((l) => l.startsWith("data:"));
        if (!dataLine) continue;
        const payload = dataLine.slice(5).trim();
        if (!payload || payload === "[DONE]") continue;
        let obj;
        try { obj = JSON.parse(payload); } catch (e) { continue; }
        if (obj.error) {
          throw new Error(typeof obj.error === "string" ? obj.error : "生成失败，请重试");
        }
        if (obj.status) {
          assistant.statusText = obj.message || assistant.statusText || "";
          scheduleBubbleUpdate(bubble, assistant);
          continue;
        }
        if (obj.sources) {
          assistant.sources = obj.sources;
          scheduleBubbleUpdate(bubble, assistant);
          continue;
        }
        if (obj.notice) {
          assistant.notice = obj.notice;
          scheduleBubbleUpdate(bubble, assistant);
          continue;
        }
        const msg = obj.message || {};
        if (msg.thinking) assistant.thinking += msg.thinking;
        if (msg.content) assistant.content += msg.content;
        if (obj.done && obj.eval_count && obj.eval_duration) {
          assistant.stats = (obj.eval_count / (obj.eval_duration / 1e9)).toFixed(1);
        }
        scheduleBubbleUpdate(bubble, assistant);
      }
    }
  } catch (err) {
    if (err.name === "AbortError") {
      assistant.content += (assistant.content ? "\n\n" : "") + "> 已停止生成";
    } else {
      assistant.content += (assistant.content ? "\n\n" : "") + `> 出错了：${translateError(err.message)}`;
    }
  } finally {
    const seconds = (performance.now() - startedAt) / 1000;
    Chat.busy = false;
    Chat.controller = null;
    assistant.statusText = "";
    updateSendBtn();
    updateBubble(bubble, assistant, false);
    if (statsEl) {
      statsEl.textContent = assistant.stats
        ? `约 ${assistant.stats} tokens/s · 用时 ${seconds.toFixed(1)} 秒`
        : "";
    }
    refreshMsgActions();
    saveChat();
    scrollToBottom();
    debouncedRefresh(1200);
  }
}

async function sendMessage() {
  if (Chat.busy) { stopGeneration(); return; }
  const input = $("#chat-input");
  const text = input.value.trim();
  if (!text) return;
  if (!Chat.model) { toast("请先下载并选择一个模型", "error"); showView("download"); return; }
  if (!S.state || !S.state.ollama.installed) {
    toast("请先安装 Ollama 运行时", "error");
    showView("models");
    return;
  }
  if (!S.state.ollama.serveRunning) {
    toast("Ollama 服务未启动，正在自动启动，首次加载会稍慢…");
  }

  input.value = "";
  autoGrow(input);
  Chat.messages.push({ role: "user", content: text });
  appendMessageEl(Chat.messages[Chat.messages.length - 1]);
  saveChat();
  const history = historyPayload(Chat.messages.length);
  await streamReply(history);
}

async function regenerate() {
  if (Chat.busy) { toast("正在生成回复，请稍候", "error"); return; }
  const last = Chat.messages[Chat.messages.length - 1];
  if (!last || last.role !== "assistant") return;
  if (!Chat.model) { toast("请先选择一个模型", "error"); return; }
  Chat.messages.pop();
  saveChat();
  renderChatAll();
  const history = historyPayload(Chat.messages.length);
  if (!history.length) return;
  await streamReply(history);
}

function stopGeneration() {
  if (Chat.controller) {
    try { Chat.controller.abort(); } catch (e) { /* ignore */ }
  }
}

function resetChat(model) {
  if (Chat.controller) {
    try { Chat.controller.abort(); } catch (e) { /* ignore */ }
  }
  Chat.busy = false;
  Chat.controller = null;
  Chat.messages = [];
  if (model !== undefined && model !== null) Chat.model = model;
  saveChat();
  renderChatAll();
  updateSendBtn();
}

async function openChatWithModel(name) {
  if (Chat.busy) stopGeneration();
  if (Chat.messages.length && Chat.model !== name) {
    const ok = await confirmDialog(
      "切换模型",
      `将使用 ${prettyName(name)} 开始新对话，当前对话会被清空，是否继续？`
    );
    if (!ok) { showView("chat"); return; }
    resetChat(name);
  } else if (!Chat.messages.length) {
    Chat.model = name;
    const select = $("#chat-model");
    if (select) select.value = name;
    localStorage.setItem(MODEL_KEY, name);
    saveChat();
  }
  showView("chat");
  setTimeout(() => $("#chat-input").focus(), 120);
}

/* ===================== 绑定 ===================== */
function bind() {
  $$(".tab").forEach((tab) => {
    tab.addEventListener("click", () => showView(tab.dataset.view));
  });
  $("#btn-settings").addEventListener("click", () => showView("settings"));
  $("#status-pill").addEventListener("click", () => showView("settings"));

  document.addEventListener("click", async (e) => {
    const goto = e.target.closest("[data-goto]");
    if (goto) { showView(goto.dataset.goto); return; }

    const installBtn = e.target.closest("[data-install]");
    if (installBtn) { startInstall(installBtn.dataset.install); return; }

    const chatBtn = e.target.closest("[data-chat-model]");
    if (chatBtn) { openChatWithModel(chatBtn.dataset.chatModel); return; }

    const delBtn = e.target.closest("[data-del-model]");
    if (delBtn) {
      const name = delBtn.dataset.delModel;
      const ok = await confirmDialog(
        "删除模型",
        `确定要删除 ${prettyName(name)} 吗？模型文件会从磁盘移除，之后需要重新下载。`
      );
      if (!ok) return;
      try {
        await api("/api/models/delete", { model: name });
        toast("模型已删除", "ok");
        if (Chat.model === name) resetChat("");
        refresh();
      } catch (err) {
        toast(err.message, "error");
      }
    }

    const memEditBtn = e.target.closest("[data-memory-edit]");
    if (memEditBtn) { openMemoryEditor(memEditBtn.dataset.memoryEdit); return; }

    const memDelBtn = e.target.closest("[data-memory-del]");
    if (memDelBtn) { deleteMemory(memDelBtn.dataset.memoryDel); return; }
  });

  $("#btn-use-existing").addEventListener("click", chooseExistingOllama);
  $("#btn-choose-dir").addEventListener("click", () => openDirPicker());
  $("#btn-download").addEventListener("click", startDownload);
  $("#btn-open-dir").addEventListener("click", () => {
    api("/api/open", { path: S.state ? S.state.config.modelDir : "" })
      .catch((err) => toast(err.message, "error"));
  });

  $("#intel-toggle").addEventListener("change", async (e) => {
    const on = e.target.checked;
    const ol = S.state ? S.state.ollama : null;
    if (on && ol && ol.installed && !ol.isIntel) {
      const ok = await confirmDialog(
        "启用 Intel 显卡优化",
        "需要下载 Intel 优化版 Ollama（约 101 MB，来自 ModelScope 国内镜像），是否现在下载并切换？"
      );
      if (!ok) { e.target.checked = false; return; }
      startInstall("intel");
      return;
    }
    if (!on && ol && ol.installed && ol.isIntel) {
      const ok = await confirmDialog(
        "切换为常规 Ollama",
        "关闭 Intel 优化模式需要下载 Ollama 标准版（约 1.6 GB，国内加速镜像），是否现在下载并切换？"
      );
      if (!ok) { e.target.checked = true; return; }
      startInstall("standard");
      return;
    }
    try {
      await api("/api/config", { intelMode: on });
      toast(on ? "已开启 Intel 显卡优化" : "已关闭 Intel 显卡优化", "ok");
      debouncedRefresh(1500);
    } catch (err) {
      toast(err.message, "error");
      e.target.checked = !on;
    }
  });

  $("#chat-model").addEventListener("change", async (e) => {
    const next = e.target.value;
    if (!next) return;
    if (Chat.messages.length && next !== Chat.model) {
      const ok = await confirmDialog(
        "切换模型",
        "切换模型会开始新的对话，当前对话将被清空，是否继续？"
      );
      if (!ok) { e.target.value = Chat.model; return; }
      resetChat(next);
    } else {
      Chat.model = next;
    }
    localStorage.setItem(MODEL_KEY, Chat.model);
    saveChat();
  });

  $("#chat-memory").addEventListener("change", (e) => {
    Chat.memory = e.target.value;
    localStorage.setItem(MEMORY_KEY, Chat.memory);
    if (Chat.memory) {
      toast(`已选择记忆：${Chat.memory.replace(/\.md$/i, "")}`, "ok");
    } else {
      toast("已取消使用记忆", "ok");
    }
  });

  $("#btn-send").addEventListener("click", sendMessage);
  $("#think-seg").addEventListener("click", (e) => {
    const btn = e.target.closest("[data-think]");
    if (!btn) return;
    Chat.thinkLevel = btn.dataset.think;
    localStorage.setItem("ds.thinkLevel", Chat.thinkLevel);
    renderChatPrefs();
  });
  $("#web-toggle").addEventListener("click", () => {
    Chat.webSearch = !Chat.webSearch;
    localStorage.setItem("ds.webSearch", Chat.webSearch ? "1" : "0");
    renderChatPrefs();
    toast(
      Chat.webSearch
        ? "已开启联网搜索，提问会先发送到必应检索资料"
        : "已关闭联网搜索",
      "ok"
    );
  });
  $("#btn-new-chat").addEventListener("click", async () => {
    if (Chat.busy) { toast("正在生成回复，请先停止", "error"); return; }
    if (Chat.messages.length) {
      const ok = await confirmDialog("新对话", "确定清空当前对话吗？");
      if (!ok) return;
    }
    resetChat();
    toast("已开始新对话", "ok");
  });

  const input = $("#chat-input");
  input.addEventListener("input", () => autoGrow(input));
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
      e.preventDefault();
      sendMessage();
    }
  });

  $("#chat-messages").addEventListener("click", async (e) => {
    const promptBtn = e.target.closest("[data-prompt]");
    if (promptBtn) {
      $("#chat-input").value = promptBtn.dataset.prompt;
      sendMessage();
      return;
    }
    const copyBtn = e.target.closest("[data-copy]");
    if (copyBtn) {
      const box = $("#chat-messages");
      const index = Array.from(box.querySelectorAll(".msg")).indexOf(copyBtn.closest(".msg"));
      const msg = Chat.messages[index];
      if (!msg) return;
      const text = msg.role === "assistant" ? splitThinking(msg.content || "").content : msg.content;
      try {
        await navigator.clipboard.writeText(text || "");
        toast("已复制到剪贴板", "ok");
      } catch (err) {
        toast("复制失败，请手动选择文本", "error");
      }
      return;
    }
    const regenBtn = e.target.closest("[data-regen]");
    if (regenBtn) regenerate();
  });

  $("#dir-close").addEventListener("click", closeDirPicker);
  $("#dir-pick").addEventListener("click", confirmDirPicker);
  $("#dir-new").addEventListener("click", () => {
    $("#dir-newrow").classList.remove("hidden");
    $("#dir-newname").focus();
  });
  $("#dir-create").addEventListener("click", createFolder);
  $("#dir-newname").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.isComposing) createFolder();
  });
  $("#dir-up").addEventListener("click", () => {
    if (DirPicker.parent) loadDir(DirPicker.parent);
  });
  $("#dir-mask").addEventListener("click", (e) => {
    if (e.target === $("#dir-mask")) closeDirPicker();
  });

  $("#btn-restart").addEventListener("click", async () => {
    try {
      await api("/api/ollama/restart");
      toast("正在重启 Ollama 服务…", "ok");
      debouncedRefresh(2400);
    } catch (err) { toast(err.message, "error"); }
  });
  $("#btn-stop").addEventListener("click", async () => {
    try {
      await api("/api/ollama/stop");
      toast("Ollama 服务已停止", "ok");
      debouncedRefresh(400);
    } catch (err) { toast(err.message, "error"); }
  });
  $("#btn-log").addEventListener("click", () => {
    const logPath = (S.state && S.state.logPath) || "";
    if (!logPath) { toast("暂无日志", "error"); return; }
    api("/api/open", { path: logPath }).catch((err) => toast(err.message, "error"));
  });
  $("#btn-reinstall-intel").addEventListener("click", () => startInstall("intel"));
  $("#btn-reinstall-standard").addEventListener("click", () => startInstall("standard"));
  $("#btn-save-ctx").addEventListener("click", async () => {
    try {
      await api("/api/config", { numCtx: Number($("#num-ctx").value) || 8192 });
      toast("参数已保存，服务正在重启", "ok");
      debouncedRefresh(1800);
    } catch (err) { toast(err.message, "error"); }
  });

  // 显存释放开关
  $("#restart-switch-toggle").addEventListener("change", async (e) => {
    try {
      await api("/api/config", { restartOnSwitch: e.target.checked });
      toast(e.target.checked ? "已开启：切换模型时自动重启 Ollama" : "已关闭：切换模型时自动重启 Ollama", "ok");
      debouncedRefresh(300);
    } catch (err) { toast(err.message, "error"); e.target.checked = !e.target.checked; }
  });
  $("#release-switch-toggle").addEventListener("change", async (e) => {
    try {
      await api("/api/config", { releaseOnSwitch: e.target.checked });
      toast(e.target.checked ? "已开启：切换模型时释放显存" : "已关闭：切换模型时释放显存", "ok");
      debouncedRefresh(300);
    } catch (err) { toast(err.message, "error"); e.target.checked = !e.target.checked; }
  });
  $("#release-stop-toggle").addEventListener("change", async (e) => {
    try {
      await api("/api/config", { releaseOnStop: e.target.checked });
      toast(e.target.checked ? "已开启：停止服务时释放显存" : "已关闭：停止服务时释放显存", "ok");
      debouncedRefresh(300);
    } catch (err) { toast(err.message, "error"); e.target.checked = !e.target.checked; }
  });

  // 选择使用哪个 Ollama：选好后需点「确定使用」才切换
  $("#btn-choose-ollama").addEventListener("click", async () => {
    try {
      const res = await api("/api/pick", { kind: "file" });
      if (!res.path) return;
      if (!/ollama.*\.exe$/i.test(res.path)) {
        toast("请选择 ollama.exe", "error");
        return;
      }
      pendingOllama = res.path;
      renderOllamaChooser(S.state.ollama || {}, S.state.ollamaCandidates || []);
      toast("已选择，点「确定使用」后切换", "ok");
    } catch (err) { toast(err.message, "error"); }
  });
  $("#btn-use-bundled").addEventListener("click", async () => {
    try {
      pendingOllama = "";
      await api("/api/ollama/use", { path: "" });
      toast("已恢复使用内置/自动检测的 Ollama", "ok");
      debouncedRefresh(2200);
    } catch (err) { toast(err.message, "error"); }
  });
  $("#ollama-candidates").addEventListener("change", (e) => {
    pendingOllama = e.target.value || "";
    renderApplyHint(S.state.ollama || {});
  });
  $("#btn-apply-ollama").addEventListener("click", async () => {
    const target = pendingOllama || $("#ollama-candidates").value || "";
    if (!target) {
      toast("请先选择一个 Ollama 程序", "error");
      return;
    }
    const current = (S.state && S.state.ollama && S.state.ollama.path) || "";
    if (target.toLowerCase() === current.toLowerCase()) {
      toast("当前已经在使用这个版本", "ok");
      return;
    }
    try {
      await api("/api/ollama/use", { path: target });
      pendingOllama = "";
      toast("已切换 Ollama 程序，服务正在重启", "ok");
      debouncedRefresh(2200);
    } catch (err) { toast(err.message, "error"); }
  });
  $("#btn-open-runtime").addEventListener("click", () => {
    const path = (S.state && S.state.ollama && S.state.ollama.runtimeRoot) || "";
    if (!path) { toast("暂未确定存放目录", "error"); return; }
    api("/api/open", { path }).catch((err) => toast(err.message, "error"));
  });

  // 自动保留记忆开关
  $("#auto-memory-toggle").addEventListener("change", async (e) => {
    try {
      await api("/api/config", { autoMemory: e.target.checked });
      toast(e.target.checked ? "已开启自动保留记忆" : "已关闭自动保留记忆", "ok");
      debouncedRefresh(300);
    } catch (err) { toast(err.message, "error"); e.target.checked = !e.target.checked; }
  });

  // 记忆目录
  $("#btn-choose-memory-dir").addEventListener("click", () => {
    const start = (S.state && S.state.config && S.state.config.memoryDir) || "";
    openDirPicker(start, async (target) => {
      await api("/api/config", { memoryDir: target });
      toast("记忆目录已更新", "ok");
      debouncedRefresh(300);
      refreshMemories();
    });
  });
  $("#btn-open-memory-dir").addEventListener("click", () => {
    const path = (S.state && S.state.config && S.state.config.memoryDir) || "";
    if (!path) { toast("未设置记忆目录", "error"); return; }
    api("/api/open", { path }).catch((err) => toast(err.message, "error"));
  });
  $("#btn-save-memory-cfg").addEventListener("click", async () => {
    try {
      await api("/api/config", { memoryDir: $("#memory-dir").value });
      toast("记忆设置已保存", "ok");
      debouncedRefresh(300);
      refreshMemories();
    } catch (err) { toast(err.message, "error"); }
  });
  $("#btn-new-memory").addEventListener("click", newMemory);

  // 在线更新
  $("#btn-check-update").addEventListener("click", () => checkUpdate(true));
  $("#btn-refresh-vram").addEventListener("click", refreshVram);
  $("#check-update-toggle").addEventListener("change", async (e) => {
    try {
      await api("/api/config", { checkUpdate: e.target.checked });
      toast(e.target.checked ? "已开启：启动时检查更新" : "已关闭：启动时检查更新", "ok");
      debouncedRefresh(300);
    } catch (err) { toast(err.message, "error"); e.target.checked = !e.target.checked; }
  });
  $("#update-ok").addEventListener("click", installUpdate);
  $("#update-cancel").addEventListener("click", () => {
    $("#update-mask").classList.add("hidden");
    $("#update-cancel").disabled = false;
  });

  // 首次启动引导
  $("#guide-ok").addEventListener("click", () => {
    $("#guide-mask").classList.add("hidden");
  });
  $("#guide-settings").addEventListener("click", () => {
    $("#guide-mask").classList.add("hidden");
    showView("settings");
  });

  // 导出对话 / 快捷键
  $("#btn-export-chat").addEventListener("click", exportChat);
  document.addEventListener("keydown", (e) => {
    if ((e.ctrlKey || e.metaKey) && !e.shiftKey && !e.altKey
      && (e.key === "n" || e.key === "N") && !e.isComposing) {
      e.preventDefault();
      $("#btn-new-chat").click();
    }
  });

  // 记忆编辑弹窗
  $("#memory-close").addEventListener("click", closeMemoryEditor);
  $("#memory-cancel").addEventListener("click", closeMemoryEditor);
  $("#memory-save").addEventListener("click", saveMemoryEditor);
  $("#memory-mask").addEventListener("click", (e) => {
    if (e.target === $("#memory-mask")) closeMemoryEditor();
  });

  $("#btn-quit").addEventListener("click", async () => {
    const ok = await confirmDialog("退出应用", "退出后 Ollama 服务也会停止，确定退出吗？");
    if (!ok) return;
    try { await api("/api/quit"); } catch (e) { /* ignore */ }
    document.body.innerHTML = `
      <div style="min-height:100vh;display:grid;place-items:center;text-align:center;padding:24px">
        <div>
          <h2 style="font-size:22px;margin-bottom:10px">应用已退出</h2>
          <p style="color:#8f9dbf;font-size:14px">Ollama 服务已停止，现在可以关闭此窗口。</p>
        </div>
      </div>`;
  });
}

async function chooseExistingOllama() {
  try {
    const res = await api("/api/pick", { kind: "file" });
    if (!res.path) return;
    await api("/api/ollama/use", { path: res.path });
    toast("已切换 Ollama 程序，正在启动服务", "ok");
    debouncedRefresh(1800);
  } catch (err) {
    toast(err.message, "error");
  }
}

/* ===================== 初始化 ===================== */
async function init() {
  bind();
  loadChat();
  renderChatPrefs();
  connectEvents();
  await refresh();
  renderChatAll();
  autoGrow($("#chat-input"));
  updateSendBtn();
  maybeShowGuide();
  if (S.state && S.state.config && S.state.config.checkUpdate) {
    setTimeout(() => checkUpdate(false), 1500);
  }
}

document.addEventListener("DOMContentLoaded", init);
