"use strict";

const state = {
  token: "",
  config: null,
  profile: null,
  currentRunId: null,
  pollTimer: null,
  ai: null,
  aiEditingId: "",
  aiSelectedId: "",
  aiFormInit: false,
  aiNameEdited: false,
  resourcePollTimer: null,
  resourceJobId: "",
  resourceModalOpen: false,
  resourceSnapshot: null,
};

const STATUS_LABEL = {
  running: "运行中",
  queued: "排队中",
  done: "已完成",
  failed: "失败",
  aborted: "已中止",
  cancelled: "已取消",
  needs_human: "需人工",
};

const TAB_META = {
  overview: ["总览", "本地运行，数据保存在本机 SQLite。"],
  run: ["任务", "对话式任务：流式思考、顶层审计与串行队列。"],
  ai: ["AI 模型", "密钥加密入库；支持多厂商、套餐、本地 Ollama 与自定义端点。"],
  usage: [
    "用量",
    "Token 是模型的计费单位（输入 + 输出）；只统计接口返回用量的成功调用。",
  ],
  history: ["历史记录", "最近 20 条任务，点击任意一行查看详情。"],
  settings: ["设置", "这些偏好会写入本机数据库。"],
};

const PIE_COLORS = [
  "#5b8cff", "#8f6bff", "#3ecf8e", "#f5a623", "#ff6b6b",
  "#38bdf8", "#f472b6", "#a3e635", "#fb923c", "#c084fc",
];

const $ = (id) => document.getElementById(id);

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

async function api(path, options = {}) {
  const headers = Object.assign({}, options.headers || {});
  if (options.body) headers["Content-Type"] = "application/json";
  if (options.method && options.method !== "GET") {
    headers["X-XM-Token"] = state.token;
  }
  const response = await fetch(path, Object.assign({}, options, { headers }));
  let data = {};
  try {
    data = await response.json();
  } catch (err) {
    data = {};
  }
  if (!response.ok) {
    throw new Error(data.error || `请求失败 (HTTP ${response.status})`);
  }
  return data;
}

function toast(message, bad) {
  const box = $("toast");
  box.textContent = message;
  box.className = bad ? "toast bad" : "toast";
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => box.classList.add("hidden"), 4200);
}

function setConnected(ok) {
  $("conn-dot").className = ok ? "dot ok" : "dot bad";
  $("conn-text").textContent = ok ? "本地 API 已连接" : "连接失败";
}

function formatNumber(value) {
  return Number(value || 0).toLocaleString("zh-CN");
}

function formatTime(value) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString("zh-CN", { hour12: false });
}

function statusBadge(status) {
  const span = el("span", `status ${status || ""}`, STATUS_LABEL[status] || status || "未知");
  return span;
}

function fillSelect(select, names, selected) {
  select.replaceChildren();
  names.forEach((name) => {
    const option = el("option", "", name);
    option.value = name;
    if (name === selected) option.selected = true;
    select.appendChild(option);
  });
}

function switchTab(tab) {
  document.body.dataset.tab = tab;
  document.querySelectorAll(".nav-item").forEach((button) => {
    button.classList.toggle("active", button.dataset.tab === tab);
  });
  document.querySelectorAll(".view").forEach((view) => {
    view.classList.toggle("hidden", view.id !== `view-${tab}`);
  });
  const [title, sub] = TAB_META[tab] || TAB_META.overview;
  $("page-title").textContent = title;
  $("page-sub").textContent = sub;
  if (tab === "run" && window.TaskUI) window.TaskUI.enter();
  if (tab === "history") refreshRuns();
  if (tab === "ai") loadAi();
  if (tab === "usage") loadUsage();
  if (tab === "settings") loadResources();
}

function bindNavigation() {
  document.querySelectorAll(".nav-item").forEach((button) => {
    button.addEventListener("click", () => switchTab(button.dataset.tab));
  });
  document.querySelectorAll("[data-jump]").forEach((button) => {
    button.addEventListener("click", () => switchTab(button.dataset.jump));
  });
}

const RAIL_MIN = 200;
const RAIL_MAX = 440;

function clamp(value, min, max) {
  return Math.min(max, Math.max(min, value));
}

function setRailWidth(width) {
  document.documentElement.style.setProperty(
    "--rail-width",
    `${Math.round(width)}px`,
  );
}

function setRailCollapsed(collapsed) {
  document.querySelector(".app").classList.toggle("rail-collapsed", collapsed);
  const toggle = $("rail-toggle");
  if (toggle) {
    const label = collapsed ? "展开任务栏" : "收起任务栏";
    toggle.title = label;
    toggle.setAttribute("aria-label", label);
  }
  try {
    localStorage.setItem("xm.railCollapsed", collapsed ? "1" : "0");
  } catch (err) {
    void err;
  }
}

function restoreRail() {
  try {
    const saved = Number(localStorage.getItem("xm.railWidth"));
    if (saved >= RAIL_MIN && saved <= RAIL_MAX) setRailWidth(saved);
    if (localStorage.getItem("xm.railCollapsed") === "1") {
      setRailCollapsed(true);
    }
  } catch (err) {
    void err;
  }
}

