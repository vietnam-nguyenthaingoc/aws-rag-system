/* ============================================================
   社内文書アシスタント — client logic
   Streams from a Lambda Function URL that emits Server-Sent
   Events in the form:
     data: {"token": "文"}
     data: {"token": "字"}
     data: {"sources": [{ "title": "...", "snippet": "...", "score": 0.87, "url": "..." }]}
     data: [DONE]
   ============================================================ */

/* ---------- Config ---------- */
const CONFIG = {
  // Your Lambda Function URL (RESPONSE_STREAM mode). No trailing changes needed.
  STREAM_URL: "https://YOUR-FUNCTION-URL.lambda-url.ap-northeast-1.on.aws/",
  // Single-tenant: leave "". Multi-tenant: set an id (sent as tenant_id).
  TENANT_ID: "",
  // How many prior turns to send back as context.
  MAX_HISTORY: 10,
};
// Optional runtime override: set window.__CHAT_CONFIG = { STREAM_URL: "..." } before this script.
Object.assign(CONFIG, window.__CHAT_CONFIG || {});

/* ---------- Elements ---------- */
const streamEl = document.getElementById("stream");
const messagesEl = document.getElementById("messages");
const welcomeEl = document.getElementById("welcome");
const inputEl = document.getElementById("input");
const sendBtn = document.getElementById("send");
const stopBtn = document.getElementById("stop");
const fieldEl = document.getElementById("field");
const statusEl = document.getElementById("status");
const statusLbl = document.getElementById("statusLabel");
const domainsEl = document.getElementById("domains");

/* ---------- State ---------- */
const history = [];        // { role: "user" | "assistant", content: string }
let busy = false;
let selectedDomain = "";   // "" = 未選択（送信前に選択必須）
let controller = null;     // AbortController for the active stream

/* ---------- Markdown (graceful fallback) ---------- */
function renderMarkdown(text) {
  if (window.marked) {
    marked.setOptions({ breaks: true, gfm: true });
    return marked.parse(text);
  }
  // Fallback: escape and preserve line breaks
  return escapeHtml(text).replace(/\n/g, "<br>");
}
function escapeHtml(s) {
  const d = document.createElement("div");
  d.textContent = s;
  return d.innerHTML;
}

/* ---------- Status ---------- */
function setStatus(state, label) {
  statusEl.className = "status" + (state ? " " + state : "");
  statusLbl.textContent = label;
}

/* ---------- Message builders ---------- */
function addUserMessage(text) {
  if (welcomeEl) welcomeEl.remove();
  const msg = document.createElement("div");
  msg.className = "msg user";
  const bubble = document.createElement("div");
  bubble.className = "bubble";
  bubble.textContent = text;           // escaped by definition
  msg.appendChild(bubble);
  messagesEl.appendChild(msg);
  scrollToBottom();
}

function addBotMessage() {
  const msg = document.createElement("div");
  msg.className = "msg bot";

  const bubble = document.createElement("div");
  bubble.className = "bubble";
  bubble.innerHTML = '<span class="typing"><span></span><span></span><span></span></span>';

  msg.appendChild(bubble);
  messagesEl.appendChild(msg);
  scrollToBottom();
  return { msg, bubble };
}

function renderSources(msgEl, sources) {
  if (!Array.isArray(sources) || sources.length === 0) return;
  const box = document.createElement("div");
  box.className = "sources";
  box.innerHTML = '<p class="sources-label">出典</p>';

  sources.forEach((s, i) => {
    const tag = s.url ? "a" : "div";
    const el = document.createElement(tag);
    el.className = "source";
    if (s.url) { el.href = s.url; el.target = "_blank"; el.rel = "noopener noreferrer"; }

    const score = (typeof s.score === "number")
      ? `<span class="source-score">${s.score.toFixed(2)}</span>` : "";

    el.innerHTML =
      `<span class="source-num">${i + 1}</span>` +
      `<div class="source-body">` +
      `<p class="source-title">${escapeHtml(s.title || s.filename || "無題の文書")}${score}</p>` +
      (s.snippet ? `<p class="source-snippet">${escapeHtml(s.snippet)}</p>` : "") +
      `</div>`;
    box.appendChild(el);
  });
  msgEl.appendChild(box);
  scrollToBottom();
}

