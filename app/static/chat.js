"use strict";

function message(row) {
  const request = esc(row.request_id || "");
  if (row.speaker === "USER")
    return `<article class="message user" data-request="${request}" aria-label="我的提问"><div class="pre">${esc(row.content)}</div></article>`;
  const refs = (row.citations || []).map((c) =>
    act("〔" + c.order_no + "〕" + c.source.title + " · V" + c.source.version_no + " · " + c.locator, () => cite(c.citation_id)),
  ).join("");
  return `<article class="message assistant" data-request="${request}" aria-label="系统回答">
    <h3><span class="answer-mark" aria-hidden="true">章</span>${esc(lab(row.status))}</h3>
    <div class="answer">${esc(row.content)}</div>
    ${refs ? `<div class="citations">${refs}</div>` : ""}
    <div class="actions compact"><small>${dt(row.created_at)}</small>${act("评价与纠错", () => feedbackModal(row.message_id), "link")}</div>
  </article>`;
}

function welcome(faqs) {
  return `<div class="chat-home" id="chat-welcome"><h2>今天想了解哪条规定？</h2>
    <p>输入问题后，可从答复中的引用查看对应条文。</p>
    <div class="featured"><div><small>收录原文</small><b>中国共产党章程</b><small>共产党员网 · 2022 年 10 月 22 日通过</small></div>
    <img class="thumb" src="/static/constitution-page.webp" alt="中国共产党章程原文首页"></div>
    <div class="topic-list">${faqs.map((f) => act(f.title, () => startQuestion(f.question, f.faq_id))).join("")}</div></div>`;
}

async function chatView() {
  const chat = state.chat;
  const [filters, faqs] = await Promise.all([
    get("/knowledge/filters"),
    chat.sid ? [] : get("/faqs?page_size=3").then((data) => data.items),
  ]);
  let rows, revision;
  do {
    revision = chat.revision;
    rows = [];
    if (chat.sid) {
      let page = 1, batch;
      do {
        batch = await get(`/sessions/${chat.sid}/messages?page_size=100&page=${page++}`);
        rows.push(...batch.items);
      } while (rows.length < batch.total && batch.items.length);
      chat.title = batch.session_title;
    }
  } while (revision !== chat.revision);
  if (rows.some((r) => r.request_id === chat.resumeKey)) chat.resumeKey = null;

  return head(chat.title || "知识问答", act("新建会话", freshChat)) +
    `<div class="chat-frame">
      <section id="chat-thread" aria-label="对话记录" tabindex="0">
        ${rows.length || chat.turn || chat.resumeKey ? "" : welcome(faqs)}
        <div class="messages" id="chat-messages">${rows.map(message).join("")}</div>
      </section>
      <button type="button" id="chat-latest" class="chat-latest" hidden>↓ 回到最新</button>
    </div>
    <form id="composer" class="composer">
      <textarea id="question" aria-label="输入问题" aria-keyshortcuts="Enter Control+Enter" rows="2" maxlength="2000" placeholder="输入问题，例如：预备党员的预备期为多长时间？" required></textarea>
      <div class="controls">
        <div class="actions"><select aria-label="限定资料分类" id="chat-cat">
          <option value="">全部分类</option>${options(filters.categories, "category_id", "name", "")}</select>
          <label><input id="reset-topic" type="checkbox">切换话题</label></div>
        <div class="composer-submit"><small class="composer-shortcut">Enter 发送 · Shift + Enter 换行</small>
          <button type="submit" class="primary" id="send">发送问题</button></div>
      </div>
    </form><div id="chat-announce" class="sr-only" role="status"></div>`;
}

function nearLatest(thread) {
  return thread.scrollHeight - thread.scrollTop - thread.clientHeight < 48;
}
function updateLatestButton() {
  const thread = $("chat-thread");
  if (thread) $("chat-latest").hidden = Boolean($("chat-welcome")) || nearLatest(thread);
}
function scrollLatest() {
  const thread = $("chat-thread");
  thread.scrollTop = thread.scrollHeight;
  updateLatestButton();
}
function visibleChat(chat) {
  return state.chat === chat && state.tab === "ask" && $("chat-messages");
}
function savedOnScreen(key) {
  return $("chat-messages").querySelector(`[data-request="${CSS.escape(key)}"]`);
}
function drawTurn(chat) {
  const turn = chat.turn;
  $("chat-turn")?.remove();
  $("chat-welcome")?.remove();
  $("send").disabled = !turn.error;
  if (savedOnScreen(turn.key)) return;
  const waiting = turn.restoring ? "正在读取本轮答复…" : "正在检索资料并核对引用…";
  const assistant = turn.error
    ? `<div class="turn-error" role="alert">${esc(turn.error)} ${turn.interrupted ? "" : act(turn.restoring ? "重新读取" : "重试", () => retryTurn(chat), "link")}</div>`
    : `<div class="turn-wait" role="status"><span class="loading-icon" aria-hidden="true"></span>${waiting}</div>`;
  $("chat-messages").insertAdjacentHTML("beforeend",
    `<div id="chat-turn" class="chat-turn">${turn.question ? message({speaker:"USER", content:turn.question}) : ""}${assistant}</div>`);
}