function bindRail() {
  const handle = $("rail-resizer");
  const toggle = $("rail-toggle");
  const rail = document.querySelector(".rail");
  if (!handle || !toggle || !rail) return;
  let dragging = false;
  let startX = 0;
  let startWidth = 0;
  const currentWidth = () => rail.getBoundingClientRect().width;
  const preferredWidth = () => {
    const value = parseFloat(
      getComputedStyle(document.documentElement).getPropertyValue(
        "--rail-width",
      ),
    );
    return Number.isFinite(value) ? value : currentWidth();
  };
  let latestWidth = preferredWidth();
  const saveWidth = () => {
    try {
      localStorage.setItem("xm.railWidth", String(Math.round(latestWidth)));
    } catch (err) {
      void err;
    }
  };

  toggle.addEventListener("click", () => {
    const collapsed = document
      .querySelector(".app")
      .classList.contains("rail-collapsed");
    setRailCollapsed(!collapsed);
  });

  handle.addEventListener("pointerdown", (event) => {
    dragging = true;
    startX = event.clientX;
    startWidth = preferredWidth();
    latestWidth = startWidth;
    handle.setPointerCapture(event.pointerId);
    handle.classList.add("dragging");
    document.body.classList.add("resizing");
    event.preventDefault();
  });

  handle.addEventListener("pointermove", (event) => {
    if (!dragging) return;
    latestWidth = clamp(startWidth + (event.clientX - startX), RAIL_MIN, RAIL_MAX);
    setRailWidth(latestWidth);
  });

  const stop = () => {
    if (!dragging) return;
    dragging = false;
    handle.classList.remove("dragging");
    document.body.classList.remove("resizing");
    saveWidth();
  };

  handle.addEventListener("pointerup", stop);
  handle.addEventListener("pointercancel", stop);
  handle.addEventListener("dblclick", () => {
    const collapsed = document
      .querySelector(".app")
      .classList.contains("rail-collapsed");
    setRailCollapsed(!collapsed);
  });
  handle.addEventListener("keydown", (event) => {
    if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
    event.preventDefault();
    const delta = event.key === "ArrowLeft" ? -16 : 16;
    latestWidth = clamp(preferredWidth() + delta, RAIL_MIN, RAIL_MAX);
    setRailWidth(latestWidth);
    saveWidth();
  });
}

function applyBootstrap(data) {
  state.token = data.token;
  state.config = data.config;
  state.profile = data.profile;

  const active = data.ai && data.ai.active;
  const modelLabel = active
    ? `${active.provider_name} · ${active.model}`
    : "未配置 · 内置演示策略";
  $("model-badge").textContent = modelLabel;
  $("stat-model").textContent = active ? active.display_name : "未配置";
  $("stat-model-note").textContent = active
    ? modelLabel
    : "前往「AI 模型」配置厂商与密钥";

  $("stat-vision").textContent = active && active.vision ? "已启用" : "未启用";
  $("stat-vision-note").textContent = active && active.vision
    ? "截图会随消息发送给模型"
    : "选择标注「可识图」的模型后自动启用";

  const totals = (data.ai && data.ai.totals) || { calls: 0, total_tokens: 0 };
  $("stat-tokens").textContent = formatNumber(totals.total_tokens);
  $("stat-tokens-note").textContent = `累计 ${formatNumber(totals.calls)} 次调用`;

  const adapters = data.config.adapters;
  $("stat-adapters").textContent = `${adapters.length} 个`;
  $("stat-adapters-note").textContent = adapters.join(" · ");

  $("info-db").textContent = data.config.db_path;
  $("info-audit").textContent = data.config.audit_root;
  $("info-default-adapter").textContent = data.config.default_adapter;
  $("info-user").textContent = data.profile.display_name;
  $("set-db").textContent = data.config.db_path;

  const adapterOptions = data.config.auto_available
    ? ["auto", ...adapters]
    : adapters;
  fillSelect($("set-adapter"), adapterOptions, data.profile.default_adapter);

  $("set-name").value = data.profile.display_name || "";
  $("set-goal").value = data.profile.default_goal || "";
  $("set-auto-confirm").checked = Boolean(data.profile.auto_confirm);

  renderRuns(data.runs || []);
  if (window.TaskUI) window.TaskUI.onBootstrap(data);
}

function eventView(event) {
  const payload = event.payload || {};
  const row = el("div", "event");
  let title = event.event;
  let meta = "";
  row.classList.add(event.event);

  if (event.event === "start") {
    title = "开始执行";
    meta = `${payload.goal || ""} · trace ${payload.trace_id || ""}`;
  } else if (event.event === "observation") {
    title = `第 ${payload.step} 步观察`;
    meta = `${String(payload.summary || "").slice(0, 320)} · ${payload.elements ?? 0} 个元素`;
  } else if (event.event === "model") {
    title = "使用模型";
    meta = `${payload.provider || "unknown"} · ${payload.model || "unknown"}${payload.vision ? " · 可识图" : ""}`;
  } else if (event.event === "route") {
    const switching = Number(payload.index) > 0;
    const chain = Array.isArray(payload.chain)
      ? payload.chain.join(" → ")
      : "";
    title = switching ? "自动切换后端" : "自动选择后端";
    meta = `${payload.adapter || ""} · ${payload.reason || ""}${
      chain ? ` · 链路 ${chain}` : ""
    }`;
  } else if (event.event === "step") {
    const receipt = payload.receipt_ok ? "回执通过" : "回执失败";
    const verify = payload.verify_ok
      ? payload.deferred
        ? "待视觉核对"
        : "验证通过"
      : "验证失败";
    title = `第 ${payload.step} 步 · ${payload.capability}`;
    meta = `${payload.finish_blocked ? "完成核对未通过 · " : ""}${receipt} · ${verify}${
      payload.error ? ` · ${payload.error}` : ""
    }`;
  } else if (event.event === "result") {
    const label = STATUS_LABEL[payload.status] || payload.status;
    title = `任务结束：${label}`;
    meta = `${payload.summary || payload.reason || ""} · ${payload.steps ?? 0} 步 · trace ${payload.trace_id || ""}`;
    row.classList.add(String(payload.status || ""));
  } else if (event.event === "error") {
    title = "执行异常";
    meta = payload.message || "";
  } else if (event.event === "lock_busy") {
    title = "桌面锁被占用";
    meta = "另一个进程正在控制桌面，请稍后重试。";
  } else {
    meta = JSON.stringify(payload);
  }

  row.appendChild(el("span", "event-dot"));
  const body = el("div");
  body.appendChild(el("div", "event-title", title));
  if (meta) body.appendChild(el("div", "event-meta", meta));
  row.appendChild(body);
  return row;
}

function renderTimeline(container, events, emptyText) {
  container.replaceChildren();
  if (!events || events.length === 0) {
    container.appendChild(el("p", "empty", emptyText));
    return;
  }
  const fragment = document.createDocumentFragment();
  events.forEach((event) => fragment.appendChild(eventView(event)));
  container.appendChild(fragment);
  container.scrollTop = container.scrollHeight;
}

