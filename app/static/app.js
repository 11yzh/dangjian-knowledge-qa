"use strict";
const $ = (id) => document.getElementById(id);
const esc = (value) =>
  String(value ?? "").replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        c
      ],
  );
const state = {
  me: null,
  tab: "ask",
  sid: null,
  title: "",
  history: [],
  pending: false,
  retry: null,
  filter: {},
  pages: {},
  serial: 0,
};
const labels = {
  USER: "普通用户",
  KNOWLEDGE_ADMIN: "知识管理员",
  SYSTEM_ADMIN: "系统管理员",
  ACTIVE: "启用",
  INACTIVE: "未启用",
  DISABLED: "停用",
  PUBLISHED: "已发布",
  REVIEW_PENDING: "待审核",
  UPLOADED: "已上传",
  PROCESSING: "处理中",
  FAILED: "失败",
  REJECTED: "已退回",
  WITHDRAWN: "已撤回",
  ARCHIVED: "已归档",
  PENDING: "待处理",
  RESOLVED: "已解决",
  SUCCESS: "有据回答",
  REFUSED: "已拒答",
  NO_EVIDENCE: "未找到依据",
  SERVICE_ERROR: "服务异常",
  USEFUL: "有用",
  NOT_USEFUL: "需要纠正",
};
const lab = (x) => labels[x] || x || "—";
const dt = (x) =>
  x
    ? new Date(x).toLocaleString("zh-CN", {
        timeZone: "Asia/Shanghai",
        hour12: false,
      })
    : "—";
const acts = new Map();
let actionN = 0,
  noticeTimer;
function act(label, fn, css = "") {
  const key = String(++actionN);
  acts.set(key, fn);
  return `<button type="button" class="${esc(css)}" data-action="${key}">${esc(label)}</button>`;
}
function say(text) {
  $("notice").textContent = text;
  clearTimeout(noticeTimer);
  noticeTimer = setTimeout(() => ($("notice").textContent = ""), 5500);
}
document.addEventListener("click", async (e) => {
  const b = e.target.closest("[data-action]");
  if (!b) return;
  const action = acts.get(b.dataset.action);
  if (!action) return;
  b.disabled = true;
  try {
    await action();
  } catch (err) {
    showError(err);
  } finally {
    b.disabled = false;
  }
});
function showError(err) {
  const node = $("dialog").open ? $("modal-error") : null;
  if (node) node.textContent = err.message;
  else say(err.message);
}
async function api(
  path,
  { method = "GET", data, form, key, terminal = false } = {},
) {
  const headers = {};
  if (method !== "GET") {
    headers["X-CSRF-Token"] = state.me?.csrf_token || "";
    headers["Idempotency-Key"] = key || crypto.randomUUID();
  }
  if (data !== undefined) headers["Content-Type"] = "application/json";
  let res;
  try {
    res = await fetch("/api/v1" + path, {
      method,
      headers,
      body: form || (data !== undefined ? JSON.stringify(data) : undefined),
    });
  } catch {
    throw new Error("网络连接中断，可以重试。");
  }
  const obj = await res.json();
  if (!res.ok && !(terminal && obj.data?.status === "SERVICE_ERROR")) {
    if (res.status === 401 && path !== "/auth/login") {
      state.me = null;
      loginScreen();
    }
    throw new Error(
      (obj.message || "操作失败。") +
        (obj.request_id ? "  请求 " + obj.request_id.slice(0, 8) : ""),
    );
  }
  return obj;
}
const get = async (path) => (await api(path)).data;
const post = async (path, data = {}, method = "POST") =>
  (await api(path, { method, data })).data;
