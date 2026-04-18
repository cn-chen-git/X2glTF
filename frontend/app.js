const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

const api = async (path, opts = {}) => {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
  });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`${res.status} ${res.statusText}: ${text}`);
  }
  return res.json();
};

const logBox = $("#log");
const stats = $("#stats");
const progressBar = $("#progressBar");
const btnRun = $("#btnRun");
const btnRunLabel = $("#btnRunLabel");
const btnInstall = $("#btnInstall");
const assimpBadge = $("#assimpBadge");
const scanBadge = $("#scanBadge");
const toast = $("#toast");
const hint = $("#hint");

let scanTimer = null;
let running = false;
let installing = false;

const showToast = (msg, kind = "info") => {
  toast.textContent = msg;
  toast.classList.add("toast-show");
  toast.classList.remove("text-danger", "text-success", "text-warn", "text-brand-700");
  if (kind === "error") toast.classList.add("text-danger");
  else if (kind === "ok") toast.classList.add("text-success");
  else if (kind === "warn") toast.classList.add("text-warn");
  else toast.classList.add("text-brand-700");
  clearTimeout(toast._timer);
  toast._timer = setTimeout(() => toast.classList.remove("toast-show"), 2500);
};

const appendLog = (msg, cls = "") => {
  const line = document.createElement("div");
  line.className = `line ${cls}`;
  line.textContent = msg;
  logBox.appendChild(line);
  logBox.scrollTop = logBox.scrollHeight;
};

const pickFolder = async (target) => {
  try {
    const title = target === "input" ? "选择输入目录" : "选择输出目录";
    const r = await api("/api/pick-folder", {
      method: "POST",
      body: JSON.stringify({ title }),
    });
    if (r.path) {
      $(`#${target}Dir`).value = r.path;
      triggerScan();
    }
  } catch (e) {
    showToast("无法打开目录选择器: " + e.message, "error");
  }
};

const collectConfig = () => ({
  input_dir: $("#inputDir").value.trim(),
  output_dir: $("#outputDir").value.trim(),
  output_format: $("#outputFormat").value,
  recursive: $("#recursive").checked,
  overwrite: $("#overwrite").checked,
  axis_up: $("#axisUp").value,
  flip_handedness: $("#flipHandedness").checked,
  global_scale: parseFloat($("#globalScale").value) || 1.0,
  join_identical_vertices: $("#joinVertices").checked,
  generate_normals: $("#genNormals").checked,
  generate_smooth_normals: $("#genSmoothNormals").checked,
  calc_tangent_space: $("#calcTangent").checked,
  triangulate: $("#triangulate").checked,
  limit_bone_weights: $("#limitBoneWeights").checked,
  improve_cache_locality: $("#improveCache").checked,
  keep_single_animation: $("#keepAnim").value.trim() || null,
  embed_textures: $("#embedTextures").checked,
  copy_textures_for_gltf: $("#copyTextures").checked,
  workers: parseInt($("#workers").value, 10) || 4,
});

const refreshAssimp = async () => {
  try {
    const s = await api("/api/assimp/status");
    if (s.running || installing) {
      const pct = Math.round((s.progress || 0) * 100);
      assimpBadge.innerHTML = `<span class="h-1.5 w-1.5 rounded-full bg-warn animate-pulse"></span><span>安装中 ${pct}%</span>`;
      assimpBadge.className = "inline-flex items-center gap-2 rounded-full bg-amber-50 px-3 py-1.5 text-xs font-medium text-warn ring-1 ring-warn/40";
    } else if (s.installed) {
      assimpBadge.innerHTML = `<span class="h-1.5 w-1.5 rounded-full bg-success"></span><span>Assimp 就绪</span>`;
      assimpBadge.className = "inline-flex items-center gap-2 rounded-full bg-emerald-50 px-3 py-1.5 text-xs font-medium text-success ring-1 ring-success/40";
    } else {
      assimpBadge.innerHTML = `<span class="h-1.5 w-1.5 rounded-full bg-danger"></span><span>未安装</span>`;
      assimpBadge.className = "inline-flex items-center gap-2 rounded-full bg-red-50 px-3 py-1.5 text-xs font-medium text-danger ring-1 ring-danger/40";
    }
  } catch (e) {
    console.error(e);
  }
};

const triggerScan = () => {
  clearTimeout(scanTimer);
  scanTimer = setTimeout(async () => {
    const dir = $("#inputDir").value.trim();
    if (!dir) {
      scanBadge.textContent = "—";
      return;
    }
    try {
      const r = await api(`/api/scan?dir=${encodeURIComponent(dir)}&recursive=${$("#recursive").checked}`);
      scanBadge.textContent = r.exists ? `发现 ${r.count} 个 .x 文件` : "目录不存在";
      scanBadge.className = "font-mono text-xs " + (r.exists && r.count ? "text-brand-600" : "text-ink-dim");
    } catch {
      scanBadge.textContent = "扫描失败";
      scanBadge.className = "font-mono text-xs text-danger";
    }
  }, 220);
};