function renderRuns(runs) {
  const body = $("history-body");
  body.replaceChildren();
  $("stat-runs").textContent = String(runs.length);
  if (runs.length === 0) {
    const row = el("tr");
    const cell = el("td", "time", "还没有任务记录");
    cell.colSpan = 5;
    row.appendChild(cell);
    body.appendChild(row);
    return;
  }
  runs.forEach((run) => {
    const row = el("tr");
    row.appendChild(el("td", "time", formatTime(run.created_at)));
    row.appendChild(el("td", "goal", run.goal));
    row.appendChild(el("td", "", run.adapter));
    const statusCell = el("td");
    statusCell.appendChild(statusBadge(run.status));
    row.appendChild(statusCell);
    row.appendChild(el("td", "", run.steps ?? 0));
    row.addEventListener("click", () => showRunDetail(run.id));
    body.appendChild(row);
  });
}

async function refreshRuns() {
  try {
    const data = await api("/api/runs");
    renderRuns(data.runs || []);
  } catch (err) {
    toast(`读取历史失败：${err.message}`, true);
  }
}

async function showRunDetail(runId) {
  try {
    const run = await api(`/api/runs/${runId}`);
    $("history-detail").classList.remove("hidden");
    $("history-detail-title").textContent = run.goal || "任务详情";
    const meta = $("history-meta");
    meta.replaceChildren();
    const parts = [
      ["后端", run.adapter],
      ["状态", STATUS_LABEL[run.status] || run.status],
      ["步数", String(run.steps ?? 0)],
      ["开始", formatTime(run.created_at)],
    ];
    if (run.finished_at) parts.push(["结束", formatTime(run.finished_at)]);
    if (run.reason) parts.push(["原因", run.reason]);
    parts.forEach(([key, value]) => {
      const span = el("span");
      span.appendChild(el("b", "", `${key}：`));
      span.appendChild(document.createTextNode(value || "—"));
      meta.appendChild(span);
    });
    renderTimeline(
      $("history-events"),
      run.events,
      "该记录来自历史数据库，未保留实时事件。",
    );
  } catch (err) {
    toast(`读取详情失败：${err.message}`, true);
  }
}

async function saveSettings(event) {
  event.preventDefault();
  try {
    const profile = await api("/api/profile", {
      method: "POST",
      body: JSON.stringify({
        display_name: $("set-name").value,
        default_adapter: $("set-adapter").value,
        default_goal: $("set-goal").value,
        auto_confirm: $("set-auto-confirm").checked,
      }),
    });
    state.profile = profile;
    $("info-user").textContent = profile.display_name;
    if (window.TaskUI) window.TaskUI.applyProfile(profile);
    toast("设置已保存");
  } catch (err) {
    toast(err.message, true);
  }
}

function actionButton(label, handler, className) {
  const button = el("button", `btn ${className || "btn-ghost"}`, label);
  button.type = "button";
  button.addEventListener("click", handler);
  return button;
}

function currentPreset() {
  if (!state.ai) return null;
  const id = $("ai-preset").value;
  return (state.ai.catalog || []).find((preset) => preset.id === id) || null;
}

function currentPlan() {
  const preset = currentPreset();
  if (!preset) return null;
  return preset.plans.find((plan) => plan.id === $("ai-plan").value) || null;
}

function fillPresetOptions(selected) {
  const select = $("ai-preset");
  select.replaceChildren();
  (state.ai.catalog || []).forEach((preset) => {
    const option = el("option", "", preset.name);
    option.value = preset.id;
    if (preset.id === selected) option.selected = true;
    select.appendChild(option);
  });
}

function fillPlanOptions(selected) {
  const preset = currentPreset();
  const select = $("ai-plan");
  select.replaceChildren();
  if (!preset) return;
  preset.plans.forEach((plan, index) => {
    const option = el("option", "", plan.name);
    option.value = plan.id;
    if (plan.id === selected || (!selected && index === 0)) option.selected = true;
    select.appendChild(option);
  });
}

function updateAdvancedFields() {
  const customEndpoint = $("ai-preset").value === "custom";
  $("ai-advanced-fields").classList.toggle("hidden", !customEndpoint);
  $("ai-protocol").disabled = !customEndpoint;
  $("ai-base-url").readOnly = !customEndpoint;
  $("ai-models-style").disabled = !customEndpoint;
  $("ai-requires-key").disabled = !customEndpoint;
  $("ai-models-style-note").textContent = customEndpoint
    ? "自定义端点可按接口实际风格选择。"
    : "由厂商套餐自动选择，无需手动配置。";
}

function endpointSummary(plan) {
  if (!plan || !plan.base_url) return "";
  const protocol =
    plan.protocol === "anthropic" ? "Anthropic 兼容" : "OpenAI 兼容";
  return `接口：${protocol} · ${plan.base_url}（自动配置）`;
}

function applyPlanDefaults() {
  const preset = currentPreset();
  const plan = currentPlan();
  if (!preset || !plan) return;
  $("ai-preset-note").textContent = [
    preset.description,
    plan.note,
    endpointSummary(plan),
  ]
    .filter(Boolean)
    .join(" ");
  $("ai-protocol").value = plan.protocol || "openai";
  $("ai-base-url").value = plan.base_url || "";
  $("ai-models-style").value = plan.models_style || "openai";
  $("ai-requires-key").checked = Boolean(plan.requires_key);
  updateAdvancedFields();
  if (!state.aiNameEdited || !$("ai-name").value.trim()) {
    $("ai-name").value =
      preset.name + (preset.plans.length > 1 ? ` · ${plan.name}` : "");
  }
}

function resetEditor(provider) {
  state.aiEditingId = provider ? provider.id : "";
  state.aiNameEdited = Boolean(provider);
  $("ai-editor-title").textContent = provider ? "编辑提供商" : "新增提供商";
  $("ai-editor-hint").textContent = provider
    ? `Key：${provider.api_key_hint || "未保存"}`
    : "";
  $("ai-api-key").value = "";
  $("ai-clear-key").checked = false;
  const presetId = provider
    ? provider.preset_id
    : (state.ai.catalog[0] && state.ai.catalog[0].id) || "custom";
  fillPresetOptions(presetId);
  fillPlanOptions(provider ? provider.plan_id : "");
  if (provider) {
    $("ai-name").value = provider.name || "";
    const plan = currentPlan();
    const preset = currentPreset();
    if (provider.preset_id === "custom") {
      $("ai-preset-note").textContent = `${provider.protocol} · ${provider.base_url}`;
      $("ai-protocol").value = provider.protocol || "openai";
      $("ai-base-url").value = provider.base_url || "";
      $("ai-models-style").value = provider.models_style || "openai";
      $("ai-requires-key").checked = Boolean(provider.requires_key);
    } else if (plan) {
      $("ai-preset-note").textContent = [
        preset && preset.description,
        plan.note,
        endpointSummary(plan),
      ]
        .filter(Boolean)
        .join(" ");
      $("ai-protocol").value = plan.protocol || "openai";
      $("ai-base-url").value = plan.base_url || "";
      $("ai-models-style").value = plan.models_style || "openai";
      $("ai-requires-key").checked = Boolean(plan.requires_key);
    }
    updateAdvancedFields();
  } else {
    $("ai-name").value = "";
    applyPlanDefaults();
  }
  renderFormModels();
}