function bindComposer() {
  const chat = state.chat;
  const form = $("composer"), question = $("question");
  form.onsubmit = (event) => {
    event.preventDefault();
    submitQuestion(question.value.trim(), null);
  };
  question.onkeydown = (event) => {
    if (event.key !== "Enter" || event.shiftKey || event.isComposing || event.keyCode === 229) return;
    event.preventDefault();
    if (!event.repeat && !$("send").disabled) form.requestSubmit();
  };
  $("chat-thread").onscroll = updateLatestButton;
  $("chat-latest").onclick = scrollLatest;
  if (chat.turn) {
    drawTurn(chat);
  } else if (chat.resumeKey) {
    const turn = {key:chat.resumeKey, restoring:true};
    chat.resumeKey = null;
    chat.turn = turn;
    pendingChats.set(chat.sid, chat);
    drawTurn(chat);
    deliver(chat, turn);
  }
  rememberChat();
  if (!$("chat-welcome")) scrollLatest();
}

async function startQuestion(question, faqId) {
  if (state.tab !== "ask") await freshChat();
  if ($("composer") && state.tab === "ask") submitQuestion(question, faqId);
}
function submitQuestion(question, faqId) {
  const chat = state.chat;
  if (!question || (chat.turn && !chat.turn.error)) return;
  const payload = {
    ...(faqId ? {faq_id: faqId} : {question}),
    category_id: $("chat-cat").value || null,
    reset_context: $("reset-topic").checked,
  };
  const previous = chat.turn;
  const sameAttempt = previous?.error && JSON.stringify(previous.payload) === JSON.stringify(payload);
  const turn = sameAttempt ? previous : {key:crypto.randomUUID(), question, payload};
  chat.turn = turn;
  turn.error = "";
  $("question").value = "";
  $("reset-topic").checked = false;
  if (matchMedia("(pointer: fine)").matches) $("question").focus({preventScroll:true});
  drawTurn(chat);
  scrollLatest();
  deliver(chat, turn);
}
function retryTurn(chat) {
  if (!visibleChat(chat) || !chat.turn?.error) return;
  const turn = chat.turn;
  turn.error = "";
  if ($("question").value === turn.question) $("question").value = "";
  drawTurn(chat);
  scrollLatest();
  return deliver(chat, turn);
}

async function savedTurn(chat, key) {
  const path = `/sessions/${chat.sid}/messages?request_id=${encodeURIComponent(key)}`;
  const started = Date.now();
  while (chat.owner === state.me) {
    const pair = await get(path);
    if (pair.items.some((r) => r.speaker === "ASSISTANT")) return pair;
    if (pair.request_state === "EXPIRED" || pair.request_state === "DONE") {
      const error = new Error("上次请求已中断，可以重新提问。");
      error.finished = true;
      throw error;
    }
    if (!pair.request_state && Date.now() - started > 5000)
      throw new Error("暂未取得本轮答复，请重试或重新提问。");
    await new Promise((resolve) => setTimeout(resolve, 850));
  }
}
async function deliver(chat, turn) {
  try {
    if (!chat.sid) {
      const session = await post("/sessions", {});
      if (chat.owner !== state.me) return;
      chat.sid = session.session_id;
    }
    pendingChats.set(chat.sid, chat);
    if (visibleChat(chat)) rememberChat();
    let truncated = false;
    if (!turn.restoring) {
      const response = await api(`/sessions/${chat.sid}/questions`, {
        method:"POST", data:turn.payload, key:turn.key, terminal:true,
      });
      truncated = response.data?.context_truncated;
    }
    const pair = await savedTurn(chat, turn.key);
    if (!pair || chat.owner !== state.me) return;
    chat.turn = null;
    chat.revision++;
    chat.title = pair.session_title;
    pendingChats.delete(chat.sid);
    if (!visibleChat(chat)) return;

    const thread = $("chat-thread"), list = $("chat-messages");
    const follow = nearLatest(thread), readingPosition = thread.scrollTop;
    $("chat-turn")?.remove();
    if (!savedOnScreen(turn.key)) list.insertAdjacentHTML("beforeend", pair.items.map(message).join(""));
    $("main").querySelector("h1").textContent = chat.title;
    $("send").disabled = false;
    const answer = pair.items.find((r) => r.speaker === "ASSISTANT");
    if (answer.status === "SERVICE_ERROR" && !$("question").value) $("question").value = turn.question || "";
    $("chat-announce").textContent = truncated ? "本轮答复已完成，上下文已按预算截断。" : "本轮答复已完成。";
    if (truncated) say("本轮上下文已按预算截断。");
    if (follow) {
      const node = list.querySelector(`.assistant[data-request="${CSS.escape(turn.key)}"]`);
      thread.scrollTop += node.getBoundingClientRect().top - thread.getBoundingClientRect().top - 12;
    } else thread.scrollTop = readingPosition;
    updateLatestButton();
    rememberChat();
  } catch (err) {
    if (chat.owner !== state.me) return;
    if (err.finished) {
      if (turn.restoring) turn.interrupted = true;
      else turn.key = crypto.randomUUID();
    }
    turn.error = err.message;
    if (visibleChat(chat)) {
      const follow = nearLatest($("chat-thread"));
      drawTurn(chat);
      if (follow) scrollLatest();
    }
  }
}