function formData(form) {
  return Object.fromEntries(new FormData(form));
}
function options(rows, value, label, selected) {
  return rows
    .map(
      (r) =>
        `<option value="${esc(r[value])}" ${String(r[value]) === String(selected) ? "selected" : ""}>${esc(r[label])}</option>`,
    )
    .join("");
}
function input(name, label, value = "", type = "text", extra = "") {
  return `<label>${esc(label)}<input name="${esc(name)}" type="${esc(type)}" value="${esc(value)}" ${extra}></label>`;
}
function area(name, label, value = "", extra = "") {
  return `<label>${esc(label)}<textarea name="${esc(name)}" rows="3" ${extra}>${esc(value)}</textarea></label>`;
}
function select(name, label, values, selected) {
  return `<label>${esc(label)}<select name="${esc(name)}">${values.map(([k, v]) => `<option value="${esc(k)}" ${String(k) === String(selected) ? "selected" : ""}>${esc(v)}</option>`).join("")}</select></label>`;
}
function detail(pairs) {
  return (
    '<dl class="detail">' +
    pairs
      .map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v ?? "—")}</dd>`)
      .join("") +
    "</dl>"
  );
}
function record(title, meta, actions = "", badge = "") {
  return `<div class="record"><div><strong>${esc(title)} ${badge ? `<span class="badge">${esc(badge)}</span>` : ""}</strong><p>${esc(meta)}</p></div><div class="actions">${actions}</div></div>`;
}
function head(title, actions = "", hint = "") {
  return `<header class="page-head"><div><h1>${esc(title)}</h1>${hint ? `<small>${esc(hint)}</small>` : ""}</div><div class="actions">${actions}</div></header>`;
}
function modal(title, body, submit, label = "保存") {
  const d = $("dialog");
  if (d.open) d.close();
  $("dialog-inside").innerHTML =
    `<header><h2 id="dialog-heading">${esc(title)}</h2>${act("关闭", () => d.close(), "link")}</header><form class="formgrid" id="entry-form">${body}<p class="errorbox" id="modal-error" role="alert"></p>${submit ? `<div class="form-actions"><button class="primary" type="submit">${esc(label)}</button></div>` : ""}</form>`;
  if (submit)
    $("entry-form").onsubmit = async (e) => {
      e.preventDefault();
      const f = e.currentTarget;
      const b = f.querySelector("[type=submit]");
      b.disabled = true;
      try {
        await submit(formData(f), f);
        if (d.open) d.close();
      } catch (err) {
        showError(err);
      } finally {
        b.disabled = false;
      }
    };
  d.showModal();
}
function askConfirm(title, body, yes, callback) {
  modal(title, `<p>${esc(body)}</p>`, callback, yes);
}
function pager(data, fn) {
  if (data.total <= data.page_size) return "";
  const p = data.page;
  return `<div class="pager">${p > 1 ? act("上一页", () => fn(p - 1)) : ""}<span>第 ${p} / ${Math.ceil(data.total / data.page_size)} 页</span>${p * data.page_size < data.total ? act("下一页", () => fn(p + 1)) : ""}</div>`;
}
function list(items) {
  return (
    '<div class="list">' +
    (items.join("") || '<p class="empty">暂无符合条件的记录。</p>') +
    "</div>"
  );
}
function loginScreen() {
  acts.clear();
  if ($("dialog").open) $("dialog").close();
  $("root").innerHTML =
    `<main class="login"><section class="login-story"><div class="brand"><span class="mark">章</span>党建知识问答</div><div class="story-body"><h1>党建知识<br>问答系统</h1><p>阅读党章原文，查询条文依据，保存学习记录。</p></div><div class="story-source">收录《中国共产党章程》<br>2022 年 10 月 22 日通过</div><div class="page-stack"><img alt="" src="/static/constitution-page.webp"></div></section><section class="login-entry"><h2>账号登录</h2><p class="muted">使用项目管理员分配的账号进入系统。</p><form id="login" class="formgrid">${input("username", "用户名", "", "text", 'autocomplete="username" required maxlength="64"')}${input("password", "密码", "", "password", 'autocomplete="current-password" required')}<div id="login-error" role="alert" class="errorbox"></div><button type="submit" class="primary">登录</button></form></section></main>`;
  $("login").onsubmit = async (e) => {
    e.preventDefault();
    const b = e.currentTarget.querySelector("button");
    b.disabled = true;
    try {
      state.me = (
        await api("/auth/login", {
          method: "POST",
          data: formData(e.currentTarget),
        })
      ).data;
      state.sid = null;
      await navigate("ask");
    } catch (err) {
      $("login-error").textContent = err.message;
    } finally {
      b.disabled = false;
    }
  };
}
const navNames = {
  ask: "知识问答",
  library: "知识目录",
  history: "历史会话",
  faqs: "常见问题",
  feedback: "我的反馈",
  versions: "知识维护",
  catalog: "分类与常见问题",
  inbox: "纠错处理",
  users: "账号与权限",
  safety: "安全规则",
  configs: "模型配置",
  stats: "运营统计",
  audit: "审计日志",
};
function allowed() {
  return [
    "ask",
    "library",
    "history",
    "faqs",
    "feedback",
    ...(state.me.role_code === "KNOWLEDGE_ADMIN"
      ? ["versions", "catalog", "inbox"]
      : []),
    ...(state.me.role_code === "SYSTEM_ADMIN"
      ? ["users", "safety", "configs", "stats", "audit"]
      : []),
  ];
}
async function logout() {
  await post("/auth/logout");
  state.me = null;
  state.history = [];
  state.sid = null;
  location.hash = "";
  loginScreen();
}
async function navigate(tab) {
  if (!state.me) {
    loginScreen();
    return;
  }
  acts.clear();
  state.tab = allowed().includes(tab) ? tab : "ask";
  const ticket = ++state.serial;
  if (location.hash.slice(1) !== state.tab)
    history.replaceState(null, "", "#" + state.tab);
  $("root").innerHTML =
    `<a class="skip" href="#main">跳到主内容</a><div class="layout"><aside class="side"><div class="brand"><span class="mark">章</span>党建知识问答</div><nav aria-label="主导航">${allowed()
      .map(
        (k) =>
          `<button type="button" data-nav="${k}" aria-current="${k === state.tab ? "page" : "false"}">${navNames[k]}</button>`,
      )
      .join(
        "",
      )}</nav><div class="side-foot"><b>${esc(state.me.display_name)}</b><span class="role">${lab(state.me.role_code)}</span>${state.me.roles.length > 1 ? `<select id="role-select" aria-label="切换角色">${state.me.roles.map((r) => `<option value="${r.role_code}" ${r.role_code === state.me.role_code ? "selected" : ""}>${lab(r.role_code)}</option>`).join("")}</select>` : ""}<div class="actions">${act("修改密码", passwordModal, "link")} ${act("退出", logout, "link")}</div></div></aside><main class="content" id="main" tabindex="-1"><p class="loading">正在读取…</p></main></div>`;
  document
    .querySelectorAll("[data-nav]")
    .forEach((b) => (b.onclick = () => navigate(b.dataset.nav)));
  if ($("role-select"))
    $("role-select").onchange = async (e) => {
      state.me = await post("/auth/role", { role_code: e.target.value }, "PUT");
      state.sid = null;
      state.history = [];
      await navigate("ask");
    };
  if (state.me.must_change_password) {
    $("main").innerHTML =
      head("首次登录") + "<p>请修改初始密码后重新登录。</p>";
    passwordModal();
    return;
  }
  try {
    const markup = await (
      {
        ask: chatView,
        library: libraryView,
        history: historyView,
        faqs: faqView,
        feedback: feedbackView,
      }[state.tab] || (() => manageView(state.tab))
    )();
    if (ticket !== state.serial) return;
    $("main").innerHTML = markup;
    if (state.tab === "ask") bindComposer();
    if ($("filters"))
      $("filters").onsubmit = (e) => {
        e.preventDefault();
        state.filter[state.tab] = formData(e.currentTarget);
        state.pages[state.tab] = 1;
        navigate(state.tab);
      };
  } catch (err) {
    if (ticket === state.serial && $("main"))
      $("main").innerHTML =
        head(navNames[state.tab]) +
        `<p class="error">${esc(err.message)}</p>` +
        act("重试", () => navigate(state.tab));
  }
}
function passwordModal() {
  modal(
    "修改密码",
    input("current_password", "当前密码", "", "password", "required") +
      input(
        "new_password",
        "新密码（至少 8 位）",
        "",
        "password",
        'required minlength="8"',
      ),
    async (d) => {
      await post("/auth/password", d, "PUT");
      state.me = null;
      loginScreen();
      say("密码已修改，请重新登录。");
    },
  );
}
const freshChat = () => {
  state.sid = null;
  state.history = [];
  state.retry = null;
  state.title = "";
  return navigate("ask");
};
function filterBar(fields) {
  return `<form class="toolbar" id="filters">${fields}<button type="submit">查询</button>${act(
    "清空",
    () => {
      state.filter[state.tab] = {};
      state.pages[state.tab] = 1;
      return navigate(state.tab);
    },
    "link",
  )}</form>`;
}
const currentFilters = () => state.filter[state.tab] || {};
async function paged(path) {
  const fields = currentFilters();
  const params = new URLSearchParams(
    Object.entries(fields).filter((x) => x[1]),
  );
  params.set("page", state.pages[state.tab] || 1);
  params.set("page_size", 20);
  return get(path + "?" + params);
}
function pagedList(data, render) {
  return (
    list(data.items.map(render)) +
    pager(data, (n) => {
      state.pages[state.tab] = n;
      return navigate(state.tab);
    })
  );
}
async function historyView() {
  const data = await paged("/sessions"),
    f = currentFilters();
  return (
    head("历史会话", act("新建会话", freshChat, "primary")) +
    filterBar(
      `<input type="search" aria-label="会话标题" name="q" value="${esc(f.q)}" placeholder="搜索会话标题"><input aria-label="开始日期" type="date" name="from" value="${esc(f.from)}"><input aria-label="结束日期" type="date" name="to" value="${esc(f.to)}">`,
    ) +
    pagedList(data, (r) =>
      record(
        r.title,
        dt(r.updated_at),
        act("打开", () => {
          state.sid = r.session_id;
          state.title = r.title;
          return navigate("ask");
        }) +
          act("改名", () =>
            modal(
              "重命名会话",
              input(
                "title",
                "标题",
                r.title,
                "text",
                'required maxlength="100"',
              ),
              async (d) => {
                await post("/sessions/" + r.session_id, d, "PATCH");
                await navigate("history");
              },
            ),
          ) +
          act("删除", () =>
            askConfirm(
              "删除会话",
              "删除后，这条记录将从你的历史列表中移除。",
              "确认删除",
              async () => {
                await api("/sessions/" + r.session_id + "?confirm=true", {
                  method: "DELETE",
                });
                if (state.sid === r.session_id) state.sid = null;
                await navigate("history");
              },
            ),
          ),
      ),
    )
  );
}
async function libraryView() {
  const [data, filt] = await Promise.all([
    paged("/knowledge"),
    get("/knowledge/filters"),
  ]);
  const f = currentFilters();
  return (
    head("知识目录", "", `当前角色可访问 ${data.total} 份资料`) +
    filterBar(
      `<input type="search" name="q" aria-label="资料标题" placeholder="搜索资料标题" value="${esc(f.q)}"><select name="category_id" aria-label="分类"><option value="">全部分类</option>${options(filt.categories, "category_id", "name", f.category_id)}</select><select name="tag_id" aria-label="标签"><option value="">全部标签</option>${options(filt.tags, "tag_id", "name", f.tag_id)}</select><input name="source" placeholder="来源" aria-label="来源" value="${esc(f.source)}"><input type="date" name="valid_on" aria-label="适用日期" value="${esc(f.valid_on)}">`,
    ) +
    pagedList(data, (r) =>
      record(
        r.title,
        [
          r.category,
          r.source_name,
          "V" + r.version_no,
          r.file_type,
          ...r.tags.map((t) => t.name),
        ].join(" · "),
        act("查看资料", () =>
          modal(
            r.title,
            detail([
              ["来源", r.source_name],
              ["版本", "V" + r.version_no],
              [
                "适用日期",
                (r.valid_from || "未标注") + " 至 " + (r.valid_to || "未标注"),
              ],
            ]) +
              `<a target="_blank" rel="noopener" href="/api/v1/versions/${esc(r.version_id)}/file">打开原文</a>` +
              (r.source_url
                ? `　<a target="_blank" rel="noopener" href="${esc(r.source_url)}">来源网站</a>`
                : ""),
          ),
        ),
        "已发布",
      ),
    )
  );
}
async function faqView() {
  const data = await paged("/faqs");
  return (
    head("常见问题") +
    pagedList(data, (r) =>
      record(
        r.title,
        r.category + " · " + r.question,
        act("提问", () => startQuestion(r.question, r.faq_id)),
      ),
    )
  );
}
async function feedbackView() {
  const data = await paged("/feedback");
  return (
    head("我的反馈") +
    pagedList(data, (r) =>
      record(
        r.comment || lab(r.rating),
        dt(r.updated_at) + " · " + (r.actions.at(-1)?.note || "等待处理"),
        act("详情", () =>
          modal(
            "反馈详情",
            detail([
              ["评价", lab(r.rating)],
              ["问题说明", r.comment],
              ["原答复", r.answer],
              ...r.actions.map((a) => [
                lab(a.to_status),
                a.note + (a.version_state ? " · " + lab(a.version_state) : ""),
              ]),
            ]),
          ),
        ),
        lab(r.status),
      ),
    )
  );
}
async function cite(id) {
  const c = await get("/citations/" + id),
    s = c.source;
  const pdf = s.title && c.locator.match(/第\s*(\d+)\s*页/);
  modal(
    "引用原文",
    `<h3>${esc(s.title)}</h3><blockquote>${esc(c.snippet)}</blockquote>` +
      detail([
        [
          "版本与位置",
          "V" + s.version_no + " · " + c.locator + " · " + lab(c.version_state),
        ],
        ["资料来源", s.source_name],
        [
          "适用日期",
          (s.valid_from || "未标注") + " 至 " + (s.valid_to || "未标注"),
        ],
      ]) +
      `<a target="_blank" rel="noopener" href="/api/v1/versions/${esc(c.version_id)}/file?citation_id=${esc(id)}${pdf ? "#page=" + pdf[1] : ""}">打开此版本原文</a>`,
  );
}
function feedbackModal(mid) {
  modal(
    "评价与纠错",
    select("rating", "评价", [
      ["USEFUL", "有用"],
      ["NOT_USEFUL", "需要纠正"],
    ]) +
      input("error_type", "错误类别（可选）") +
      area("comment", "说明（可选）"),
    async (d) => {
      await post("/messages/" + mid + "/feedback", d, "PUT");
      say("反馈已保存。");
    },
    "提交反馈",
  );
}
function message(r) {
  if (r.speaker === "USER")
    return `<article class="message user"><div class="pre">${esc(r.content)}</div></article>`;
  return `<article class="message"><h3>${esc(lab(r.status))}</h3><div class="answer">${esc(r.content)}</div><div class="citations">${(r.citations || []).map((c) => act("〔" + c.order_no + "〕" + c.source.title + " · V" + c.source.version_no + " · " + c.locator, () => cite(c.citation_id))).join("")}</div><div class="actions compact"><small>${dt(r.created_at)}</small>${r.message_id ? act("评价与纠错", () => feedbackModal(r.message_id), "link") : ""}</div></article>`;
}
async function chatView() {
  let faqs = [],
    filters;
  if (!state.sid) {
    [faqs, filters] = await Promise.all([
      get("/faqs?page_size=3").then((x) => x.items),
      get("/knowledge/filters"),
    ]);
    state.history = [];
  } else {
    let all = [],
      n = 1,
      pack;
    do {
      pack = await get(
        "/sessions/" + state.sid + "/messages?page_size=100&page=" + n++,
      );
      all.push(...pack.items);
    } while (all.length < pack.total);
    state.history = all;
    filters = await get("/knowledge/filters");
  }
  return (
    head(
      state.sid ? state.title || "学习会话" : "知识问答",
      state.sid ? act("新建会话", freshChat) : "",
    ) +
    (!state.history.length
      ? `<div class="chat-home"><h2>今天想了解哪条规定？</h2><p>输入问题后，可从答复中的引用查看对应条文。</p><div class="featured"><div><small>收录原文</small><b>中国共产党章程</b><small>共产党员网 · 2022 年 10 月 22 日通过</small></div><img class="thumb" src="/static/constitution-page.webp" alt="中国共产党章程原文首页"></div><div class="topic-list">${faqs.map((f) => act(f.title, () => startQuestion(f.question, f.faq_id))).join("")}</div></div>`
      : `<div class="messages">${state.history.map(message).join("")}</div>`) +
    `<form id="composer" class="composer"><textarea id="question" aria-label="输入问题" maxlength="2000" placeholder="输入问题，例如：预备党员的预备期为多长时间？" required>${esc(state.retry?.question || "")}</textarea><div class="controls"><div class="actions"><select aria-label="限定资料分类" id="chat-cat"><option value="">全部分类</option>${options(filters.categories, "category_id", "name", "")}</select><label><input id="reset-topic" type="checkbox">切换话题</label></div><button type="submit" class="primary" id="send">发送问题</button></div><div id="chat-progress" class="help" role="status"></div></form>`
  );
}
function bindComposer() {
  const f = $("composer");
  if (!f) return;
  f.onsubmit = (e) => {
    e.preventDefault();
    submitQuestion($("question").value.trim(), null).catch(showError);
  };
  $("question").onkeydown = (e) => {
    if (e.ctrlKey && e.key === "Enter") {
      e.preventDefault();
      f.requestSubmit();
    }
  };
}
async function startQuestion(q, fid) {
  if (state.tab !== "ask") {
    state.sid = null;
    await navigate("ask");
  }
  if ($("question")) $("question").value = q;
  await submitQuestion(q, fid);
}
async function submitQuestion(q, fid) {
  if (!q || state.pending) return;
  state.pending = true;
  const send = $("send"),
    progress = $("chat-progress");
  if (send) send.disabled = true;
  if (progress) progress.textContent = "正在检索已发布资料并核对引用…";
  try {
    if (!state.sid) {
      const s = await post("/sessions", {});
      state.sid = s.session_id;
      state.title = q.slice(0, 36);
    }
    const payload = {
      ...(fid ? { faq_id: fid } : { question: q }),
      category_id: $("chat-cat")?.value || null,
      reset_context: Boolean($("reset-topic")?.checked),
    };
    const sig = JSON.stringify([state.sid, payload]);
    const key =
      state.retry?.sig === sig ? state.retry.key : crypto.randomUUID();
    state.retry = { question: q, sig, key };
    const opts = { method: "POST", data: payload, key, terminal: true };
    let resp = await api("/sessions/" + state.sid + "/questions", opts);
    while (resp.code === "REQUEST_RUNNING") {
      await new Promise((r) => setTimeout(r, 800));
      resp = await api("/sessions/" + state.sid + "/questions", opts);
    }
    state.retry = null;
    await navigate("ask");
    if (resp.data?.context_truncated) say("本轮上下文已按预算截断。");
    if (resp.data?.status === "SERVICE_ERROR" && $("question"))
      $("question").value = q;
  } catch (err) {
    if (progress) progress.textContent = err.message;
    throw err;
  } finally {
    state.pending = false;
    if (send) send.disabled = false;
  }
}
window.addEventListener("hashchange", () => {
  if (state.me) navigate(location.hash.slice(1));
});
(async () => {
  try {
    state.me = await get("/me");
    await navigate(location.hash.slice(1) || "ask");
  } catch {
    loginScreen();
  }
})();