function updateOverviewFromAi() {
  const active = state.ai && state.ai.active;
  $("model-badge").textContent = active
    ? `${active.provider_name} · ${active.model}`
    : "未配置 · 内置演示策略";
  $("stat-model").textContent = active ? active.display_name : "未配置";
  $("stat-model-note").textContent = active
    ? `${active.provider_name} · ${active.model}`
    : "前往「AI 模型」配置厂商与密钥";
  $("stat-vision").textContent = active && active.vision ? "已启用" : "未启用";
  $("stat-vision-note").textContent = active && active.vision
    ? "截图会随消息发送给模型"
    : "选择标注「可识图」的模型后自动启用";
  const totals = (state.ai && state.ai.totals) || null;
  if (totals) {
    $("stat-tokens").textContent = formatNumber(totals.total_tokens);
    $("stat-tokens-note").textContent = `累计 ${formatNumber(totals.calls)} 次调用`;
  }
}

function renderAiProviders() {
  const list = $("ai-provider-list");
  list.replaceChildren();
  const providers = state.ai.providers || [];
  if (providers.length === 0) {
    list.appendChild(el("p", "empty", "还没有配置提供商。"));
    return;
  }
  const active = state.ai.active;
  providers.forEach((provider) => {
    const card = el("div", "provider-card");
    if (active && active.provider_id === provider.id) card.classList.add("active");
    if (state.aiSelectedId === provider.id) card.classList.add("selected");

    const main = el("div", "provider-main");
    const name = el("div", "provider-name");
    name.appendChild(document.createTextNode(provider.name));
    if (active && active.provider_id === provider.id) {
      name.appendChild(el("span", "chip ok", "使用中"));
    }
    if (provider.has_key) {
      name.appendChild(el("span", "chip ok", "Key 已保存"));
    } else if (provider.requires_key) {
      name.appendChild(el("span", "chip warn", "缺少 Key"));
    } else {
      name.appendChild(el("span", "chip", "无需 Key"));
    }
    if (provider.models.some((model) => model.vision)) {
      name.appendChild(el("span", "chip vision", "含可识图模型"));
    }
    const keyNote = provider.api_key_hint ? ` · ${provider.api_key_hint}` : "";
    main.append(
      name,
      el(
        "div",
        "provider-meta",
        `${provider.protocol} · ${provider.base_url} · ${provider.models.length} 个模型${keyNote}`,
      ),
    );

    const actions = el("div", "provider-actions");
    actions.append(
      actionButton("模型", () => {
        state.aiSelectedId = provider.id;
        renderAi();
        $("ai-models-panel").scrollIntoView({ behavior: "smooth", block: "start" });
      }),
      actionButton("编辑", () => {
        state.aiSelectedId = provider.id;
        resetEditor(provider);
        renderAi();
        $("ai-editor-panel").scrollIntoView({ behavior: "smooth", block: "start" });
      }),
      actionButton("测试", () => testProviderFlow(provider)),
      actionButton("删除", () => removeProvider(provider)),
    );
    card.append(main, actions);
    list.appendChild(card);
  });
}

function modelOptionLabel(model) {
  const parts = [
    model.display_name && model.display_name !== model.model_id
      ? `${model.display_name} · ${model.model_id}`
      : model.model_id,
  ];
  if (model.vision) parts.push("可识图");
  if (model.source === "custom") parts.push("自定义");
  return parts.join(" · ");
}

function formProvider() {
  return (
    (state.ai.providers || []).find((item) => item.id === state.aiEditingId) ||
    null
  );
}

function clearFormModels(note) {
  const select = $("ai-form-model");
  select.replaceChildren();
  const placeholder = el("option", "", "保存并获取模型后可选");
  placeholder.value = "";
  select.appendChild(placeholder);
  select.disabled = true;
  $("ai-form-custom-row").classList.add("hidden");
  $("ai-form-model-note").textContent =
    note || "厂商或套餐已变更，请保存后重新获取模型列表。";
}

function renderFormModels() {
  const select = $("ai-form-model");
  const note = $("ai-form-model-note");
  const provider = formProvider();
  select.replaceChildren();
  $("ai-form-custom-row").classList.add("hidden");
  if (!provider) {
    select.disabled = true;
    select.appendChild(el("option", "", "填写 Key 后获取官方模型"));
    note.textContent = "先填写 API Key，再从官方接口获取后选择模型。";
    return;
  }
  select.disabled = false;
  const active = state.ai.active;
  const hasActive = Boolean(active && active.provider_id === provider.id);
  if (provider.models.length === 0) {
    const placeholder = el("option", "", "尚未获取模型");
    placeholder.value = "";
    select.appendChild(placeholder);
  } else {
    if (!hasActive) {
      const placeholder = el("option", "", "选择模型…");
      placeholder.value = "";
      select.appendChild(placeholder);
    }
    provider.models.forEach((model) => {
      const option = el("option", "", modelOptionLabel(model));
      option.value = model.model_id;
      if (hasActive && active.model === model.model_id) option.selected = true;
      select.appendChild(option);
    });
  }
  const custom = el("option", "", "使用其他模型（自定义 ID）…");
  custom.value = "__custom__";
  select.appendChild(custom);
  note.textContent =
    provider.models.length === 0
      ? "点击「获取模型列表」从官方接口拉取模型。"
      : provider.requires_key && !provider.has_key
        ? `已保存 ${provider.models.length} 个模型；尚未填写 API Key，测试可能失败。`
        : `${provider.models.length} 个模型，来自官方接口与自定义添加；选择后立即生效。`;
}