const startInstall = async (force = false) => {
  if (installing) return;
  installing = true;
  btnInstall.disabled = true;
  appendLog(">>> 安装 / 更新 Assimp 原生库", "muted");
  try {
    await api(`/api/assimp/install?force=${force}`, { method: "POST" });
    let last = "";
    while (true) {
      const s = await api("/api/assimp/status");
      refreshAssimp();
      if (s.message && s.message !== last) {
        const pct = Math.round((s.progress || 0) * 100);
        appendLog(`  [${String(pct).padStart(3, " ")}%] ${s.message}`);
        last = s.message;
      }
      if (!s.running) {
        if (s.error) {
          appendLog(`<<< 安装失败: ${s.error}`, "fail");
          showToast("Assimp 安装失败", "error");
        } else {
          appendLog("<<< Assimp 安装完成", "ok");
          showToast("Assimp 安装完成", "ok");
        }
        break;
      }
      await new Promise((r) => setTimeout(r, 250));
    }
  } catch (e) {
    appendLog(`ERROR: ${e.message}`, "fail");
  } finally {
    installing = false;
    btnInstall.disabled = false;
    refreshAssimp();
  }
};

const startConvert = async () => {
  if (running) return;
  const cfg = collectConfig();
  if (!cfg.input_dir || !cfg.output_dir) {
    showToast("请先选择输入与输出目录", "warn");
    return;
  }
  running = true;
  btnRun.disabled = true;
  btnRunLabel.innerHTML = `<span class="spinner"></span> 转换中…`;
  progressBar.style.width = "0%";
  stats.textContent = "启动…";
  hint.textContent = "转换过程中请勿修改目录内容";

  try {
    const { job_id } = await api("/api/convert", {
      method: "POST",
      body: JSON.stringify(cfg),
    });
    appendLog(`>>> 开始批量转换 job=${job_id.slice(0, 8)}`, "muted");

    const es = new EventSource(`/api/convert/${job_id}/stream`);
    es.onmessage = (ev) => {
      const payload = JSON.parse(ev.data);
      const { snapshot, events } = payload;
      if (snapshot.total) {
        const pct = Math.round((snapshot.done / snapshot.total) * 100);
        progressBar.style.width = pct + "%";
      }
      stats.textContent = `${snapshot.done}/${snapshot.total || "?"} · ok ${snapshot.ok} · fail ${snapshot.failed}`;
      for (const ev of events) {
        const name = ev.src ? ev.src.replace(/.*[\\/]/, "") : "";
        const tag = ev.msg.startsWith("FAIL") ? "fail" : ev.msg === "ok" ? "ok" : "";
        appendLog(
          `[${String(ev.done).padStart(3, " ")}/${String(ev.total).padStart(3, " ")}] ${name} → ${ev.msg}`,
          tag
        );
      }
      if (snapshot.finished) {
        es.close();
        running = false;
        btnRun.disabled = false;
        btnRunLabel.innerHTML = `<svg class="h-4 w-4" viewBox="0 0 24 24" fill="currentColor"><path d="M8 5v14l11-7z"/></svg> 再次运行`;
        hint.textContent = `完成：成功 ${snapshot.ok} · 失败 ${snapshot.failed}`;
        showToast(
          `完成：成功 ${snapshot.ok}，失败 ${snapshot.failed}`,
          snapshot.failed ? "warn" : "ok"
        );
      }
    };
    es.onerror = () => {
      es.close();
      running = false;
      btnRun.disabled = false;
      btnRunLabel.innerHTML = `<svg class="h-4 w-4" viewBox="0 0 24 24" fill="currentColor"><path d="M8 5v14l11-7z"/></svg> 开始批量转换`;
      appendLog("ERROR: 事件流中断", "fail");
    };
  } catch (e) {
    running = false;
    btnRun.disabled = false;
    btnRunLabel.innerHTML = `<svg class="h-4 w-4" viewBox="0 0 24 24" fill="currentColor"><path d="M8 5v14l11-7z"/></svg> 开始批量转换`;
    appendLog(`ERROR: ${e.message}`, "fail");
    showToast(e.message, "error");
  }
};

