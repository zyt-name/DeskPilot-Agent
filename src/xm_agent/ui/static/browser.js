"use strict";

/* 浏览器视图：把 AI 正在看的页面镜像到面板里。
 *
 * 三条约束决定了实现方式：
 * 1. 画面只在「有人看」时才拉——抽屉一关就断开 SSE，浏览器端停流，CPU 归零。
 * 2. 抽屉是覆盖层，不挤压任务页；全屏模式才切成左右两栏（左对话右浏览器）。
 * 3. 不重启浏览器：视图只读，不替 AI 做决定；手动打开地址是显式的用户动作。 */

(function () {
  const view = {
    enabled: false,
    open: false,
    focus: false,
    source: null,
    status: null,
    frames: 0,
    pollTimer: 0,
    drawing: false,
    pending: null,
    lastUrl: "",
  };

  const el = (id) => document.getElementById(id);

  function token() {
    return (typeof state !== "undefined" && state.token) || "";
  }

  function setText(id, text) {
    const node = el(id);
    if (node) node.textContent = text || "";
  }

  function setDot(mode) {
    ["browser-dot", "task-browser-dot"].forEach((id) => {
      const dot = el(id);
      if (!dot) return;
      dot.classList.remove("hidden", "live", "off");
      if (mode === "live") dot.classList.add("live");
      if (mode === "off") dot.classList.add("off");
    });
  }

  function renderStatus(data) {
    view.status = data || {};
    const started = Boolean(data && data.started);
    if (started) {
      setText("browser-title", data.title || "浏览器");
      setText("browser-url", data.url || "");
      const mode = data.headless ? "无头" : "可见窗口";
      setText("browser-status", `${data.description || ""} · ${mode}`);
      setDot(data.mirror ? "live" : "on");
      el("browser-empty").classList.add("hidden");
    } else {
      setText("browser-title", "浏览器");
      setText("browser-url", "未启动");
      setText("browser-status", "未启动");
      setDot("off");
      el("browser-empty").classList.remove("hidden");
    }
    setText("browser-frames", view.frames ? `${view.frames} 帧` : "");
  }

  async function refresh() {
    if (!view.enabled) return;
    try {
      const data = await api("/api/browser");
      renderStatus(data);
    } catch (err) {
      /* 面板可能正在重启，下一轮再试 */
    }
  }

  function draw(image) {
    const canvas = el("browser-canvas");
    if (!canvas) return;
    const img = new Image();
    img.onload = () => {
      if (canvas.width !== img.width || canvas.height !== img.height) {
        canvas.width = img.width;
        canvas.height = img.height;
      }
      const ctx = canvas.getContext("2d");
      if (ctx) ctx.drawImage(img, 0, 0);
      view.frames += 1;
      setText("browser-frames", `${view.frames} 帧`);
      el("browser-empty").classList.add("hidden");
      view.drawing = false;
      const next = view.pending;
      view.pending = null;
      if (next) draw(next);
    };
    img.onerror = () => {
      view.drawing = false;
    };
    img.src = image;
  }

  function pushFrame(image) {
    if (!image) return;
    if (view.drawing) {
      // 上一帧还没解码完，只留最新的一张，避免排队造成延迟
      view.pending = image;
      return;
    }
    view.drawing = true;
    draw(image);
  }

  function connect() {
    if (view.source || !view.enabled) return;
    const url = `/api/browser/stream?token=${encodeURIComponent(token())}`;
    const source = new EventSource(url);
    view.source = source;
    source.addEventListener("browser_ready", (event) => {
      try {
        renderStatus(JSON.parse(event.data).payload || {});
      } catch (err) {
        /* 状态刷新失败不影响画面 */
      }
    });
    source.addEventListener("browser_frame", (event) => {
      try {
        const payload = JSON.parse(event.data).payload || {};
        pushFrame(payload.image);
      } catch (err) {
        /* 单帧损坏直接丢弃 */
      }
    });
    source.onerror = () => {
      disconnect();
      if (view.open) {
        window.setTimeout(() => {
          if (view.open) connect();
        }, 2500);
      }
    };
  }

  function disconnect() {
    if (view.source) {
      view.source.close();
      view.source = null;
    }
  }

  function open() {
    if (!view.enabled) return;
    view.open = true;
    el("browser-drawer").classList.add("open");
    el("browser-drawer").setAttribute("aria-hidden", "false");
    connect();
    refresh();
    window.clearInterval(view.pollTimer);
    view.pollTimer = window.setInterval(refresh, 2500);
  }

  function close() {
    view.open = false;
    view.focus = false;
    el("browser-drawer").classList.remove("open");
    el("browser-drawer").setAttribute("aria-hidden", "true");
    el("task-workspace").classList.remove("browser-focus");
    disconnect();
    window.clearInterval(view.pollTimer);
    view.pollTimer = 0;
  }

  function toggleFocus() {
    view.focus = !view.focus;
    el("task-workspace").classList.toggle("browser-focus", view.focus);
    if (view.focus && !view.open) open();
  }

  async function act(action, payload = {}) {
    try {
      const data = await api("/api/browser", {
        method: "POST",
        body: JSON.stringify({ action, ...payload }),
      });
      if (data && data.ok === false) {
        setText("browser-status", data.error || "操作失败");
      }
      await refresh();
      return data;
    } catch (err) {
      setText("browser-status", "请求失败");
      return null;
    }
  }

  /* AI 用到浏览器时给个信号：亮灯 + 首次自动展开一次，之后不再打扰。
     用户手动收起后不再自动弹出（尊重「我看完了」这个意图）。 */
  let autoOpened = false;
  let dismissed = false;

  function notice(capability) {
    if (!view.enabled) return;
    if (!String(capability || "").startsWith("browser.")) return;
    if (view.open) return;
    if (!autoOpened && !dismissed) {
      autoOpened = true;
      open();
      return;
    }
    setDot("live");
  }

  async function init() {
    const toggle = el("task-browser-toggle");
    try {
      const data = await api("/api/browser");
      view.enabled = Boolean(data && data.enabled);
      renderStatus(data);
    } catch (err) {
      view.enabled = false;
    }
    if (!view.enabled) {
      if (toggle) toggle.classList.add("hidden");
      return;
    }
    if (toggle) {
      toggle.classList.remove("hidden");
      toggle.addEventListener("click", () => {
        if (view.open) {
          dismissed = true;
          close();
        } else {
          open();
        }
      });
    }
    el("browser-close").addEventListener("click", () => {
      dismissed = true;
      close();
    });
    el("browser-expand").addEventListener("click", toggleFocus);
    el("browser-handoff").addEventListener("click", () => act("handoff"));
    el("browser-open-form").addEventListener("submit", async (event) => {
      event.preventDefault();
      const input = el("browser-open-url");
      const url = String(input.value || "").trim();
      if (!url) return;
      setText("browser-status", "正在打开…");
      await act("open", { url });
      input.value = "";
    });
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape" && view.focus) {
        event.preventDefault();
        toggleFocus();
      }
    });
  }

  window.browserView = { open, close, notice, refresh, get state() { return view; } };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