function renderAiModels() {
  const body = $("ai-models-body");
  body.replaceChildren();
  const provider = (state.ai.providers || []).find(
    (item) => item.id === state.aiSelectedId,
  );
  if (!provider) {
    $("ai-models-title").textContent = "模型明细";
    $("ai-models-status").textContent = "请选择一个提供商。";
    return;
  }
  $("ai-models-title").textContent = `模型明细 · ${provider.name}`;
  $("ai-models-status").textContent =
    `${provider.models.length} 个模型 · ` +
    (provider.has_key ? `Key ${provider.api_key_hint}` : "未保存 Key");
  if (provider.models.length === 0) {
    const row = el("tr");
    const cell = el("td", "time", "还没有模型，请在上方表单中获取官方模型或添加自定义模型。");
    cell.colSpan = 6;
    row.appendChild(cell);
    body.appendChild(row);
    return;
  }
  const active = state.ai.active;
  provider.models.forEach((model) => {
    const row = el("tr");
    const activeCell = el("td");
    const radio = document.createElement("input");
    radio.type = "radio";
    radio.name = "ai-active-model";
    radio.checked = Boolean(
      active &&
        active.provider_id === provider.id &&
        active.model === model.model_id,
    );
    radio.addEventListener("change", () =>
      activateModel(provider.id, model.model_id),
    );
    activeCell.appendChild(radio);
    row.appendChild(activeCell);
    row.appendChild(el("td", "", model.model_id));
    row.appendChild(el("td", "", model.display_name || model.model_id));
    const capability = el("td");
    capability.appendChild(
      model.vision
        ? el("span", "chip vision", "可识图")
        : el("span", "chip", "文本"),
    );
    row.appendChild(capability);
    row.appendChild(
      el("td", "time", model.source === "custom" ? "自定义" : "官方接口"),
    );
    const ops = el("td");
    ops.append(
      actionButton("测试", () => runTest(provider.id, model.model_id)),
      actionButton("删除", () => removeModel(provider.id, model.model_id)),
    );
    row.appendChild(ops);
    body.appendChild(row);
  });
}

function renderAi() {
  if (!state.ai) return;
  const active = state.ai.active;
  $("ai-active-badge").textContent = active ? "使用中" : "未配置";
  $("ai-active-provider").textContent = active
    ? active.provider_name
    : "—";
  $("ai-active-model").textContent = active
    ? `${active.display_name} (${active.model})`
    : "—";
  $("ai-active-vision").textContent = active
    ? active.vision
      ? "可识图"
      : "文本"
    : "—";
  renderAiProviders();
  renderFormModels();
  renderAiModels();
  updateOverviewFromAi();
}

async function loadAi() {
  try {
    state.ai = await api("/api/ai");
    const providers = state.ai.providers || [];
    if (!state.aiSelectedId && providers.length > 0) {
      state.aiSelectedId = providers[0].id;
    }
    if (!state.aiFormInit) {
      state.aiFormInit = true;
      resetEditor(providers[0] || null);
    }
    renderAi();
  } catch (err) {
    toast(`读取模型配置失败：${err.message}`, true);
  }
}

async function saveProvider(testAfter) {
  const body = {
    preset_id: $("ai-preset").value,
    plan_id: $("ai-plan").value,
    name: $("ai-name").value.trim(),
    protocol: $("ai-protocol").value,
    base_url: $("ai-base-url").value.trim(),
    models_style: $("ai-models-style").value,
    requires_key: $("ai-requires-key").checked,
    api_key: $("ai-api-key").value.trim(),
    clear_key: $("ai-clear-key").checked,
  };
  if (state.aiEditingId) body.id = state.aiEditingId;
  try {
    const data = await api("/api/ai/providers/save", {
      method: "POST",
      body: JSON.stringify(body),
    });
    state.ai = data.ai;
    state.aiEditingId = data.provider.id;
    state.aiSelectedId = data.provider.id;
    resetEditor(data.provider);
    renderAi();
    toast("提供商已保存");
    if (testAfter) await testProviderFlow(data.provider);
    return data.provider;
  } catch (err) {
    toast(err.message, true);
    return null;
  }
}

async function fetchModels(providerId) {
  const target = providerId || state.aiSelectedId;
  if (!target) {
    toast("请先选择一个提供商", true);
    return;
  }
  const keyOverride =
    target === state.aiEditingId ? $("ai-api-key").value.trim() : "";
  const button = $("ai-form-fetch");
  button.disabled = true;
  $("ai-models-status").textContent = "正在从官方接口获取模型…";
  $("ai-form-model-note").textContent = "正在从官方接口获取模型…";
  try {
    const data = await api("/api/ai/models/fetch", {
      method: "POST",
      body: JSON.stringify({ provider_id: target, api_key: keyOverride }),
    });
    state.ai = data.ai;
    state.aiSelectedId = target;
    renderAi();
    if (data.models.length === 0) {
      toast(
        "官方接口返回 0 个模型：该套餐可能不提供模型列表，请用「使用其他模型」手动填写 ID。",
        true,
      );
      $("ai-form-model-note").textContent =
        "官方接口返回 0 个模型；可用「使用其他模型」手动填写 ID。";
    } else {
      toast(
        `已获取 ${data.models.length} 个模型，可在「模型名称」下拉中选择`,
      );
    }
  } catch (err) {
    toast(`获取模型失败：${err.message}`, true);
    $("ai-models-status").textContent = `获取失败：${err.message}`;
    $("ai-form-model-note").textContent = `获取失败：${err.message}`;
  } finally {
    button.disabled = false;
  }
}

async function testProviderFlow(provider) {
  const providerId = typeof provider === "string" ? provider : provider.id;
  let current = (state.ai.providers || []).find(
    (item) => item.id === providerId,
  );
  if (!current) return;
  if (current.models.length === 0) {
    await fetchModels(current.id);
    current = (state.ai.providers || []).find(
      (item) => item.id === providerId,
    );
  }
  if (!current || current.models.length === 0) {
    toast("请先从官方接口获取模型或手动添加模型", true);
    return;
  }
  const active = state.ai.active;
  const modelId =
    active && active.provider_id === current.id
      ? active.model
      : current.models[0].model_id;
  await runTest(current.id, modelId);
}

