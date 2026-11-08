"use strict";

(function () {
  const taskState = {
    conversations: [],
    conversationId: "",
    conversation: null,
    queue: { queued: [], running: [] },
    activeRunId: "",
    lastEventId: 0,
    replayAfter: 0,
    source: null,
    sourceConversation: "",
    stepNodes: new Map(),
    running: false,
    paused: false,
    nativeHotkey: false,
    defaultAuditMode: "ai",
    confirms: [],
    currentConfirm: null,
    confirmOpen: false,
  };

  const AUDIT_LABEL = {
    allow: ["允许", "ok"],
    ask_user: ["需确认", "warn"],
    deny: ["阻止", "bad"],
  };

  const RISK_LABEL = {
    low: "低风险",
    medium: "中风险",
    high: "高风险",
    critical: "严重风险",
  };

  function $(id) {
    return document.getElementById(id);
  }

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function nearBottom(box) {
    return box.scrollHeight - box.scrollTop - box.clientHeight < 140;
  }

  function scrollChat(force) {
    const box = $("task-chat-scroll");
    if (!box) return;
    if (force || nearBottom(box)) {
      box.scrollTop = box.scrollHeight;
    }
  }

  function showEmpty(show) {
    const empty = $("task-chat-empty");
    if (empty) empty.classList.toggle("hidden", !show);
  }

  function resetStream() {
    taskState.stepNodes.clear();
    taskState.lastEventId = 0;
  }

  function ensureStep(step, capability) {
    const key = Number(step) || 0;
    let node = taskState.stepNodes.get(key);
    if (node) {
      if (capability && !node.capability.textContent) {
        node.capability.textContent = capability;
      }
      return node;
    }
    const card = el("div", "step-card");
    const head = el("div", "step-head");
    const dot = el("span", "step-dot");
    const index = el("span", "step-index", key ? `第 ${key} 步` : "执行");
    const cap = el("span", "step-cap", capability || "");
    head.append(dot, index, cap);

    const thinking = document.createElement("details");
    thinking.className = "step-thinking";
    const summary = el("summary", "", "思考过程");
    const thinkingText = el("pre", "thinking-text");
    thinking.append(summary, thinkingText);

    const assistant = el("div", "step-assistant");
    const meta = el("div", "step-meta");
    card.append(head, thinking, assistant, meta);
    $("task-messages").appendChild(card);
    node = {
      card,
      head,
      dot,
      capability: cap,
      thinking,
      summary,
      thinkingText,
      assistant,
      meta,
      hasThinking: false,
      hasAssistant: false,
    };
    taskState.stepNodes.set(key, node);
    showEmpty(false);
    scrollChat(true);
    return node;
  }

  function appendUserBubble(text, pending) {
    const row = el("div", "msg user");
    const bubble = el("div", "bubble user");
    bubble.textContent = text;
    if (pending) bubble.dataset.pending = "1";
    row.appendChild(bubble);
    $("task-messages").appendChild(row);
    showEmpty(false);
    scrollChat(true);
    return bubble;
  }

  function appendAssistantBubble(text, payload) {
    const row = el("div", "msg assistant");
    const bubble = el("div", "bubble assistant");
    if (payload && payload.status) {
      const chip = el(
        "span",
        `chip ${payload.status === "done" ? "ok" : "warn"}`,
        STATUS_LABEL[payload.status] || payload.status,
      );
      bubble.appendChild(chip);
      const body = el("div", "", text || payload.reason || "");
      bubble.appendChild(body);
    } else {
      bubble.textContent = text;
    }
    if (payload && payload.thinking) {
      const details = document.createElement("details");
      details.className = "msg-thinking";
      details.appendChild(el("summary", "", "思考过程"));
      details.appendChild(el("pre", "thinking-text", payload.thinking));
      bubble.appendChild(details);
    }
    row.appendChild(bubble);
    $("task-messages").appendChild(row);
    showEmpty(false);
    scrollChat(true);
  }

  function commitPendingGoal(goal) {
    const pending = document.querySelector("#task-messages .bubble.user[data-pending='1']");
    if (pending) {
      delete pending.dataset.pending;
      return;
    }
    if (goal) appendUserBubble(goal, false);
  }

  function renderMessages(messages) {
    const box = $("task-messages");
    box.replaceChildren();
    resetStream();
    const empty = el("div", "chat-empty");
    empty.id = "task-chat-empty";
    empty.appendChild(el("h3", "", "开始一个桌面任务"));
    empty.appendChild(
      el(
        "p",
        "",
        "描述目标，AI 会逐步操作并验证。敏感操作先由顶层审计 AI 判断；涉及其他项目或用户数据时，系统会请你审计目的、流程与影响。",
      ),
    );
    box.appendChild(empty);
    if (!messages || messages.length === 0) {
      showEmpty(true);
      return;
    }
    showEmpty(true);
    messages.forEach((message) => {
      if (message.role === "user") {
        appendUserBubble(message.content);
      } else {
        appendAssistantBubble(message.content, message.payload || {});
      }
    });
    showEmpty(false);
    scrollChat(true);
  }

  function auditCard(payload) {
    const verdict = payload.verdict || {};
    const operation = payload.operation || {};
    const [label, tone] = AUDIT_LABEL[verdict.verdict] || [
      verdict.verdict || "审计",
      "",
    ];
    const card = el("div", `audit-card ${tone}`);
    const head = el("div", "audit-head");
    head.append(
      el("span", "audit-tag", "顶层审计"),
      el("span", `chip ${tone}`, label),
      el("span", "chip", RISK_LABEL[verdict.risk] || verdict.risk || "未评估"),
    );
    if (verdict.source) {
      head.appendChild(
        el(
          "span",
          "audit-source",
          verdict.source === "ai"
            ? `模型 · ${verdict.model || "audit"}`
            : verdict.source === "rule"
              ? "确定性规则"
              : verdict.source === "fallback"
                ? "审计不可用，已升级"
                : verdict.source,
        ),
      );
    }
    card.appendChild(head);

    const summary = operationSummary(operation);
    if (summary) card.appendChild(el("div", "audit-operation", summary));

    if (verdict.purpose) {
      const line = el("div", "audit-line");
      line.appendChild(el("b", "", "目的："));
      line.appendChild(document.createTextNode(verdict.purpose));
      card.appendChild(line);
    }
    if (verdict.impact) {
      const line = el("div", "audit-line");
      line.appendChild(el("b", "", "可能影响："));
      line.appendChild(document.createTextNode(verdict.impact));
      card.appendChild(line);
    }
    if (Array.isArray(verdict.steps) && verdict.steps.length) {
      const line = el("div", "audit-line");
      line.appendChild(el("b", "", "流程："));
      line.appendChild(
        document.createTextNode(verdict.steps.slice(0, 6).join(" → ")),
      );
      card.appendChild(line);
    }
    if (verdict.safer_alternative) {
      const line = el("div", "audit-line safe");
      line.appendChild(el("b", "", "更安全的替代："));
      line.appendChild(document.createTextNode(verdict.safer_alternative));
      card.appendChild(line);
    }
    if (verdict.reversible !== null && verdict.reversible !== undefined) {
      card.appendChild(
        el(
          "div",
          "audit-line",
          verdict.reversible ? "可逆性：可逆" : "可逆性：不可逆",
        ),
      );
    }
    if (payload.user_confirmed) {
      card.appendChild(el("span", "chip ok", "用户已确认"));
    } else if (payload.allowed === false) {
      card.appendChild(
        el(
          "div",
          "audit-line bad",
          `已阻止：${payload.reason || verdict.impact || "审计拒绝"}`,
        ),
      );
    }
    return card;
  }

  function operationSummary(operation) {
    if (!operation || !operation.capability) return "";
    const action = operation.action ? `:${operation.action}` : "";
    const args = operation.args || {};
    let detail = "";
    if (operation.capability === "terminal.exec") {
      detail = args.command || "";
    } else if (operation.capability === "file.transfer") {
      detail = `${args.direction || ""} ${args.source || ""} → ${args.destination || ""}`;
    } else if (operation.capability === "desktop.act") {
      detail = args.text || args.keys || args.element_id || "";
    }
    if (String(detail).length > 200) detail = `${String(detail).slice(0, 200)}…`;
    return `${operation.capability}${action}${detail ? ` ${detail}` : ""}`;
  }

  function appendResult(payload) {
    const status = payload.status || "failed";
    const card = el("div", `result-card ${status}`);
    const head = el("div", "result-head");
    head.append(
      el("span", "result-title", `任务结束 · ${STATUS_LABEL[status] || status}`),
      el("span", "chip", `${payload.steps ?? 0} 步`),
    );
    card.appendChild(head);
    if (payload.reason) card.appendChild(el("div", "result-reason", payload.reason));
    $("task-messages").appendChild(card);
    scrollChat(true);
  }

  function appendErrorCard(message, title) {
    const card = el("div", "result-card failed");
    card.appendChild(el("div", "result-head")).appendChild(
      el("span", "result-title", title || "执行异常"),
    );
    card.appendChild(el("div", "result-reason", message || ""));
    $("task-messages").appendChild(card);
    scrollChat(true);
  }

  function handleEvent(name, message) {
    if (!message) return;
    if (message.id) {
      taskState.lastEventId = Math.max(taskState.lastEventId, Number(message.id));
    }
    const payload = message.payload || {};
    if (name === "start") {
      resetStream();
      taskState.running = true;
      taskState.paused = false;
      taskState.activeRunId = message.run_id || "";
      setRunning(true);
      const divider = el("div", "run-divider");
      divider.appendChild(el("span", "", "开始执行"));
      divider.appendChild(el("span", "muted", `trace ${payload.trace_id || ""}`));
      $("task-messages").appendChild(divider);
      showEmpty(false);
      commitPendingGoal(payload.goal);
      scrollChat(true);
      return;
    }
    if (name === "thinking") {
      const node = ensureStep(payload.step);
      node.hasThinking = true;
      node.thinking.classList.remove("hidden");
      node.thinkingText.textContent += String(payload.text || "");
      const count = node.thinkingText.textContent.length;
      node.summary.textContent = `思考过程 · ${count} 字`;
      scrollChat();
      return;
    }
    if (name === "assistant") {
      const node = ensureStep(payload.step);
      node.hasAssistant = true;
      node.assistant.textContent += String(payload.text || "");
      scrollChat();
      return;
    }
    if (name === "observation") {
      const node = ensureStep(payload.step);
      node.meta.textContent = `观察：${String(payload.summary || "").slice(0, 180)}`;
      return;
    }
    if (name === "model") {
      const divider = el("div", "run-meta");
      divider.textContent = `模型 ${payload.provider || "?"} · ${payload.model || "?"}${
        payload.vision ? " · 可识图" : ""
      }`;
      $("task-messages").appendChild(divider);
      return;
    }
    if (name === "instructions") {
      const items = payload.items || [];
      if (!items.length) return;
      const node = ensureStep(payload.step);
      node.meta.appendChild(
        el("span", "chip warn", `已注入指令：${items.join("；")}`),
      );
      return;
    }
    if (name === "audit") {
      const node = ensureStep(payload.step);
      node.card.appendChild(auditCard(payload));
      scrollChat();
      return;
    }
    if (name === "step") {
      const node = ensureStep(payload.step, payload.capability);
      if (payload.audit_blocked) {
        node.card.classList.add("blocked");
        node.dot.className = "step-dot bad";
        node.meta.textContent = `已阻止：${payload.error || "审计拒绝"}`;
      } else if (payload.finish_blocked) {
        node.card.classList.add("blocked");
        node.dot.className = "step-dot bad";
        node.meta.textContent = `完成核对未通过：${
          payload.error || "屏幕未显示目标状态"
        }`;
      } else {
        const receipt = payload.receipt_ok ? "回执通过" : "回执失败";
        const verify = payload.verify_ok
          ? payload.deferred
            ? "待视觉核对"
            : "验证通过"
          : "验证失败";
        node.dot.className = `step-dot ${
          payload.receipt_ok && payload.verify_ok ? "ok" : "bad"
        }`;
        node.meta.textContent = `${receipt} · ${verify}${
          payload.error ? ` · ${payload.error}` : ""
        }`;
      }
      scrollChat();
      return;
    }
    if (name === "result") {
      appendResult(payload);
      taskState.running = false;
      taskState.paused = false;
      taskState.activeRunId = "";
      setRunning(false);
      refreshConversations();
      refreshQueue();
      return;
    }
    if (name === "error") {
      appendErrorCard(payload.message);
      return;
    }
    if (name === "lock_busy") {
      appendErrorCard(
        "另一个进程正在控制桌面，请稍后重试。",
        "桌面锁被占用",
      );
      taskState.running = false;
      setRunning(false);
      return;
    }
    if (name === "pause") {
      setPaused(Boolean(payload.paused));
      return;
    }
    if (name === "queue" || name === "queue_added") {
      if (payload && Array.isArray(payload.queued)) {
        taskState.queue = {
          queued: payload.queued,
          running: payload.running || [],
        };
        renderQueue();
      } else {
        refreshQueue();
      }
      refreshConversations();
      return;
    }
    if (name === "confirm_request") {
      taskState.confirms.push(payload);
      if (!taskState.confirmOpen) showNextConfirm();
      return;
    }
    if (name === "confirm_result") {
      const current = taskState.currentConfirm;
      if (current && current.id === payload.id && taskState.confirmOpen) {
        hideConfirm();
        showNextConfirm();
      }
    }
  }

  function closeEvents() {
    if (taskState.source) {
      taskState.source.close();
      taskState.source = null;
      taskState.sourceConversation = "";
    }
  }

  function connectEvents() {
    closeEvents();
    if (!taskState.conversationId || !state.token) return;
    const after = taskState.replayAfter || 0;
    const url =
      `/api/events?token=${encodeURIComponent(state.token)}` +
      `&conversation_id=${encodeURIComponent(taskState.conversationId)}` +
      (after ? `&after=${after}` : "");
    const source = new EventSource(url);
    const names = [
      "start",
      "observation",
      "thinking",
      "assistant",
      "model",
      "instructions",
      "audit",
      "step",
      "result",
      "error",
      "lock_busy",
      "pause",
      "queue",
      "queue_added",
      "confirm_request",
      "confirm_result",
    ];
    names.forEach((name) => {
      source.addEventListener(name, (event) => {
        let message = null;
        try {
          message = JSON.parse(event.data);
        } catch (err) {
          return;
        }
        handleEvent(name, message);
      });
    });
    source.onerror = () => {
      // EventSource reconnects automatically and sends Last-Event-ID.
    };
    taskState.source = source;
    taskState.sourceConversation = taskState.conversationId;
  }

  function setRunning(running) {
    taskState.running = running;
    $("task-run-controls").classList.toggle("hidden", !running);
    $("task-pause").classList.toggle("hidden", taskState.paused);
    $("task-resume").classList.toggle("hidden", !taskState.paused);
    $("task-run-pill").textContent = taskState.paused ? "已暂停" : "运行中";
    $("task-composer-hint").textContent = running
      ? "运行中可按 F8 暂停；暂停后输入内容会作为新指令注入。"
      : "敏感操作会先经过顶层审计。";
    if (!running) setPaused(false);
  }

  function setPaused(paused) {
    taskState.paused = paused;
    $("task-pause-banner").classList.toggle("hidden", !paused);
    $("task-pause").classList.toggle("hidden", paused);
    $("task-resume").classList.toggle("hidden", !paused);
    $("task-run-pill").textContent = paused ? "已暂停" : "运行中";
  }

  async function togglePause() {
    if (!taskState.activeRunId || !taskState.running) return;
    try {
      if (taskState.paused) {
        await api(`/api/runs/${taskState.activeRunId}/resume`, {
          method: "POST",
          body: JSON.stringify({}),
        });
      } else {
        await api(`/api/runs/${taskState.activeRunId}/pause`, {
          method: "POST",
          body: JSON.stringify({}),
        });
      }
    } catch (err) {
      toast(err.message, true);
    }
  }

  async function cancelRun() {
    if (!taskState.activeRunId) return;
    try {
      await api(`/api/runs/${taskState.activeRunId}/cancel`, {
        method: "POST",
        body: JSON.stringify({}),
      });
      toast("已请求结束控制");
    } catch (err) {
      toast(err.message, true);
    }
  }

  async function refreshConversations() {
    try {
      const data = await api("/api/conversations");
      taskState.conversations = data.conversations || [];
      renderConversationList();
    } catch (err) {
      toast(`读取任务列表失败：${err.message}`, true);
    }
  }

  async function refreshQueue() {
    try {
      const data = await api("/api/queue");
      taskState.queue = {
        queued: data.queued || [],
        running: data.running || [],
      };
      renderQueue();
    } catch (err) {
      toast(`读取队列失败：${err.message}`, true);
    }
  }

  function renderConversationList() {
    const box = $("task-conversation-list");
    box.replaceChildren();
    if (!taskState.conversations.length) {
      box.appendChild(el("p", "empty", "还没有任务。"));
      return;
    }
    taskState.conversations.forEach((conversation) => {
      const item = el("button", "conversation-item");
      item.type = "button";
      if (conversation.id === taskState.conversationId) {
        item.classList.add("active");
      }
      const top = el("div", "conversation-top");
      top.appendChild(
        el("span", "conversation-title", conversation.title || "未命名任务"),
      );
      if (conversation.last_status) {
        top.appendChild(
          el(
            "span",
            `chip ${conversation.last_status === "done" ? "ok" : "warn"}`,
            STATUS_LABEL[conversation.last_status] || conversation.last_status,
          ),
        );
      }
      item.appendChild(top);
      item.appendChild(
        el(
          "div",
          "conversation-meta",
          `${conversation.adapter} · ${conversation.task_count || 0} 个任务 · ${
            conversation.message_count || 0
          } 条消息`,
        ),
      );
      item.appendChild(
        el(
          "div",
          "conversation-time",
          formatTime(conversation.last_message_at || conversation.updated_at),
        ),
      );
      item.addEventListener("click", () => selectConversation(conversation.id));
      box.appendChild(item);
    });
  }

  function renderQueue() {
    const box = $("task-queue-list");
    box.replaceChildren();
    const running = taskState.queue.running || [];
    const queued = taskState.queue.queued || [];
    $("task-queue-count").textContent = `${queued.length} 个等待`;
    if (!running.length && !queued.length) {
      box.appendChild(el("p", "empty", "队列为空。"));
      return;
    }
    running.forEach((task) => {
      const item = el("div", "queue-item running");
      item.appendChild(el("div", "queue-goal", task.goal));
      item.appendChild(
        el("div", "queue-meta", `运行中 · ${task.adapter} · 前台独占`),
      );
      const actions = el("div", "queue-actions");
      actions.appendChild(
        makeButton("结束", "btn btn-danger-ghost mini", async () => {
          try {
            await api(`/api/runs/${task.run_id}/cancel`, {
              method: "POST",
              body: JSON.stringify({}),
            });
          } catch (err) {
            toast(err.message, true);
          }
        }),
      );
      item.appendChild(actions);
      box.appendChild(item);
    });
    queued.forEach((task, index) => {
      const item = el("div", "queue-item");
      item.appendChild(el("div", "queue-goal", task.goal));
      item.appendChild(
        el(
          "div",
          "queue-meta",
          `#${index + 1} · ${task.adapter} · ${
            task.audit_mode === "confirm_all" ? "每次确认" : "AI 审计"
          }`,
        ),
      );
      const actions = el("div", "queue-actions");
      actions.append(
        makeButton("插队", "btn btn-ghost mini", async () => {
          try {
            await api("/api/queue/jump", {
              method: "POST",
              body: JSON.stringify({ id: task.id, preempt: true }),
            });
            await refreshQueue();
          } catch (err) {
            toast(err.message, true);
          }
        }),
        makeButton("移除", "btn btn-danger-ghost mini", async () => {
          try {
            await api("/api/queue/remove", {
              method: "POST",
              body: JSON.stringify({ id: task.id }),
            });
            await refreshQueue();
          } catch (err) {
            toast(err.message, true);
          }
        }),
      );
      item.appendChild(actions);
      box.appendChild(item);
    });
  }

  function makeButton(label, className, handler) {
    const button = el("button", className, label);
    button.type = "button";
    button.addEventListener("click", handler);
    return button;
  }

  async function selectConversation(conversationId) {
    if (!conversationId) return;
    try {
      const detail = await api(
        `/api/conversations/${encodeURIComponent(conversationId)}`,
      );
      taskState.conversationId = conversationId;
      taskState.conversation = detail;
      taskState.lastEventId = detail.last_event_id || 0;
      const runningRun = (detail.runs || []).find(
        (run) => run.status === "running",
      );
      if (runningRun) {
        taskState.activeRunId = runningRun.id;
        taskState.replayAfter = runningRun.first_event_id
          ? Math.max(0, Number(runningRun.first_event_id) - 1)
          : detail.last_event_id || 0;
      } else {
        taskState.activeRunId = "";
        taskState.replayAfter = detail.last_event_id || 0;
      }
      $("task-conversation-title").textContent = detail.title || "新任务";
      $("task-conversation-sub").textContent = `${detail.adapter} · ${
        detail.audit_mode === "confirm_all" ? "每次操作都询问" : "AI 审计 + 用户确认"
      }`;
      if (state.config && state.config.adapters.includes(detail.adapter)) {
        $("task-adapter").value = detail.adapter;
      }
      $("task-audit-mode").value = detail.audit_mode || "ai";
      renderMessages(detail.messages || []);
      connectEvents();
      setRunning(Boolean(runningRun));
      setPaused(Boolean(runningRun && runningRun.paused));
      renderConversationList();
    } catch (err) {
      toast(`读取任务失败：${err.message}`, true);
    }
  }

  function newConversation() {
    closeEvents();
    taskState.conversationId = "";
    taskState.conversation = null;
    taskState.activeRunId = "";
    taskState.lastEventId = 0;
    taskState.replayAfter = 0;
    taskState.paused = false;
    setRunning(false);
    $("task-conversation-title").textContent = "新任务";
    $("task-conversation-sub").textContent =
      "敏感操作由顶层审计 AI 判断，涉及用户数据时请你确认。";
    $("task-audit-mode").value = taskState.defaultAuditMode;
    const profile = state.profile || {};
    if (profile.default_adapter) {
      $("task-adapter").value = profile.default_adapter;
    }
    renderMessages([]);
    renderConversationList();
    $("task-goal").focus();
  }

  async function submitGoal(event) {
    event.preventDefault();
    const goal = $("task-goal").value.trim();
    if (!goal) {
      toast("请输入任务目标", true);
      return;
    }
    const adapter = $("task-adapter").value;
    const auditMode = $("task-audit-mode").value;
    if (taskState.paused && taskState.activeRunId) {
      try {
        await api(`/api/runs/${taskState.activeRunId}/instruct`, {
          method: "POST",
          body: JSON.stringify({ text: goal }),
        });
        $("task-goal").value = "";
        toast("指令已注入，将在下一次决策时生效");
      } catch (err) {
        toast(err.message, true);
      }
      return;
    }
    try {
      if (!taskState.conversationId) {
        const created = await api("/api/conversations", {
          method: "POST",
          body: JSON.stringify({
            title: goal.slice(0, 60),
            adapter,
            audit_mode: auditMode,
          }),
        });
        taskState.conversationId = created.conversation.id;
        taskState.conversation = created.conversation;
        $("task-conversation-title").textContent =
          created.conversation.title || "新任务";
        taskState.replayAfter = 0;
        connectEvents();
      }
      appendUserBubble(goal, true);
      $("task-goal").value = "";
      const data = await api("/api/runs", {
        method: "POST",
        body: JSON.stringify({
          goal,
          adapter,
          auto_confirm: $("task-auto-confirm").checked,
          conversation_id: taskState.conversationId,
          audit_mode: auditMode,
        }),
      });
      if (data.queued) {
        toast("当前任务正在运行，已加入队列");
        await refreshQueue();
        await refreshConversations();
        return;
      }
      taskState.activeRunId = data.id;
      setRunning(true);
      connectEvents();
    } catch (err) {
      const pending = document.querySelector(
        "#task-messages .bubble.user[data-pending='1']",
      );
      if (pending) pending.parentElement.remove();
      toast(err.message, true);
    }
  }

  async function updateConversationSettings() {
    if (!taskState.conversationId) return;
    try {
      const data = await api("/api/conversations/update", {
        method: "POST",
        body: JSON.stringify({
          id: taskState.conversationId,
          adapter: $("task-adapter").value,
          audit_mode: $("task-audit-mode").value,
        }),
      });
      taskState.conversation = data.conversation;
      $("task-conversation-sub").textContent = `${data.conversation.adapter} · ${
        data.conversation.audit_mode === "confirm_all"
          ? "每次操作都询问"
          : "AI 审计 + 用户确认"
      }`;
      toast("任务设置已更新");
    } catch (err) {
      toast(err.message, true);
    }
  }

  function riskHint(prompt) {
    const match = String(prompt || "").match(/风险：([a-z_]+)/i);
    const risk = match ? match[1].toLowerCase() : "medium";
    return RISK_LABEL[risk] || "需确认";
  }

  function showNextConfirm() {
    const item = taskState.confirms.shift();
    if (!item) {
      hideConfirm();
      return;
    }
    taskState.currentConfirm = item;
    taskState.confirmOpen = true;
    const action = item.action || {};
    $("confirm-summary").textContent = `${action.capability || "操作"} · 请审计目的、流程与影响后决定。`;
    $("confirm-prompt").textContent = item.prompt || "";
    $("confirm-risk").textContent = riskHint(item.prompt);
    $("confirm-modal").classList.remove("hidden");
  }

  function hideConfirm() {
    taskState.confirmOpen = false;
    taskState.currentConfirm = null;
    $("confirm-modal").classList.add("hidden");
  }

  async function resolveConfirm(approve) {
    const item = taskState.currentConfirm;
    if (!item) return;
    try {
      await api("/api/confirm", {
        method: "POST",
        body: JSON.stringify({ request_id: item.id, approve }),
      });
      hideConfirm();
      showNextConfirm();
    } catch (err) {
      toast(err.message, true);
    }
  }

  function applyProfile(profile) {
    if (!profile) return;
    if (profile.default_adapter && !taskState.conversationId) {
      $("task-adapter").value = profile.default_adapter;
    }
    if (!taskState.conversationId) {
      $("task-auto-confirm").checked = Boolean(profile.auto_confirm);
    }
  }

  function onBootstrap(data) {
    taskState.conversations = data.conversations || [];
    const queue = data.queue || {};
    taskState.queue = {
      queued: queue.queued || [],
      running: queue.running || [],
    };
    taskState.defaultAuditMode = (data.audit && data.audit.mode) || "ai";
    taskState.nativeHotkey = Boolean(data.hotkey && data.hotkey.enabled);
    fillSelect($("task-adapter"), data.config.adapters, data.profile.default_adapter);
    $("task-audit-mode").value = taskState.defaultAuditMode;
    $("task-auto-confirm").checked = Boolean(data.profile.auto_confirm);
    renderConversationList();
    renderQueue();
  }

  let entered = false;

  async function enter() {
    await refreshConversations();
    await refreshQueue();
    if (!entered) {
      entered = true;
      if (!taskState.conversationId && taskState.conversations.length) {
        await selectConversation(taskState.conversations[0].id);
      }
    }
  }

  function bind() {
    $("task-form").addEventListener("submit", submitGoal);
    $("task-new-conversation").addEventListener("click", newConversation);
    $("task-refresh").addEventListener("click", async () => {
      await refreshConversations();
      await refreshQueue();
    });
    $("task-pause").addEventListener("click", togglePause);
    $("task-resume").addEventListener("click", togglePause);
    $("task-banner-resume").addEventListener("click", togglePause);
    $("task-cancel").addEventListener("click", cancelRun);
    $("task-banner-cancel").addEventListener("click", cancelRun);
    $("task-audit-mode").addEventListener("change", updateConversationSettings);
    $("task-adapter").addEventListener("change", updateConversationSettings);
    $("confirm-approve").addEventListener("click", () => resolveConfirm(true));
    $("confirm-deny").addEventListener("click", () => resolveConfirm(false));
    $("confirm-modal").addEventListener("click", (event) => {
      if (event.target === $("confirm-modal")) resolveConfirm(false);
    });
    document.addEventListener("keydown", (event) => {
      if (event.key !== "F8" || taskState.nativeHotkey) return;
      const view = $("view-run");
      if (!view || view.classList.contains("hidden")) return;
      event.preventDefault();
      togglePause();
    });
  }

  window.TaskUI = { onBootstrap, enter, applyProfile };
  bind();
})();