function scrollToBottom() {
  streamEl.scrollTop = streamEl.scrollHeight;
}

/* ---------- Send / stream ---------- */
async function send(text) {
  text = text.trim();
  if (busy) return;
  if (!text) {
    alert("質問を入力してください。");
    inputEl.focus();
    return;
  }
  if (!selectedDomain) {
    alert("質問する分野を選択してください。");
    return;
  }

  addUserMessage(text);
  history.push({ role: "user", content: text });

  inputEl.value = "";
  autosize();
  refreshSend();
  setBusy(true);

  const { msg, bubble } = addBotMessage();
  let full = "";
  let started = false;

  controller = new AbortController();

  try {
    const payload = {
      message: text,
      history: history.slice(-CONFIG.MAX_HISTORY),
    };
    if (CONFIG.TENANT_ID) payload.tenant_id = CONFIG.TENANT_ID;
    if (selectedDomain) payload.domain = selectedDomain;

    const res = await fetch(CONFIG.STREAM_URL, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
      signal: controller.signal,
    });

    if (!res.ok || !res.body) {
      throw new Error("bad_response");
    }

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";

    while (true) {
      const { value, done } = await reader.read();
      if (done) break;

      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split("\n");
      buffer = lines.pop();            // keep any partial trailing line

      for (const raw of lines) {
        const line = raw.trim();
        if (!line.startsWith("data:")) continue;

        const data = line.slice(5).trim();
        if (data === "[DONE]") continue;

        let parsed;
        try { parsed = JSON.parse(data); }
        catch { continue; }            // skip malformed chunk

        if (parsed.token) {
          if (!started) { bubble.innerHTML = ""; started = true; }
          full += parsed.token;
          bubble.innerHTML = renderMarkdown(full);
          scrollToBottom();
        }
        if (parsed.sources) {
          renderSources(msg, parsed.sources);
        }
        if (parsed.error) {
          throw new Error(parsed.error);
        }
      }
    }

    if (!started) {
      bubble.classList.add("is-error");
      bubble.textContent = "回答を取得できませんでした。もう一度お試しください。";
    } else {
      history.push({ role: "assistant", content: full });
    }
  } catch (err) {
    if (err.name === "AbortError") {
      // User stopped: keep whatever streamed so far
      if (started) {
        history.push({ role: "assistant", content: full });
      } else {
        bubble.classList.add("is-error");
        bubble.textContent = "生成を停止しました。";
      }
    } else {
      bubble.classList.add("is-error");
      bubble.textContent = "サーバーに接続できませんでした。ネットワークと接続先を確認してください。";
      setStatus("error", "接続エラー");
    }
  } finally {
    controller = null;
    setBusy(false);
  }
}

function setBusy(v) {
  busy = v;
  sendBtn.disabled = v;
  sendBtn.hidden = v;
  stopBtn.hidden = !v;
  if (v) {
    setStatus("busy", "回答を生成中…");
  } else {
    setStatus("", "準備完了");
    inputEl.focus();
  }
}

/* ---------- Composer behavior ---------- */
function autosize() {
  inputEl.style.height = "auto";
  inputEl.style.height = Math.min(inputEl.scrollHeight, 200) + "px";
}

function refreshSend() {
  sendBtn.classList.toggle("locked", !inputEl.value.trim() || !selectedDomain);
}

inputEl.addEventListener("input", () => { autosize(); refreshSend(); });
inputEl.addEventListener("focus", () => fieldEl.classList.add("focused"));
inputEl.addEventListener("blur", () => fieldEl.classList.remove("focused"));

inputEl.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
    e.preventDefault();
    send(inputEl.value);
  }
});

sendBtn.addEventListener("click", () => send(inputEl.value));
stopBtn.addEventListener("click", () => { if (controller) controller.abort(); });

domainsEl?.addEventListener("click", (e) => {
  const card = e.target.closest(".domain-card");
  if (!card) return;
  selectedDomain = card.dataset.domain;
  domainsEl.querySelectorAll(".domain-card").forEach((c) =>
    c.setAttribute("aria-checked", String(c === card))
  );
  refreshSend();
});

/* ---------- Init ---------- */
inputEl.focus();