async function runTest(providerId, modelId) {
  toast(`正在测试 ${modelId}…`);
  try {
    const result = await api("/api/ai/test", {
      method: "POST",
      body: JSON.stringify({ provider_id: providerId, model_id: modelId }),
    });
    if (result.ok) {
      const tokens = Number((result.usage && result.usage.total_tokens) || 0);
      toast(
        `测试通过：${result.text || "OK"} · ${result.latency_ms || 0}ms` +
          (tokens ? ` · ${formatNumber(tokens)} tokens` : ""),
      );
      await loadAi();
    } else {
      toast(`测试失败：${result.error || "未知错误"}`, true);
    }
  } catch (err) {
    toast(`测试失败：${err.message}`, true);
  }
}

async function activateModel(providerId, modelId) {
  try {
    const data = await api("/api/ai/active", {
      method: "POST",
      body: JSON.stringify({ provider_id: providerId, model_id: modelId }),
    });
    state.ai = data.ai;
    renderAi();
    toast(`已切换到 ${modelId}`);
  } catch (err) {
    toast(err.message, true);
  }
}

async function removeProvider(provider) {
  if (
    !window.confirm(
      `删除提供商「${provider.name}」及其模型配置？已产生的 Token 统计会保留。`,
    )
  ) {
    return;
  }
  try {
    const data = await api("/api/ai/providers/delete", {
      method: "POST",
      body: JSON.stringify({ id: provider.id }),
    });
    state.ai = data.ai;
    if (state.aiSelectedId === provider.id) state.aiSelectedId = "";
    if (state.aiEditingId === provider.id) resetEditor(null);
    renderAi();
    toast("提供商已删除");
  } catch (err) {
    toast(err.message, true);
  }
}

async function removeModel(providerId, modelId) {
  if (!window.confirm(`删除模型「${modelId}」？`)) return;
  try {
    const data = await api("/api/ai/models/delete", {
      method: "POST",
      body: JSON.stringify({ provider_id: providerId, model_id: modelId }),
    });
    state.ai = data.ai;
    renderAi();
    toast("模型已删除");
  } catch (err) {
    toast(err.message, true);
  }
}

async function addCustomModel() {
  const modelId = $("ai-form-custom-id").value.trim();
  if (!modelId) {
    toast("请输入自定义模型 ID", true);
    return;
  }
  const provider = await saveProvider(false);
  if (!provider) return;
  try {
    await api("/api/ai/models/add", {
      method: "POST",
      body: JSON.stringify({
        provider_id: provider.id,
        model_id: modelId,
        display_name: modelId,
      }),
    });
    await api("/api/ai/active", {
      method: "POST",
      body: JSON.stringify({ provider_id: provider.id, model_id: modelId }),
    });
    $("ai-form-custom-id").value = "";
    await loadAi();
    toast(`已添加并启用 ${modelId}`);
  } catch (err) {
    toast(err.message, true);
  }
}

function renderUsageModels(items, total) {
  const body = $("usage-models-body");
  body.replaceChildren();
  if (items.length === 0) {
    const row = el("tr");
    const cell = el("td", "time", "还没有用量记录。");
    cell.colSpan = 7;
    row.appendChild(cell);
    body.appendChild(row);
    return;
  }
  items.forEach((item) => {
    const pct = total
      ? `${((Number(item.total_tokens || 0) / total) * 100).toFixed(1)}%`
      : "0%";
    const row = el("tr");
    row.append(
      el("td", "", item.model),
      el("td", "", item.provider_name || item.provider_id || "—"),
      el("td", "", formatNumber(item.calls)),
      el("td", "", formatNumber(item.prompt_tokens)),
      el("td", "", formatNumber(item.completion_tokens)),
      el("td", "", formatNumber(item.total_tokens)),
      el("td", "", pct),
    );
    body.appendChild(row);
  });
}

function renderUsageProviders(items, total) {
  const box = $("usage-providers");
  box.replaceChildren();
  if (items.length === 0) {
    box.appendChild(el("p", "empty", "还没有提供商用量。"));
    return;
  }
  const max = Math.max(...items.map((item) => Number(item.total_tokens || 0)), 1);
  items.forEach((item) => {
    const tokens = Number(item.total_tokens || 0);
    const pct = total ? ((tokens / total) * 100).toFixed(1) : "0.0";
    const line = el("div", "stat-line");
    line.appendChild(
      el("span", "name", item.provider_name || item.provider_id || "未知"),
    );
    const track = el("div", "stat-track");
    const fill = el("div", "stat-fill");
    fill.style.width = `${Math.max(2, (tokens / max) * 100)}%`;
    track.appendChild(fill);
    line.appendChild(track);
    line.appendChild(
      el(
        "span",
        "value",
        `${formatNumber(tokens)} · ${pct}% · ${formatNumber(item.calls)} 次`,
      ),
    );
    box.appendChild(line);
  });
}

function renderUsageRecent(items) {
  const body = $("usage-recent-body");
  body.replaceChildren();
  if (items.length === 0) {
    const row = el("tr");
    const cell = el("td", "time", "还没有调用记录。");
    cell.colSpan = 8;
    row.appendChild(cell);
    body.appendChild(row);
    return;
  }
  items.forEach((item) => {
    const row = el("tr");
    row.append(
      el("td", "time", formatTime(item.ts)),
      el("td", "", item.provider_name || "—"),
      el("td", "", item.model || "—"),
      el("td", "", formatNumber(item.prompt_tokens)),
      el("td", "", formatNumber(item.completion_tokens)),
      el("td", "", formatNumber(item.total_tokens)),
      el("td", "", item.source || "—"),
      el("td", "time", item.latency_ms ? `${item.latency_ms} ms` : "—"),
    );
    body.appendChild(row);
  });
}

