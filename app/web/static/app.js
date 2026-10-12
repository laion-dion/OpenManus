/* ================================================================
   OpenManus Web Console — client runtime
   ----------------------------------------------------------------
   * Chat: SSE token stream rendered char-by-char via a rAF-driven
     queue (never blocks the main thread / rendering).
   * Logs: retractable CLI-style terminal fed by the "logs" SSE
     channel; prints each tool the Python agent triggers, including
     the bash scripts being executed.
   ================================================================ */
"use strict";

(() => {
  // ---------------------------- helpers ----------------------------
  const $ = (sel) => document.querySelector(sel);
  const el = (tag, cls, text) => {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined) n.textContent = text;
    return n;
  };
  const escapeHtml = (s) =>
    String(s).replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[c]));

  const sessionId =
    sessionStorage.getItem("openmanus.session") ||
    (() => {
      const id = "web-" + Math.random().toString(36).slice(2, 10);
      sessionStorage.setItem("openmanus.session", id);
      return id;
    })();

  // ============================ CHAT ==============================
  const messagesEl = $("#messages");
  const composerEl = $("#composer");
  const inputEl = $("#prompt-input");
  const sendBtn = $("#send-btn");
  const statusEl = $("#conn-status");

  let currentBubble = null;   // bubble receiving streamed tokens
  let streaming = false;

  function setStatus(on) {
    statusEl.textContent = on ? "live" : "offline";
    statusEl.classList.toggle("status-on", on);
    statusEl.classList.toggle("status-off", !on);
  }

  function addMessage(role, text) {
    const msg = el("div", `msg msg-${role}`);
    msg.appendChild(el("div", "avatar", role === "user" ? "☺" : "◐"));
    const bubble = el("div", "bubble");
    bubble.textContent = text;
    msg.appendChild(bubble);
    messagesEl.appendChild(msg);
    scrollChat();
    return bubble;
  }

  function scrollChat() {
    messagesEl.scrollTop = messagesEl.scrollHeight;
  }

  // ---- Token render queue: SSE deltas land in a buffer and are
  //      flushed to the DOM one animation frame at a time, so the
  //      UI keeps painting smoothly under bursty streams. ----
  const pendingChunks = [];
  let rafId = null;

  function enqueueDelta(text) {
    pendingChunks.push(text);
    if (rafId === null) rafId = requestAnimationFrame(flushDeltas);
  }

  function flushDeltas() {
    rafId = null;
    if (!currentBubble) { pendingChunks.length = 0; return; }
    if (pendingChunks.length) {
      currentBubble.appendText(pendingChunks.join(""));
      pendingChunks.length = 0;
      scrollChat();
    }
  }

  function beginStream() {
    streaming = true;
    sendBtn.disabled = true;
    currentBubble = addMessage("agent", "");
    currentBubble.classList.add("streaming");
  }

  function endStream(finalText) {
    if (rafId !== null) { cancelAnimationFrame(rafId); rafId = null; }
    flushDeltas();
    if (currentBubble) {
      currentBubble.classList.remove("streaming");
      // If no tokens were streamed (non-stream model), show final result.
      if (finalText && currentBubble.textContent.trim() === "") {
        currentBubble.textContent = finalText;
      }
    }
    currentBubble = null;
    streaming = false;
    sendBtn.disabled = false;
    inputEl.focus();
  }

  // Composer behaviour ---------------------------------------------------
  inputEl.addEventListener("input", () => {
    inputEl.style.height = "auto";
    inputEl.style.height = Math.min(inputEl.scrollHeight, 160) + "px";
  });

  inputEl.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      composerEl.requestSubmit();
    }
  });

  composerEl.addEventListener("submit", async (e) => {
    e.preventDefault();
    const prompt = inputEl.value.trim();
    if (!prompt || streaming) return;
    addMessage("user", prompt);
    inputEl.value = "";
    inputEl.style.height = "auto";
    try {
      const res = await fetch("/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ prompt, session_id: sessionId }),
      });
      if (!res.ok) throw new Error(await res.text());
    } catch (err) {
      addMessage("agent", "⚠ Failed to reach the agent server: " + err.message);
    }
  });

  // Chat SSE feed ---------------------------------------------------------
  function connectChatStream() {
    const es = new EventSource("/api/stream/chat");
    es.onopen = () => setStatus(true);
    es.onerror = () => {
      setStatus(false);
      es.close();
      setTimeout(connectChatStream, 2000); // auto-reconnect
    };
    es.onmessage = (ev) => {
      let data;
      try { data = JSON.parse(ev.data); } catch { return; }
      switch (data.type) {
        case "turn_start":
          beginStream();
          break;
        case "delta":
          if (!currentBubble) beginStream();
          enqueueDelta(data.text || "");
          break;
        case "turn_result":
          if (currentBubble && data.result) {
            // Prefer streamed text; fall back to the authoritative result.
            currentBubble._fallback = data.result;
          }
          break;
        case "turn_end":
          endStream(currentBubble && currentBubble._fallback);
          break;
        case "error":
          endStream(null);
          addMessage("agent", "⚠ " + (data.message || "Agent error"));
          break;
      }
    };
  }
  connectChatStream();

  // ============================ LOGS =================================
  const logsPanel = $("#logs-panel");
  const logsBody = $("#logs-body");
  const toggleLogsBtn = $("#toggle-logs");
  const logsCloseBtn = $("#logs-close");
  const logsClearBtn = $("#logs-clear");
  const logsFollowBtn = $("#logs-autoscroll");
  const logsResizer = $("#logs-resizer");

  let followLogs = true;
  const MAX_LOG_LINES = 2000;

  function logsOpen() { return !logsPanel.classList.contains("collapsed"); }

  function setLogsOpen(open) {
    logsPanel.classList.toggle("collapsed", !open);
    logsPanel.setAttribute("aria-hidden", String(!open));
    toggleLogsBtn.classList.toggle("active", open);
    if (open) scrollLogs(true);
  }

  function scrollLogs(force) {
    if (force || followLogs) logsBody.scrollTop = logsBody.scrollHeight;
  }

  logsBody.addEventListener("scroll", () => {
    const atBottom =
      logsBody.scrollHeight - logsBody.scrollTop - logsBody.clientHeight < 24;
    if (!atBottom && followLogs) setFollow(false);
    else if (atBottom && !followLogs) setFollow(true);
  });

  function setFollow(v) {
    followLogs = v;
    logsFollowBtn.classList.toggle("active", v);
  }

  toggleLogsBtn.addEventListener("click", () => setLogsOpen(!logsOpen()));
  logsCloseBtn.addEventListener("click", () => setLogsOpen(false));
  logsClearBtn.addEventListener("click", () => { logsBody.innerHTML = ""; });
  logsFollowBtn.addEventListener("click", () => {
    setFollow(!followLogs);
    if (followLogs) scrollLogs(true);
  });

  document.addEventListener("keydown", (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key === "`") {
      e.preventDefault();
      setLogsOpen(!logsOpen());
    }
  });

  // Drag-to-resize (desktop layout only)
  (() => {
    let dragging = false;
    logsResizer.addEventListener("mousedown", (e) => {
      if (window.innerWidth <= 900) return;
      dragging = true;
      e.preventDefault();
      document.body.style.cursor = "col-resize";
    });
    window.addEventListener("mousemove", (e) => {
      if (!dragging) return;
      const w = Math.min(Math.max(window.innerWidth - e.clientX, 260), window.innerWidth - 320);
      logsPanel.style.flexBasis = w + "px";
    });
    window.addEventListener("mouseup", () => {
      dragging = false;
      document.body.style.cursor = "";
    });
  })();

  // Log line builders ------------------------------------------------------
  function appendLogLine(node) {
    logsBody.appendChild(node);
    while (logsBody.childElementCount > MAX_LOG_LINES) logsBody.removeChild(logsBody.firstChild);
    scrollLogs();
  }

  function logPlain(kind, html) {
    const line = el("span", "log-line " + kind);
    line.innerHTML = html + "\n";
    appendLogLine(line);
  }

  function renderLogEvent(d) {
    const ts = `<span class="ts">${escapeHtml(d.ts || "")}</span> `;
    const lvl = `<span class="lvl-${escapeHtml(d.level || "INFO")}">[${escapeHtml((d.level || "INFO").padEnd(8))}</span>`;
    const mod = d.module ? ` <span class="mod">${escapeHtml(d.module)}</span>` : "";
    logPlain("log-generic", `${ts}${lvl}${mod} ${escapeHtml(d.message || "")}`);
  }

  function renderToolStart(d) {
    const head =
      `<span class="ts">${escapeHtml(d.ts || new Date().toTimeString().slice(0, 8))}</span> ` +
      `<span class="toolname">▶ ${escapeHtml(d.tool || "?")}</span>` +
      (d.agent ? ` <span class="mod">(${escapeHtml(d.agent)})</span>` : "");
    let block = head;
    if (d.command) {
      block += `\n$ ${escapeHtml(d.command)}`;
    } else if (d.args && Object.keys(d.args).length) {
      block += `\n<span class="cmd">${escapeHtml(JSON.stringify(d.args))}</span>`;
    }
    logPlain("log-tool-start", block);
  }

  function renderToolEnd(d) {
    const head =
      `<span class="ts">${escapeHtml(d.ts || new Date().toTimeString().slice(0, 8))}</span> ` +
      `<span class="toolname">◀ ${escapeHtml(d.tool || "?")}</span> done`;
    let block = head;
    if (d.result) {
      block += `\n<span class="result">${escapeHtml(d.result)}</span>`;
    }
    logPlain("log-tool-end", block);
  }

  // Logs SSE feed -----------------------------------------------------------
  function connectLogsStream() {
    const es = new EventSource("/api/stream/logs");
    es.onmessage = (ev) => {
      let d;
      try { d = JSON.parse(ev.data); } catch { return; }
      switch (d.kind) {
        case "hello":
          logPlain("log-sep",
            `── connected ${escapeHtml(d.ts || "")} · agentic log stream ──`);
          break;
        case "log":
          renderLogEvent(d);
          break;
        case "tool_start":
          renderToolStart(d);
          break;
        case "tool_end":
          renderToolEnd(d);
          break;
        default:
          renderLogEvent(d);
      }
    };
    es.onerror = () => {
      es.close();
      setTimeout(connectLogsStream, 3000);
    };
  }
  connectLogsStream();

  // Start collapsed on small screens, open on wide desktops.
  setLogsOpen(window.innerWidth > 1100);
  inputEl.focus();
})();
