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
    toolGroup: null,
    running: false,
    paused: false,
    awaiting: null,
    nativeHotkey: false,
    defaultAuditMode: "ai",
    confirms: [],
    currentConfirm: null,
    confirmOpen: false,
    context: null,
    aiChoices: [],
    runModelChoice: "",
    auditChoice: "",
    auditVisionAvailable: true,
    runVision: null,
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
    taskState.toolGroup = null;
  }

  function toolStats() {
    let count = 0;
    let failures = 0;
    taskState.stepNodes.forEach((node) => {
      if (node.capabilityName !== "task.finish") count += 1;
      if (node.failed) failures += 1;
    });
    return { count, failures };
  }

  function updateToolGroup(current) {
    const group = taskState.toolGroup;
    if (!group) return;
    const stats = toolStats();
    group.label.textContent = `执行工具 ${stats.count} 次`;
    group.failures.textContent = stats.failures
      ? `${stats.failures} 个失败`
      : "";
    group.failures.classList.toggle("hidden", !stats.failures);
    if (current) {
      group.state.textContent = current;
    }
  }

  function ensureToolGroup() {
    if (taskState.toolGroup) return taskState.toolGroup;
    const group = document.createElement("details");
    group.className = "tool-group";
    group.open = true;
    const head = document.createElement("summary");
    head.className = "tool-group-head";
    const chevron = el("span", "tool-chevron", "›");
    const label = el("span", "tool-group-label", "执行工具 0 次");
    const failures = el("span", "tool-group-failures hidden", "");
    const stateText = el("span", "tool-group-state", "运行中");
    head.append(chevron, label, failures, stateText);
    const list = el("div", "tool-list");
    group.append(head, list);
    $("task-messages").appendChild(group);
    taskState.toolGroup = {
      group,
      label,
      failures,
      state: stateText,
      list,
    };
    return taskState.toolGroup;
  }

  function ensureStep(step, capability) {
    const key = Number(step) || 0;
    let node = taskState.stepNodes.get(key);
    if (node) {
      if (capability && node.capabilityName !== capability) {
        node.capabilityName = capability;
        node.capability.textContent = capability;
        updateToolGroup(`正在 ${capability}`);
      }
      return node;
    }
    const group = ensureToolGroup();
    const row = document.createElement("details");
    row.className = "tool-row";
    const head = document.createElement("summary");
    head.className = "tool-row-head";
    const dot = el("span", "tool-dot");
    const index = el("span", "tool-index", key ? `#${key}` : "执行");
    const cap = el("span", "tool-cap", capability || "…");
    const audit = el("span", "tool-chip hidden", "");
    const status = el("span", "tool-status", "运行中");
    head.append(dot, index, cap, audit, status);

    const body = el("div", "tool-row-body");
    const thinking = document.createElement("details");
    thinking.className = "tool-thinking hidden";
    const thinkingLabel = el("summary", "", "思考过程");
    const thinkingText = el("pre", "thinking-text");
    thinking.append(thinkingLabel, thinkingText);
    const assistantWrap = el("div", "tool-assistant-wrap hidden");
    assistantWrap.appendChild(el("div", "tool-section-label", "模型输出"));
    const assistant = el("pre", "tool-assistant");
    assistantWrap.appendChild(assistant);
    const meta = el("div", "tool-meta");
    const audits = el("div", "tool-audits");
    body.append(thinking, assistantWrap, meta, audits);
    row.append(head, body);
    group.list.appendChild(row);

    node = {
      row,
      head,
      dot,
      capability: cap,
      capabilityName: capability || "",
      auditChip: audit,
      status,
      thinking,
      thinkingLabel,
      thinkingText,
      assistantWrap,
      assistant,
      meta,
      audits,
      hasThinking: false,
      hasAssistant: false,
      failed: false,
    };
    taskState.stepNodes.set(key, node);
    updateToolGroup(capability ? `正在 ${capability}` : "运行中");
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
    if (payload && payload.status) {
      $("task-messages").appendChild(
        summaryRow(text || payload.summary || payload.reason, payload),
      );
      showEmpty(false);
      scrollChat(true);
      return;
    }
    const bubble = el("div", "bubble assistant");
    bubble.textContent = text;
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
        "描述目标，AI 会逐步操作并验证。敏感与回退风险操作先由顶层识图审计 AI 对照屏幕判断；涉及其他项目或用户数据时，系统会请你审计目的、流程与影响。",
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
    if (payload.rollback_review) {
      head.appendChild(
        el(
          "span",
          verdict.visual ? "chip ok" : "chip warn",
          verdict.visual ? "回退风险 · 已识图审查" : "回退风险 · 未识图",
        ),
      );
    }
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
    } else if (operation.capability === "browser.act") {
      detail =
        args.url ||
        args.text ||
        args.keys ||
        (args.element_index !== undefined ? `#${args.element_index}` : "");
    } else if (operation.capability === "browser.open") {
      detail = args.url || "";
    }
    if (String(detail).length > 200) detail = `${String(detail).slice(0, 200)}…`;
    return `${operation.capability}${action}${detail ? ` ${detail}` : ""}`;
  }

  function appendInline(parent, text) {
    const pattern = /(\*\*[^*]+\*\*|`[^`]+`)/g;
    let last = 0;
    let match = pattern.exec(text);
    while (match) {
      if (match.index > last) {
        parent.appendChild(
          document.createTextNode(text.slice(last, match.index)),
        );
      }
      const token = match[0];
      if (token.startsWith("**")) {
        parent.appendChild(el("strong", "", token.slice(2, -2)));
      } else {
        parent.appendChild(el("code", "", token.slice(1, -1)));
      }
      last = match.index + token.length;
      match = pattern.exec(text);
    }
    if (last < text.length) {
      parent.appendChild(document.createTextNode(text.slice(last)));
    }
  }

  function renderMarkdown(container, source) {
    const lines = String(source || "")
      .replace(/\r\n?/g, "\n")
      .split("\n");
    let list = null;
    let listTag = "";
    const resetList = () => {
      list = null;
      listTag = "";
    };
    const openList = (tag) => {
      if (list && listTag === tag) return list;
      resetList();
      list = el(tag, "summary-list");
      listTag = tag;
      container.appendChild(list);
      return list;
    };
    let index = 0;
    while (index < lines.length) {
      const text = lines[index].trim();
      index += 1;
      if (!text) {
        resetList();
        continue;
      }
      if (text.startsWith("```")) {
        resetList();
        const code = [];
        while (index < lines.length && !lines[index].trim().startsWith("```")) {
          code.push(lines[index]);
          index += 1;
        }
        if (index < lines.length) index += 1;
        container.appendChild(el("pre", "summary-code", code.join("\n")));
        continue;
      }
      const heading = text.match(/^(#{1,4})\s+(.*)$/);
      if (heading) {
        resetList();
        const node = el("div", `summary-heading level-${heading[1].length}`);
        appendInline(node, heading[2]);
        container.appendChild(node);
        continue;
      }
      const bullet = text.match(/^[-*•]\s+(.*)$/);
      if (bullet) {
        const ul = openList("ul");
        const item = el("li", "");
        appendInline(item, bullet[1]);
        ul.appendChild(item);
        continue;
      }
      const numbered = text.match(/^(\d{1,2})[.)]\s+(.*)$/);
      if (numbered) {
        const ol = openList("ol");
        const item = el("li", "");
        appendInline(item, numbered[2]);
        ol.appendChild(item);
        continue;
      }
      resetList();
      const node = el("p", "summary-paragraph");
      appendInline(node, text);
      container.appendChild(node);
    }
  }

  function summaryRow(text, payload) {
    const status = payload.status || "failed";
    const summary = String(text || payload.summary || payload.reason || "").trim();
    const row = el("div", "msg assistant");
    const bubble = el("div", "bubble assistant task-summary");
    const head = el("div", "summary-head");
    head.appendChild(
      el(
        "span",
        `chip ${status === "done" ? "ok" : status === "needs_human" ? "warn" : "bad"}`,
        STATUS_LABEL[status] || status,
      ),
    );
    head.appendChild(
      el(
        "span",
        "summary-title",
        payload.await_user ? "任务已暂停" : "任务总结",
      ),
    );
    if (payload.await_user) {
      head.appendChild(
        el(
          "span",
          "chip warn",
          payload.await_user.kind === "login" ? "等待你登录" : "等待你处理",
        ),
      );
    }
    head.appendChild(
      el(
        "span",
        "summary-detail",
        `${payload.steps ?? 0} 步${
          payload.trace_id ? ` · trace ${payload.trace_id}` : ""
        }`,
      ),
    );
    bubble.appendChild(head);
    const body = el("div", "summary-body");
    if (summary) {
      renderMarkdown(body, summary);
    } else {
      body.appendChild(
        el("p", "summary-paragraph", "任务已结束，但没有生成总结。"),
      );
    }
    bubble.appendChild(body);
    if (payload.thinking) {
      const details = document.createElement("details");
      details.className = "msg-thinking";
      details.appendChild(el("summary", "", "思考过程"));
      details.appendChild(el("pre", "thinking-text", payload.thinking));
      bubble.appendChild(details);
    }
    row.appendChild(bubble);
    return row;
  }

  function appendResult(payload) {
    $("task-messages").appendChild(summaryRow("", payload));
    showEmpty(false);
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
      taskState.runVision = null;
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
      node.thinkingLabel.textContent = `思考过程 · ${count} 字`;
      scrollChat();
      return;
    }
    if (name === "assistant") {
      const node = ensureStep(payload.step);
      node.hasAssistant = true;
      node.assistantWrap.classList.remove("hidden");
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
      taskState.runVision = Boolean(payload.vision);
      const divider = el("div", "run-meta");
      divider.textContent = `模型 ${payload.provider || "?"} · ${payload.model || "?"}${
        payload.vision ? " · 可识图" : ""
      }`;
      $("task-messages").appendChild(divider);
      return;
    }
    if (name === "route") {
      const switching = Number(payload.index) > 0;
      const chain = Array.isArray(payload.chain)
        ? payload.chain.join(" → ")
        : "";
      const divider = el("div", "run-meta");
      const modeLabel =
        payload.mode === "browser"
          ? "纯浏览器模式"
          : switching
            ? "后端切换"
            : "自动路由";
      divider.textContent = `${modeLabel} · ${adapterLabel(
        payload.adapter,
      )}${payload.reason ? ` · ${payload.reason}` : ""}${
        chain ? ` · 链路 ${chain}` : ""
      }`;
      $("task-messages").appendChild(divider);
      scrollChat();
      return;
    }
    if (name === "browser_handoff") {
      const detail = payload.detail || {};
      const divider = el("div", "run-meta");
      divider.textContent = detail.restarted
        ? "已把浏览器切换为可见窗口，扫码登录后回来点“继续”"
        : detail.note || "浏览器窗口已前置，请在其中完成登录";
      $("task-messages").appendChild(divider);
      scrollChat();
      if (window.browserView) window.browserView.refresh();
      return;
    }
    if (name === "observation_retry") {
      const divider = el("div", "run-meta");
      divider.textContent = `第 ${payload.step || "?"} 步观察失败，正在重新观察${
        payload.error ? ` · ${String(payload.error).slice(0, 160)}` : ""
      }`;
      $("task-messages").appendChild(divider);
      scrollChat();
      return;
    }
    if (name === "provider_retry") {
      const divider = el("div", "run-meta");
      divider.textContent = `模型响应异常（第 ${payload.attempt || 1} 次）${
        payload.retrying ? " · 正在重试" : " · 已停止重试"
      }${payload.error ? ` · ${String(payload.error).slice(0, 160)}` : ""}`;
      $("task-messages").appendChild(divider);
      scrollChat();
      return;
    }
    if (name === "provider_recovered") {
      const divider = el("div", "run-meta");
      divider.textContent = `模型已恢复（重试 ${payload.attempts || 0} 次后成功）`;
      $("task-messages").appendChild(divider);
      scrollChat();
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
    if (name === "plan") {
      const actions = Array.isArray(payload.actions) ? payload.actions : [];
      if (actions.length < 2) return;
      const card = el("div", "plan-card");
      card.appendChild(el("span", "plan-tag", "批量计划"));
      card.appendChild(
        el(
          "span",
          "plan-text",
          actions
            .map((item) => {
              const action = item.args && item.args.action;
              return action
                ? `${item.capability}:${action}`
                : item.capability;
            })
            .join(" → "),
        ),
      );
      card.appendChild(
        el("span", "plan-note", `${actions.length} 步 · 执行后统一验证`),
      );
      $("task-messages").appendChild(card);
      showEmpty(false);
      scrollChat(true);
      return;
    }
    if (name === "audit") {
      const node = ensureStep(payload.step);
      node.audits.appendChild(auditCard(payload));
      const verdict = payload.verdict || {};
      const [label, tone] = AUDIT_LABEL[verdict.verdict] || [];
      if (label) {
        node.auditChip.textContent = label;
        node.auditChip.className = `tool-chip ${tone || ""}`;
      }
      scrollChat();
      return;
    }
    if (name === "step") {
      if (payload.capability && window.browserView) {
        window.browserView.notice(payload.capability);
      }
      const node = ensureStep(payload.step, payload.capability);
      let state = "完成";
      let tone = "ok";
      if (payload.audit_blocked) {
        state = "已阻止";
        tone = "bad";
        node.meta.textContent = `已阻止：${payload.error || "审计拒绝"}`;
      } else if (payload.finish_blocked) {
        state = "核对未通过";
        tone = "bad";
        node.meta.textContent = `完成核对未通过：${
          payload.error || "屏幕未显示目标状态"
        }`;
      } else if (payload.declared_failure) {
        state = "模型判定失败";
        tone = "bad";
        node.meta.textContent = payload.error || "模型判定任务未完成";
      } else {
        const receipt = payload.receipt_ok ? "回执通过" : "回执失败";
        const verify = payload.verify_ok
          ? payload.deferred
            ? "待视觉核对"
            : "验证通过"
          : "验证失败";
        tone = payload.receipt_ok && payload.verify_ok ? "ok" : "bad";
        state = payload.verify_ok
          ? payload.deferred
            ? "待核对"
            : "完成"
          : "失败";
        node.meta.textContent = `${receipt} · ${verify}${
          payload.error ? ` · ${payload.error}` : ""
        }`;
      }
      node.dot.className = `tool-dot ${tone}`;
      node.status.textContent = state;
      node.status.className = `tool-status ${tone}`;
      if (tone === "bad" && !node.failed) {
        node.failed = true;
      }
      updateToolGroup(
        payload.capability ? `正在 ${payload.capability}` : "",
      );
      scrollChat();
      return;
    }
    if (name === "result") {
      taskState.running = false;
      taskState.paused = false;
      taskState.activeRunId = "";
      if (taskState.toolGroup) {
        taskState.toolGroup.group.open = false;
        const stats = toolStats();
        updateToolGroup(stats.failures ? "已结束" : "已完成");
      }
      appendResult(payload);
      setRunning(false);
      setAwaiting(payload.await_user || null);
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
    if (name === "conversation_title") {
      const heading = $("task-conversation-title");
      if (
        heading &&
        payload.conversation_id === taskState.conversationId
      ) {
        heading.textContent = payload.title || "新任务";
      }
      const item = taskState.conversations.find(
        (conversation) => conversation.id === payload.conversation_id,
      );
      if (item) {
        item.title = payload.title || "";
        renderConversationList();
      }
      return;
    }
    if (name === "context") {
      taskState.context = payload;
      renderContext(payload);
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
      "observation_retry",
      "thinking",
      "assistant",
      "model",
      "instructions",
      "context",
      "audit",
      "plan",
      "step",
      "provider_retry",
      "provider_recovered",
      "result",
      "error",
      "lock_busy",
      "pause",
      "queue",
      "queue_added",
      "conversation_title",
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
    if (running) setAwaiting(null);
    $("task-run-controls").classList.toggle("hidden", !running);
    $("task-pause").classList.toggle("hidden", taskState.paused);
    $("task-resume").classList.toggle("hidden", !taskState.paused);
    $("task-run-pill").textContent = taskState.paused ? "已暂停" : "运行中";
    $("task-composer-hint").textContent = running
      ? "运行中可按 F8 暂停；暂停后输入内容会作为新指令注入。"
      : idleHint();
    if (!running) setPaused(false);
  }

  function idleHint() {
    return taskState.awaiting
      ? "任务已暂停等待你处理；完成登录后输入“继续”，我会从当前屏幕接着做。"
      : "敏感与回退风险操作会先经过顶层识图审计。";
  }

  function pendingAwait(messages) {
    const list = messages || [];
    const last = list[list.length - 1];
    if (
      last &&
      last.role === "assistant" &&
      last.payload &&
      last.payload.await_user
    ) {
      return last.payload.await_user;
    }
    return null;
  }

  async function resumeAwaiting() {
    if (!taskState.awaiting || taskState.running) return;
    $("task-goal").value = "继续";
    await submitGoal();
  }

  function setAwaiting(info) {
    taskState.awaiting = info || null;
    $("task-await-banner").classList.toggle("hidden", !info);
    if (info) {
      $("task-await-title").textContent =
        info.kind === "login" ? "等待你登录" : "等待你处理";
      $("task-await-text").textContent =
        info.prompt || "当前任务需要你先处理，完成后输入“继续”。";
      $("task-goal").placeholder =
        "登录完成后输入“继续”，或点击上方“我已登录，继续”";
    } else {
      $("task-goal").placeholder =
        "描述任务，例如：在记事本中输入 hello 并保存";
    }
    if (!taskState.running) {
      $("task-composer-hint").textContent = idleHint();
    }
  }

  function setPaused(paused) {
    taskState.paused = paused;
    $("task-pause-banner").classList.toggle("hidden", !paused);
    $("task-pause").classList.toggle("hidden", paused);
    $("task-resume").classList.toggle("hidden", !paused);
    $("task-run-pill").textContent = paused ? "已暂停" : "运行中";
    const ask = $("task-banner-ask");
    const noVision = taskState.runVision === false;
    ask.disabled = noVision;
    ask.title = noVision
      ? "当前模型不支持图像输入，请切换到可识图模型"
      : "截取当前屏幕并向模型提问";
    if (!paused) $("task-ask-panel").classList.add("hidden");
  }

  function openScreenAsk() {
    if (taskState.runVision === false) {
      toast("当前模型不支持图像输入，请切换到可识图模型", true);
      return;
    }
    $("task-ask-panel").classList.remove("hidden");
    $("task-ask-answer").classList.add("hidden");
    $("task-ask-question").focus();
  }

  async function askScreen() {
    const question = $("task-ask-question").value.trim();
    if (!question) {
      toast("请先输入要问的问题", true);
      return;
    }
    if (!taskState.activeRunId) {
      toast("当前没有运行中的任务", true);
      return;
    }
    const answer = $("task-ask-answer");
    answer.classList.remove("hidden", "error");
    answer.textContent = "正在截屏并思考…";
    $("task-ask-submit").disabled = true;
    try {
      const data = await api(
        `/api/runs/${taskState.activeRunId}/ask_screen`,
        {
          method: "POST",
          body: JSON.stringify({ question }),
        },
      );
      answer.textContent = data.answer || "（模型没有返回内容）";
    } catch (err) {
      answer.textContent = err.message;
      answer.classList.add("error");
    } finally {
      $("task-ask-submit").disabled = false;
    }
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
      syncHeaderTitle();
    } catch (err) {
      toast(`读取任务列表失败：${err.message}`, true);
    }
  }

  function syncHeaderTitle() {
    if (!taskState.conversationId) return;
    const heading = $("task-conversation-title");
    if (!heading) return;
    const current = taskState.conversations.find(
      (conversation) => conversation.id === taskState.conversationId,
    );
    if (current) {
      heading.textContent = current.title || "新任务";
    }
  }

  async function renameConversation(title) {
    const value = String(title || "").trim();
    if (!value || !taskState.conversationId) return;
    const data = await api("/api/conversations/update", {
      method: "POST",
      body: JSON.stringify({ id: taskState.conversationId, title: value }),
    });
    taskState.conversation = data.conversation;
    $("task-conversation-title").textContent =
      data.conversation.title || "新任务";
    await refreshConversations();
    toast("任务名称已更新");
  }

  function startTitleEdit() {
    if (!taskState.conversationId || titleEditing) return;
    const heading = $("task-conversation-title");
    const input = el("input", "title-input");
    input.type = "text";
    input.maxLength = 24;
    input.placeholder = "任务名称";
    input.value = heading.textContent === "新任务" ? "" : heading.textContent;
    titleEditing = true;
    heading.replaceWith(input);
    input.focus();
    input.select();
    let settled = false;
    const finish = async (save) => {
      if (settled) return;
      settled = true;
      const value = input.value.trim();
      input.replaceWith(heading);
      titleEditing = false;
      if (save && value && value !== heading.textContent) {
        try {
          await renameConversation(value);
        } catch (err) {
          toast(err.message, true);
        }
      }
    };
    input.addEventListener("keydown", (event) => {
      if (event.key === "Enter") {
        event.preventDefault();
        finish(true);
      } else if (event.key === "Escape") {
        event.preventDefault();
        finish(false);
      }
    });
    input.addEventListener("blur", () => finish(true));
  }

  function formatTokens(value) {
    const count = Number(value) || 0;
    return count >= 1000 ? `${(count / 1000).toFixed(1)}k` : String(count);
  }

  function renderContext(data) {
    const fill = $("task-context-fill");
    const label = $("task-context-label");
    if (!fill || !label) return;
    if (!data) {
      fill.style.width = "0%";
      fill.className = "context-fill";
      label.textContent = "上下文 0%";
      renderContextPanel(null);
      return;
    }
    const budget = Math.max(1, Number(data.budget_tokens) || 1);
    const used = Math.max(0, Number(data.used_tokens) || 0);
    const pct = Math.min(100, Math.round((used / budget) * 100));
    fill.style.width = `${pct}%`;
    fill.className = `context-fill${
      pct >= 100 ? " bad" : pct >= 80 ? " warn" : ""
    }`;
    label.textContent = `上下文 ${pct}% · ${formatTokens(used)}/${formatTokens(budget)}`;
    renderContextPanel(data);
  }

  function renderContextMessage(box, item) {
    const card = el("div", `context-msg ${item.role}`);
    card.appendChild(
      el(
        "div",
        "context-msg-role",
        `${item.role === "user" ? "用户" : "AI"}${
          item.status ? ` · ${STATUS_LABEL[item.status] || item.status}` : ""
        }`,
      ),
    );
    card.appendChild(el("div", "context-msg-body", item.content || ""));
    box.appendChild(card);
  }

  function renderContextPanel(data) {
    const usage = $("context-usage");
    const summary = $("context-summary");
    const covered = $("context-covered");
    const recentBox = $("context-recent");
    const meta = $("context-recent-meta");
    const olderBox = $("context-older");
    const olderBlock = $("context-older-block");
    const olderMeta = $("context-older-meta");
    if (
      !usage ||
      !summary ||
      !covered ||
      !recentBox ||
      !meta ||
      !olderBox ||
      !olderBlock ||
      !olderMeta
    ) {
      return;
    }
    if (!data) {
      usage.textContent = "0%";
      summary.textContent = "暂无压缩摘要。对话变长后会在这里生成。";
      covered.textContent = "";
      meta.textContent = "";
      olderBlock.classList.add("hidden");
      olderBox.replaceChildren();
      recentBox.replaceChildren(
        el("p", "context-empty", "还没有对话。"),
      );
      return;
    }
    const budget = Math.max(1, Number(data.budget_tokens) || 1);
    const used = Math.max(0, Number(data.used_tokens) || 0);
    usage.textContent = `${Math.min(100, Math.round((used / budget) * 100))}%`;
    summary.textContent =
      data.summary || "暂无压缩摘要。对话变长后会在这里生成。";
    covered.textContent = data.covered_message_id
      ? `已压缩到第 ${data.covered_message_id} 条消息`
      : "";
    const older = data.older || [];
    const recent = data.recent || [];
    olderBlock.classList.toggle("hidden", older.length === 0);
    olderMeta.textContent = older.length ? `${older.length} 条` : "";
    olderBox.replaceChildren();
    older.forEach((item) => renderContextMessage(olderBox, item));
    meta.textContent = `${recent.length} 条 · 全部 ${
      data.total_messages || 0
    } 条`;
    recentBox.replaceChildren();
    if (!recent.length) {
      recentBox.appendChild(el("p", "context-empty", "还没有对话。"));
    }
    recent.forEach((item) => renderContextMessage(recentBox, item));
  }

  async function refreshContext() {
    if (!taskState.conversationId) {
      taskState.context = null;
      renderContext(null);
      return;
    }
    try {
      const data = await api(
        `/api/conversations/${encodeURIComponent(
          taskState.conversationId,
        )}/context`,
      );
      if (taskState.conversationId !== data.conversation_id) return;
      taskState.context = data;
      renderContext(data);
    } catch (err) {
      void err;
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
          `${adapterLabel(conversation.adapter)} · ${conversation.message_count || 0} 条消息`,
        ),
      );
      item.appendChild(
        el(
          "div",
          "conversation-time",
          formatTime(conversation.last_message_at || conversation.updated_at),
        ),
      );
      item.addEventListener("click", () => {
        if (document.body.dataset.tab !== "run") switchTab("run");
        selectConversation(conversation.id);
      });
      box.appendChild(item);
    });
  }

  function renderQueue() {
    const box = $("task-queue-list");
    box.replaceChildren();
    const running = taskState.queue.running || [];
    const queued = taskState.queue.queued || [];
    $("task-queue-count").textContent = `${queued.length} 个等待`;
    const badge = $("task-queue-badge");
    if (badge) {
      const count = running.length + queued.length;
      badge.textContent = String(count);
      badge.classList.toggle("hidden", count === 0);
    }
    if (!running.length && !queued.length) {
      box.appendChild(
        el("p", "empty", "队列为空。运行中的任务与排队任务会显示在这里。"),
      );
      return;
    }
    running.forEach((task) => {
      const item = el("div", "queue-item running");
      item.appendChild(el("div", "queue-goal", task.goal));
      item.appendChild(
        el("div", "queue-meta", `运行中 · ${adapterLabel(task.adapter)} · 前台独占`),
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
          `#${index + 1} · ${adapterLabel(task.adapter)} · ${
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

  function setSideVisible(visible) {
    const workspace = $("task-workspace");
    if (!workspace) return;
    workspace.classList.toggle("side-hidden", !visible);
    try {
      localStorage.setItem("xm.taskSide", visible ? "1" : "0");
    } catch (err) {
      void err;
    }
  }

  function restoreSideVisible() {
    try {
      if (localStorage.getItem("xm.taskSide") === "0") setSideVisible(false);
    } catch (err) {
      void err;
    }
  }

  const QUEUE_MIN = 220;
  const QUEUE_MAX = 460;

  function setQueueWidth(width) {
    document.documentElement.style.setProperty(
      "--queue-width",
      `${Math.round(width)}px`,
    );
  }

  function restoreQueueWidth() {
    try {
      const saved = Number(localStorage.getItem("xm.queueWidth"));
      if (saved >= QUEUE_MIN && saved <= QUEUE_MAX) setQueueWidth(saved);
    } catch (err) {
      void err;
    }
  }

  function bindQueueResize() {
    const handle = $("task-side-resizer");
    if (!handle) return;
    const side = $("task-side");
    let dragging = false;
    let startX = 0;
    let startWidth = 0;
    const preferredWidth = () => {
      const value = parseFloat(
        getComputedStyle(document.documentElement).getPropertyValue(
          "--queue-width",
        ),
      );
      if (Number.isFinite(value)) return value;
      return side.getBoundingClientRect().width;
    };
    let latestWidth = preferredWidth();
    const saveWidth = () => {
      try {
        localStorage.setItem("xm.queueWidth", String(Math.round(latestWidth)));
      } catch (err) {
        void err;
      }
    };
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
      latestWidth = Math.min(
        QUEUE_MAX,
        Math.max(QUEUE_MIN, startWidth - (event.clientX - startX)),
      );
      setQueueWidth(latestWidth);
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
    handle.addEventListener("dblclick", () => setSideVisible(false));
    handle.addEventListener("keydown", (event) => {
      if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
      event.preventDefault();
      const delta = event.key === "ArrowLeft" ? 16 : -16;
      latestWidth = Math.min(
        QUEUE_MAX,
        Math.max(QUEUE_MIN, preferredWidth() + delta),
      );
      setQueueWidth(latestWidth);
      saveWidth();
    });
  }

  const ADAPTER_LABEL = {
    auto: "auto（自动）",
    browser: "内置浏览器",
    mock: "mock（演示）",
  };

  function adapterLabel(name) {
    if (!name) return "";
    return ADAPTER_LABEL[name] || name;
  }

  function defaultAdapter(config, profile) {
    const saved = profile && profile.default_adapter;
    if (config && config.auto_available && (!saved || saved === "mock")) {
      return "auto";
    }
    return saved || (config && config.auto_available ? "auto" : "");
  }

  function fillAdapterSelect(select, config, selected) {
    select.replaceChildren();
    const auto = Boolean(config && config.auto_available);
    const names = (config && config.adapters) || [];
    if (auto) {
      const option = el("option", "", "auto · 自动选择");
      option.value = "auto";
      select.appendChild(option);
    }
    names.forEach((name) => {
      const option = el("option", "", name);
      option.value = name;
      select.appendChild(option);
    });
    const wanted =
      selected &&
      ((auto && selected === "auto") || names.includes(selected))
        ? selected
        : auto
          ? "auto"
          : names[0] || "";
    if (wanted) select.value = wanted;
  }

  function modelToken(providerId, modelId) {
    return providerId && modelId ? `${providerId}::${modelId}` : "";
  }

  function parseModelToken(value) {
    const text = String(value || "");
    const index = text.indexOf("::");
    if (index <= 0) return { providerId: "", modelId: "" };
    return {
      providerId: text.slice(0, index),
      modelId: text.slice(index + 2),
    };
  }

  function modelLabel(model) {
    const base =
      model.display_name && model.display_name !== model.model_id
        ? `${model.display_name} · ${model.model_id}`
        : model.model_id;
    return model.vision ? `${base} · 可识图` : base;
  }

  function fillModelSelect(select, options) {
    select.replaceChildren();
    if (options.follow) {
      const follow = el(
        "option",
        "",
        options.visionOnly ? "自动选择（需可识图）" : "跟随执行模型",
      );
      follow.value = "";
      select.appendChild(follow);
    }
    const providers = (taskState.aiChoices || [])
      .map((provider) => ({
        ...provider,
        models: (provider.models || []).filter(
          (model) => !options.visionOnly || model.vision,
        ),
      }))
      .filter((provider) => provider.models.length > 0);
    if (providers.length === 0) {
      if (!options.follow) {
        const option = el(
          "option",
          "",
          options.visionOnly
            ? "未配置可识图模型 · 无法完成识图审查"
            : "未配置模型 · 使用内置策略",
        );
        option.value = "";
        select.appendChild(option);
      }
      select.value = "";
      select.disabled = Boolean(options.follow);
      return "";
    }
    select.disabled = false;
    providers.forEach((provider) => {
      const group = document.createElement("optgroup");
      group.label = provider.name;
      provider.models.forEach((model) => {
        const option = el("option", "", modelLabel(model));
        option.value = modelToken(provider.id, model.model_id);
        group.appendChild(option);
      });
      select.appendChild(group);
    });
    const wanted = String(options.selected || "");
    const exists = Array.from(select.options).some(
      (option) => option.value === wanted,
    );
    select.value = exists ? wanted : select.options[0].value;
    return select.value;
  }

  function applyRunModelDefault() {
    const active = state.ai && state.ai.active;
    if (active) {
      taskState.runModelChoice = modelToken(
        active.provider_id,
        active.model,
      );
    }
    const chosen = fillModelSelect($("task-model"), {
      selected: taskState.runModelChoice,
    });
    taskState.runModelChoice = chosen;
  }

  function applyAuditModelSelect() {
    const chosen = fillModelSelect($("task-audit-model"), {
      follow: true,
      visionOnly: true,
      selected: taskState.auditChoice,
    });
    taskState.auditChoice = chosen;
  }

  function syncModelSelects(choices) {
    if (choices) taskState.aiChoices = choices;
    const runChosen = fillModelSelect($("task-model"), {
      selected: taskState.runModelChoice,
    });
    taskState.runModelChoice = runChosen;
    const auditChosen = fillModelSelect($("task-audit-model"), {
      follow: true,
      visionOnly: true,
      selected: taskState.auditChoice,
    });
    taskState.auditChoice = auditChosen;
  }

  async function refreshAiChoices() {
    try {
      const data = await api("/api/ai");
      syncModelSelects(
        (data.providers || []).map((provider) => ({
          id: provider.id,
          name: provider.name,
          models: provider.models || [],
        })),
      );
    } catch (err) {
      void err;
    }
  }

  async function updateAuditModel() {
    const selection = parseModelToken($("task-audit-model").value);
    try {
      const data = await api("/api/audit/model", {
        method: "POST",
        body: JSON.stringify({
          provider_id: selection.providerId,
          model_id: selection.modelId,
        }),
      });
      taskState.auditChoice = selection.providerId
        ? $("task-audit-model").value
        : "";
      const audit = data.audit || {};
      toast(
        audit.follow_executor
          ? "审计模型将跟随执行模型"
          : "审计模型已更新",
      );
    } catch (err) {
      toast(err.message, true);
      applyAuditModelSelect();
    }
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
      $("task-title-edit").disabled = false;
      $("task-context-open").disabled = false;
      $("task-conversation-sub").textContent = `${adapterLabel(detail.adapter)} · ${
        detail.audit_mode === "confirm_all" ? "每次操作都询问" : "AI 审计 + 用户确认"
      }`;
      fillAdapterSelect($("task-adapter"), state.config, detail.adapter);
      $("task-audit-mode").value = detail.audit_mode || "ai";
      renderMessages(detail.messages || []);
      connectEvents();
      refreshContext();
      setRunning(Boolean(runningRun));
      setPaused(Boolean(runningRun && runningRun.paused));
      setAwaiting(
        runningRun ? null : pendingAwait(detail.messages || []),
      );
      renderConversationList();
    } catch (err) {
      toast(`读取任务失败：${err.message}`, true);
    }
  }

  function conversationHint() {
    if (!taskState.auditVisionAvailable) {
      return "未配置可识图审计模型：回退风险操作将强制由你确认。";
    }
    return "敏感与回退风险操作由顶层识图审计 AI 判断，必要时对照屏幕截图复核；涉及用户数据时请你确认。";
  }

  function newConversation() {
    if (document.body.dataset.tab !== "run") switchTab("run");
    closeEvents();
    taskState.conversationId = "";
    taskState.conversation = null;
    taskState.activeRunId = "";
    taskState.lastEventId = 0;
    taskState.replayAfter = 0;
    taskState.paused = false;
    setRunning(false);
    setAwaiting(null);
    $("task-conversation-title").textContent = "新任务";
    $("task-title-edit").disabled = true;
    $("task-context-open").disabled = true;
    taskState.context = null;
    renderContext(null);
    $("task-conversation-sub").textContent = conversationHint();
    $("task-audit-mode").value = taskState.defaultAuditMode;
    fillAdapterSelect(
      $("task-adapter"),
      state.config,
      defaultAdapter(state.config, state.profile),
    );
    applyRunModelDefault();
    renderMessages([]);
    renderConversationList();
    $("task-goal").focus();
  }

  function isResumeText(text) {
    const compact = String(text || "").replace(/\s+/g, "");
    return (
      compact.length > 0 &&
      compact.length <= 24 &&
      /(继续|已登录|已经登录|登录好了|登录完成|可以继续)/.test(compact)
    );
  }

  async function submitGoal(event) {
    if (event) event.preventDefault();
    const goal = $("task-goal").value.trim();
    if (!goal) {
      toast("请输入任务目标", true);
      return;
    }
    const adapter = $("task-adapter").value;
    const auditMode = $("task-audit-mode").value;
    const model = parseModelToken($("task-model").value);
    const resume = Boolean(taskState.awaiting && isResumeText(goal));
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
            adapter,
            audit_mode: auditMode,
          }),
        });
        taskState.conversationId = created.conversation.id;
        taskState.conversation = created.conversation;
        $("task-conversation-title").textContent =
          created.conversation.title || "新任务";
        $("task-title-edit").disabled = false;
        $("task-context-open").disabled = false;
        refreshContext();
        taskState.replayAfter = 0;
        connectEvents();
        refreshConversations();
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
          model_provider_id: model.providerId,
          model_id: model.modelId,
          resume,
        }),
      });
      if (data.queued) {
        setAwaiting(null);
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
      $("task-conversation-sub").textContent = `${adapterLabel(
        data.conversation.adapter,
      )} · ${
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
    if (!taskState.conversationId) {
      fillAdapterSelect(
        $("task-adapter"),
        state.config,
        defaultAdapter(state.config, profile),
      );
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
    taskState.auditVisionAvailable = !(
      data.audit && data.audit.vision_available === false
    );
    taskState.nativeHotkey = Boolean(data.hotkey && data.hotkey.enabled);
    taskState.aiChoices = data.ai_choices || [];
    taskState.auditChoice = data.audit && !data.audit.follow_executor
      ? modelToken(data.audit.model_provider_id, data.audit.model_id)
      : "";
    fillAdapterSelect(
      $("task-adapter"),
      data.config,
      defaultAdapter(data.config, data.profile),
    );
    $("task-audit-mode").value = taskState.defaultAuditMode;
    applyRunModelDefault();
    applyAuditModelSelect();
    if (!taskState.conversationId) {
      $("task-conversation-sub").textContent = conversationHint();
    }
    $("task-auto-confirm").checked = Boolean(data.profile.auto_confirm);
    renderConversationList();
    renderQueue();
  }

  let titleEditing = false;

  async function enter() {
    restoreSideVisible();
    await refreshConversations();
    await refreshQueue();
    await refreshAiChoices();
  }

  function bind() {
    restoreQueueWidth();
    bindQueueResize();
    $("task-form").addEventListener("submit", submitGoal);
    $("task-await-resume").addEventListener("click", resumeAwaiting);
    $("task-new-conversation").addEventListener("click", newConversation);
    $("task-title-edit").addEventListener("click", startTitleEdit);
    $("task-context-open").addEventListener("click", () => {
      if ($("task-context-open").disabled) return;
      if (taskState.context) renderContextPanel(taskState.context);
      $("context-modal").classList.remove("hidden");
    });
    $("context-close").addEventListener("click", () => {
      $("context-modal").classList.add("hidden");
    });
    $("context-modal").addEventListener("click", (event) => {
      if (event.target === $("context-modal")) {
        $("context-modal").classList.add("hidden");
      }
    });
    $("task-side-toggle").addEventListener("click", () => {
      const hidden = $("task-workspace").classList.contains("side-hidden");
      setSideVisible(hidden);
    });
    $("task-side-close").addEventListener("click", () => setSideVisible(false));
    $("task-refresh").addEventListener("click", async () => {
      await refreshConversations();
      await refreshQueue();
    });
    $("task-pause").addEventListener("click", togglePause);
    $("task-resume").addEventListener("click", togglePause);
    $("task-banner-resume").addEventListener("click", togglePause);
    $("task-banner-ask").addEventListener("click", openScreenAsk);
    $("task-ask-submit").addEventListener("click", askScreen);
    $("task-ask-close").addEventListener("click", () =>
      $("task-ask-panel").classList.add("hidden"),
    );
    $("task-ask-question").addEventListener("keydown", (event) => {
      if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) {
        event.preventDefault();
        askScreen();
      }
    });
    $("task-cancel").addEventListener("click", cancelRun);
    $("task-banner-cancel").addEventListener("click", cancelRun);
    $("task-audit-mode").addEventListener("change", updateConversationSettings);
    $("task-adapter").addEventListener("change", updateConversationSettings);
    $("task-model").addEventListener("change", () => {
      taskState.runModelChoice = $("task-model").value;
    });
    $("task-audit-model").addEventListener("change", updateAuditModel);
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