function renderPie(items, total) {
  const box = $("usage-pie");
  const legend = $("usage-legend");
  box.replaceChildren();
  legend.replaceChildren();
  if (items.length === 0 || !total) {
    legend.appendChild(
      el(
        "p",
        "empty",
        "还没有用量记录。配置模型并运行任务或测试后，这里会自动统计。",
      ),
    );
    return;
  }
  const top = items.slice(0, 8);
  const rest = items
    .slice(8)
    .reduce((sum, item) => sum + Number(item.total_tokens || 0), 0);
  const slices =
    rest > 0
      ? top.concat([{ model: "其他", provider_name: "", total_tokens: rest }])
      : top;
  let cursor = 0;
  const stops = slices.map((item, index) => {
    const start = cursor;
    cursor += (Number(item.total_tokens || 0) / total) * 100;
    return `${PIE_COLORS[index % PIE_COLORS.length]} ${start.toFixed(2)}% ${cursor.toFixed(2)}%`;
  });
  const pie = el("div", "pie");
  pie.style.background = `conic-gradient(${stops.join(", ")})`;
  pie.dataset.total = `${formatNumber(total)}\nToken`;
  box.appendChild(pie);
  slices.forEach((item, index) => {
    const row = el("div", "legend-item");
    const dot = el("span", "legend-dot");
    dot.style.background = PIE_COLORS[index % PIE_COLORS.length];
    const name = item.provider_name
      ? `${item.provider_name} / ${item.model}`
      : item.model;
    const pct = `${((Number(item.total_tokens || 0) / total) * 100).toFixed(1)}%`;
    row.append(
      dot,
      el("span", "legend-name", name),
      el("span", "legend-value", pct),
      el("span", "legend-value", formatNumber(item.total_tokens)),
    );
    legend.appendChild(row);
  });
}

function renderUsage(usage) {
  const totals = usage.totals || {};
  const today = usage.today || {};
  const total = Number(totals.total_tokens || 0);
  $("usage-total").textContent = formatNumber(total);
  $("usage-total-note").textContent = `累计 ${formatNumber(totals.calls)} 次调用`;
  $("usage-prompt").textContent = formatNumber(totals.prompt_tokens);
  $("usage-completion").textContent = formatNumber(totals.completion_tokens);
  $("usage-today").textContent = formatNumber(today.total_tokens);
  $("usage-today-note").textContent = `今日 ${formatNumber(today.calls)} 次调用`;
  const byModel = (usage.by_model || []).filter(
    (item) => Number(item.total_tokens) > 0,
  );
  renderPie(byModel, total);
  renderUsageModels(byModel, total);
  renderUsageProviders(usage.by_provider || [], total);
  renderUsageRecent(usage.recent || []);
}

async function loadUsage() {
  try {
    renderUsage(await api("/api/ai/usage"));
  } catch (err) {
    toast(`读取用量失败：${err.message}`, true);
  }
}

const RESOURCE_STATUS = {
  ready: ["已就绪", "ok"],
  missing: ["未安装", "pending"],
  incomplete: ["需补全", "pending"],
  outdated: ["版本不符", "pending"],
  installing: ["安装中", "active"],
  error: ["失败", "bad"],
};

function resourceStatus(status) {
  return RESOURCE_STATUS[status] || [status || "未知", "pending"];
}

function resourceRuntime(row) {
  if (row.runtime_ok === true) {
    const count = (row.runtime_tools || []).length;
    return {
      label: "运行可用",
      tone: "ok",
      detail: `MCP 握手通过 · ${count} 个工具`,
    };
  }
  if (row.runtime_ok === false) {
    return {
      label: "运行不可用",
      tone: "bad",
      detail: row.runtime_detail || "MCP 启动失败",
    };
  }
  if (row.status === "ready") {
    return {
      label: "运行未验证",
      tone: "pending",
      detail: "尚未执行 MCP 握手检测",
    };
  }
  return { label: "", tone: "pending", detail: "" };
}

function failingResourceIds(rows) {
  return (rows || [])
    .filter((row) => row.status !== "ready" || row.runtime_ok === false)
    .map((row) => row.id);
}

function renderResourceSummary(rows) {
  const box = $("resource-summary");
  box.replaceChildren();
  if (!rows || !rows.length) {
    box.appendChild(el("span", "muted", "尚未检测。"));
    return;
  }
  rows.forEach((row) => {
    const [label, tone] = resourceStatus(row.status);
    const runtime = resourceRuntime(row);
    const chip = el("span", `resource-chip ${tone}`);
    chip.appendChild(el("span", "chip-dot"));
    const runtimeLabel =
      row.runtime_ok === true
        ? " · 可用"
        : row.runtime_ok === false
          ? " · 不可用"
          : "";
    chip.appendChild(
      el("span", "", `${row.name} ${row.version} · ${label}${runtimeLabel}`),
    );
    chip.title = [row.detail, runtime.detail, row.location]
      .filter(Boolean)
      .join("\n");
    box.appendChild(chip);
  });
}

function renderResourceRows(rows) {
  const box = $("resource-list");
  box.replaceChildren();
  (rows || []).forEach((row) => {
    const [label, tone] = resourceStatus(row.status);
    const runtime = resourceRuntime(row);
    const item = el("div", "resource-row");
    const name = el("div", "r-name");
    name.appendChild(el("strong", "", row.name));
    name.appendChild(
      el(
        "span",
        "",
        [row.detail || row.purpose || "", runtime.detail]
          .filter(Boolean)
          .join(" · "),
      ),
    );
    item.appendChild(name);
    item.appendChild(el("span", "r-version", `v${row.version}`));
    item.appendChild(el("span", `r-status ${tone}`, label));
    box.appendChild(item);
  });
}

function renderResourceJob(snapshot) {
  state.resourceSnapshot = snapshot;
  const job = snapshot.job;
  const rows = job ? job.resources : snapshot.resources;
  const running = Boolean(job && job.running);
  const percent = job ? Number(job.percent || 0) : 0;
  $("resource-progress").style.width = `${Math.max(0, Math.min(100, percent))}%`;
  $("resource-spinner").classList.toggle("hidden", !running);
  renderResourceRows(rows);
  renderResourceSummary(rows);
  const failedIds = failingResourceIds(rows);
  $("resource-force").classList.toggle(
    "hidden",
    running || !failedIds.length,
  );

  if (!job) {
    $("resource-modal-title").textContent = "资源状态";
    $("resource-modal-note").textContent = "点击「检测并修复资源」开始检查。";
    return;
  }
  $("resource-modal-note").textContent =
    job.message || (running ? "正在检测资源完整性与可用性…" : "已完成");
  if (running) {
    const detecting = job.phase === "detecting";
    const probing = job.phase === "probe";
    $("resource-modal-title").textContent = detecting
      ? "正在检测资源完整性"
      : probing
        ? "正在检测资源可用性"
        : "正在修复资源";
  } else if (job.ok) {
    $("resource-modal-title").textContent = "资源检测完成";
  } else {
    $("resource-modal-title").textContent = "资源检测完成（有异常）";
  }
}