const enhanceSelect = (selectEl) => {
  if (selectEl.dataset.csEnhanced === "1") return;
  selectEl.dataset.csEnhanced = "1";

  const wrap = document.createElement("div");
  wrap.className = "cs-wrap " + (selectEl.dataset.wrapClass || "w-full");

  const trigger = document.createElement("button");
  trigger.type = "button";
  trigger.className = "cs-trigger";
  trigger.setAttribute("aria-haspopup", "listbox");
  trigger.setAttribute("aria-expanded", "false");
  trigger.innerHTML = `
    <span class="cs-label"></span>
    <svg class="cs-caret" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">
      <path fill-rule="evenodd" d="M5.293 7.293a1 1 0 011.414 0L10 10.586l3.293-3.293a1 1 0 111.414 1.414l-4 4a1 1 0 01-1.414 0l-4-4a1 1 0 010-1.414z" clip-rule="evenodd"/>
    </svg>`;

  const menu = document.createElement("div");
  menu.className = "cs-menu";
  menu.setAttribute("role", "listbox");

  const opts = Array.from(selectEl.options);
  opts.forEach((o, i) => {
    const item = document.createElement("div");
    item.className = "cs-option";
    item.setAttribute("role", "option");
    item.dataset.value = o.value;
    item.textContent = o.textContent;
    if (o.selected) item.setAttribute("aria-selected", "true");
    item.addEventListener("click", () => {
      setValue(o.value);
      close();
    });
    menu.appendChild(item);
  });

  const parent = selectEl.parentNode;
  parent.replaceChild(wrap, selectEl);
  selectEl.classList.add("cs-native");
  wrap.appendChild(selectEl);
  wrap.appendChild(trigger);
  wrap.appendChild(menu);

  const labelEl = trigger.querySelector(".cs-label");

  const syncLabel = () => {
    const sel = selectEl.options[selectEl.selectedIndex];
    labelEl.textContent = sel ? sel.textContent : "";
    Array.from(menu.children).forEach((item) => {
      if (item.dataset.value === selectEl.value)
        item.setAttribute("aria-selected", "true");
      else item.removeAttribute("aria-selected");
    });
  };

  const setValue = (val) => {
    if (selectEl.value === val) return;
    selectEl.value = val;
    selectEl.dispatchEvent(new Event("change", { bubbles: true }));
    syncLabel();
  };

  const open = () => {
    wrap.classList.add("open");
    trigger.setAttribute("aria-expanded", "true");
    document.addEventListener("click", outsideClick, true);
    document.addEventListener("keydown", onKey);
    const active = menu.querySelector('[aria-selected="true"]');
    if (active) active.scrollIntoView({ block: "nearest" });
  };
  const close = () => {
    wrap.classList.remove("open");
    trigger.setAttribute("aria-expanded", "false");
    document.removeEventListener("click", outsideClick, true);
    document.removeEventListener("keydown", onKey);
    Array.from(menu.children).forEach((c) => c.classList.remove("kbd"));
  };
  const outsideClick = (e) => {
    if (!wrap.contains(e.target)) close();
  };
  const onKey = (e) => {
    const items = Array.from(menu.children);
    let idx = items.findIndex((c) => c.classList.contains("kbd"));
    if (idx === -1)
      idx = items.findIndex((c) => c.getAttribute("aria-selected") === "true");
    if (e.key === "ArrowDown") {
      e.preventDefault();
      items.forEach((c) => c.classList.remove("kbd"));
      const next = items[Math.min(items.length - 1, idx + 1)];
      next.classList.add("kbd");
      next.scrollIntoView({ block: "nearest" });
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      items.forEach((c) => c.classList.remove("kbd"));
      const prev = items[Math.max(0, idx - 1)];
      prev.classList.add("kbd");
      prev.scrollIntoView({ block: "nearest" });
    } else if (e.key === "Enter") {
      e.preventDefault();
      const kbd = items.find((c) => c.classList.contains("kbd"));
      if (kbd) {
        setValue(kbd.dataset.value);
        close();
      }
    } else if (e.key === "Escape") {
      close();
      trigger.focus();
    }
  };

  trigger.addEventListener("click", () => {
    wrap.classList.contains("open") ? close() : open();
    trigger.focus();
  });

  syncLabel();
};

const init = async () => {
  try {
    const defaults = await api("/api/defaults");
    $("#inputDir").value = defaults.input_dir;
    $("#outputDir").value = defaults.output_dir;
    triggerScan();
  } catch (e) {
    console.error(e);
  }

  enhanceSelect($("#outputFormat"));
  enhanceSelect($("#axisUp"));

  $$("[data-picker]").forEach((btn) =>
    btn.addEventListener("click", () => pickFolder(btn.dataset.picker))
  );
  $("#inputDir").addEventListener("input", triggerScan);
  $("#recursive").addEventListener("change", triggerScan);
  $("#btnClearLog").addEventListener("click", () => (logBox.innerHTML = ""));
  btnRun.addEventListener("click", startConvert);
  btnInstall.addEventListener("click", () => startInstall(true));

  refreshAssimp();
  setInterval(refreshAssimp, 4000);
};

document.addEventListener("DOMContentLoaded", init);