function stopResourcePolling() {
  if (state.resourcePollTimer) {
    clearTimeout(state.resourcePollTimer);
    state.resourcePollTimer = null;
  }
}

async function pollResourceJob() {
  stopResourcePolling();
  try {
    const snapshot = await api("/api/resources");
    renderResourceJob(snapshot);
    if (snapshot.job && snapshot.job.running) {
      state.resourceJobId = snapshot.job.id;
      state.resourcePollTimer = setTimeout(pollResourceJob, 800);
      return;
    }
    state.resourceJobId = "";
    if (snapshot.job) {
      toast(snapshot.job.message || "资源检测完成", !snapshot.job.ok);
    }
  } catch (err) {
    toast(`读取资源状态失败：${err.message}`, true);
  }
}

function openResourceModal() {
  state.resourceModalOpen = true;
  $("resource-modal").classList.remove("hidden");
}

function closeResourceModal() {
  state.resourceModalOpen = false;
  $("resource-modal").classList.add("hidden");
}

async function checkResources() {
  openResourceModal();
  stopResourcePolling();
  $("resource-spinner").classList.remove("hidden");
  $("resource-modal-title").textContent = "正在检测资源完整性";
  $("resource-modal-note").textContent =
    "正在检查本地 MCP 组件，并验证服务能否实际启动…";
  $("resource-progress").style.width = "1%";
  try {
    const snapshot = await api("/api/resources/sync", {
      method: "POST",
      body: JSON.stringify({}),
    });
    renderResourceJob(snapshot);
    pollResourceJob();
  } catch (err) {
    $("resource-spinner").classList.add("hidden");
    $("resource-modal-title").textContent = "资源检测失败";
    $("resource-modal-note").textContent = err.message;
    toast(`资源检测失败：${err.message}`, true);
  }
}

async function forceRepairResources() {
  const snapshot = state.resourceSnapshot;
  const rows = snapshot
    ? snapshot.job
      ? snapshot.job.resources
      : snapshot.resources
    : [];
  const ids = failingResourceIds(rows);
  if (!ids.length) return;
  openResourceModal();
  stopResourcePolling();
  $("resource-spinner").classList.remove("hidden");
  $("resource-modal-title").textContent = "正在强制重装资源";
  $("resource-modal-note").textContent = `重新安装：${ids.join("、")}`;
  $("resource-progress").style.width = "1%";
  try {
    const started = await api("/api/resources/sync", {
      method: "POST",
      body: JSON.stringify({ only: ids, force: true }),
    });
    renderResourceJob(started);
    pollResourceJob();
  } catch (err) {
    $("resource-spinner").classList.add("hidden");
    $("resource-modal-title").textContent = "资源重装失败";
    $("resource-modal-note").textContent = err.message;
    toast(`资源重装失败：${err.message}`, true);
  }
}

async function loadResources() {
  try {
    const snapshot = await api("/api/resources");
    state.resourceSnapshot = snapshot;
    renderResourceSummary(snapshot.resources);
    if (snapshot.job && snapshot.job.running) {
      openResourceModal();
      renderResourceJob(snapshot);
      pollResourceJob();
    }
  } catch (err) {
    renderResourceSummary([]);
  }
}

function bindResources() {
  $("resource-check").addEventListener("click", checkResources);
  $("resource-force").addEventListener("click", forceRepairResources);
  $("resource-modal-close").addEventListener("click", closeResourceModal);
  $("resource-modal").addEventListener("click", (event) => {
    if (event.target === $("resource-modal")) closeResourceModal();
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && state.resourceModalOpen) closeResourceModal();
  });
}

function bindForms() {
  $("settings-form").addEventListener("submit", saveSettings);
  $("history-refresh").addEventListener("click", refreshRuns);
  $("history-close").addEventListener("click", () => {
    $("history-detail").classList.add("hidden");
  });

  $("ai-new").addEventListener("click", () => {
    resetEditor(null);
    $("ai-editor-panel").scrollIntoView({ behavior: "smooth", block: "start" });
  });
  $("ai-form").addEventListener("submit", (event) => {
    event.preventDefault();
    saveProvider(false);
  });
  $("ai-save-test").addEventListener("click", () => saveProvider(true));
  $("ai-preset").addEventListener("change", () => {
    fillPlanOptions();
    applyPlanDefaults();
    clearFormModels("已切换厂商，请保存后重新获取模型列表。");
  });
  $("ai-plan").addEventListener("change", () => {
    applyPlanDefaults();
    clearFormModels("已切换套餐，请保存后重新获取模型列表。");
  });
  $("ai-name").addEventListener("input", () => {
    state.aiNameEdited = true;
  });
  $("ai-form-fetch").addEventListener("click", async () => {
    const provider = await saveProvider(false);
    if (provider) await fetchModels(provider.id);
  });
  $("ai-form-model").addEventListener("change", () => {
    const value = $("ai-form-model").value;
    const row = $("ai-form-custom-row");
    if (value === "__custom__") {
      row.classList.remove("hidden");
      $("ai-form-custom-id").focus();
      return;
    }
    row.classList.add("hidden");
    const provider = formProvider();
    if (!provider || !value) return;
    activateModel(provider.id, value);
  });
  $("ai-form-custom-add").addEventListener("click", addCustomModel);
  $("usage-refresh").addEventListener("click", loadUsage);
}

async function init() {
  restoreRail();
  bindRail();
  bindNavigation();
  bindForms();
  bindResources();
  try {
    const data = await api("/api/bootstrap");
    applyBootstrap(data);
    setConnected(true);
  } catch (err) {
    setConnected(false);
    toast(`无法连接本地服务：${err.message}`, true);
  }
}

init();
