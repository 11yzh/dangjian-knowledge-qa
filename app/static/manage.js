"use strict";
const roles = [
  ["USER", "普通用户"],
  ["KNOWLEDGE_ADMIN", "知识管理员"],
  ["SYSTEM_ADMIN", "系统管理员"],
];
const refresh = () => navigate(state.tab);
function checks(chosen = []) {
  return (
    "<fieldset><legend>授权角色</legend>" +
    roles
      .map(
        ([k, v]) =>
          `<label><input type="checkbox" name="roles" value="${k}" ${chosen.includes(k) ? "checked" : ""}>${v}</label>`,
      )
      .join("") +
    "</fieldset>"
  );
}
function checked(form, name) {
  return Array.from(
    form.querySelectorAll('input[name="' + name + '"]:checked'),
  ).map((x) => x.value);
}
function types(rows, key = "category_id") {
  return rows.map((x) => [x[key], x.name]);
}
async function allItems(path) {
  let rows = [],
    i = 1,
    page;
  do {
    page = await get(
      path + (path.includes("?") ? "&" : "?") + "page_size=100&page=" + i++,
    );
    rows.push(...page.items);
  } while (rows.length < page.total);
  return rows;
}
async function uploadDialog(version) {
  const cats = await allItems("/manage/categories");
  const fields =
    input("title", "资料标题", version?.title || "", "text", "required") +
    input(
      "source_name",
      "来源名称",
      version?.source_name || "",
      "text",
      "required",
    ) +
    input("source_url", "来源网址", version?.source_url || "", "url") +
    select("category_id", "分类", types(cats), version?.category_id) +
    input("valid_from", "适用起始日", version?.valid_from || "", "date") +
    input("valid_to", "适用结束日", version?.valid_to || "", "date") +
    '<label>原文文件<input name="file" type="file" accept=".pdf,.docx,.txt" required></label><p class="help">支持文本型 PDF、DOCX 和 UTF-8 TXT，单个文件最大 20 MB。</p>';
  modal(
    version ? "上传新版本" : "导入知识资料",
    fields,
    async (d, form) => {
      const body = new FormData(form);
      if (version) body.set("document_id", version.document_id);
      const res = (
        await api("/manage/versions", { method: "POST", form: body })
      ).data;
      say(res.duplicate ? "同一文件已登记。" : "资料已受理，可查看处理进度。");
      await refresh();
    },
    "提交导入",
  );
}
async function showChunks(v) {
  let data = await allItems("/manage/versions/" + v.version_id + "/chunks");
  modal(
    "原文片段",
    detail([
      ["资料", v.title + " · V" + v.version_no],
      ["原文 SHA-256", v.sha256],
    ]) +
      `<a href="/api/v1/versions/${esc(v.version_id)}/preview" target="_blank" rel="noopener">打开原文件</a>` +
      data
        .map(
          (c) =>
            `<p><small>${esc(c.locator)}</small></p><blockquote>${esc(c.content)}</blockquote>`,
        )
        .join(""),
  );
}
async function review(v) {
  modal(
    "审核知识版本",
    detail([
      ["资料", v.title + " · V" + v.version_no],
      ["来源", v.source_name],
      [
        "适用日期",
        (v.valid_from || "未标注") + " 至 " + (v.valid_to || "未标注"),
      ],
    ]) +
      select("decision", "审核结果", [
        ["PUBLISH", "审核通过并发布"],
        ["REJECT", "退回修订"],
      ]) +
      checks(["USER"]) +
      area("review_note", "审核意见", "", "required"),
    async (d, f) => {
      await post("/manage/versions/" + v.version_id + "/review", {
        decision: d.decision,
        review_note: d.review_note,
        role_ids: checked(f, "roles"),
        revision: v.revision,
        base_current_version_id: v.current_version_id,
      });
      await refresh();
    },
    "提交审核",
  );
}
function changeVersion(v, op, label) {
  modal(
    label,
    detail([
      ["资料", v.title + " · V" + v.version_no],
      ["当前状态", lab(v.state)],
    ]) + area("reason", "操作原因", "", "required"),
    async (d) => {
      await post("/manage/versions/" + v.version_id + "/" + op, {
        ...d,
        revision: v.revision,
      });
      await refresh();
    },
    label,
  );
}
async function editTags(v) {
  const [cats, tags, docs] = await Promise.all([
    allItems("/manage/categories"),
    allItems("/manage/tags"),
    allItems("/manage/documents"),
  ]);
  let current = docs.find((x) => x.document_id === v.document_id),
    active = current?.tags.map((x) => x.tag_id) || [];
  modal(
    "资料分类与标签",
    select("category_id", "分类", types(cats), v.category_id) +
      "<fieldset><legend>标签</legend>" +
      tags
        .map(
          (x) =>
            `<label><input type="checkbox" name="tags" value="${esc(x.tag_id)}" ${active.includes(x.tag_id) ? "checked" : ""}>${esc(x.name)}</label>`,
        )
        .join("") +
      "</fieldset>",
    async (d, f) => {
      await post(
        "/manage/documents/" + v.document_id,
        { category_id: d.category_id, tag_ids: checked(f, "tags") },
        "PATCH",
      );
      await refresh();
    },
  );
}
async function versionView() {
  let data = await paged("/manage/versions"),
    f = currentFilters();
  return (
    head(
      "知识维护",
      act("导入资料", () => uploadDialog(null), "primary"),
    ) +
    filterBar(
      `<input type="search" name="q" placeholder="搜索资料标题" aria-label="搜索资料标题" value="${esc(f.q)}">` +
        select(
          "state",
          "版本状态",
          [
            ["", "全部状态"],
            ...[
              "UPLOADED",
              "PROCESSING",
              "FAILED",
              "REVIEW_PENDING",
              "PUBLISHED",
              "WITHDRAWN",
              "REJECTED",
              "ARCHIVED",
            ].map((x) => [x, lab(x)]),
          ],
          f.state,
        ),
    ) +
    pagedList(data, (v) => {
      let buttons =
        act("预览", () => showChunks(v)) +
        act("任务进度", async () => {
          const j = await get("/manage/jobs/" + v.job_id);
          modal(
            "导入进度",
            detail([
              ["阶段", j.stage],
              ["尝试次数", j.attempt],
              ["最近更新", dt(j.updated_at)],
              ["失败原因", j.reason_code],
            ]),
          );
        });
      if (v.state === "REVIEW_PENDING")
        buttons += act("审核", () => review(v), "primary");
      if (v.state === "FAILED")
        buttons += act("重试", async () => {
          await post("/manage/jobs/" + v.job_id + "/retry");
          await refresh();
        });
      if (v.state === "PUBLISHED")
        buttons += act("撤回", () => changeVersion(v, "withdraw", "撤回版本"));
      if (v.state === "WITHDRAWN")
        buttons += act("重新审核", () =>
          changeVersion(v, "reopen", "重新送审"),
        );
      if (["WITHDRAWN", "REJECTED"].includes(v.state))
        buttons += act("归档", () => changeVersion(v, "archive", "归档版本"));
      buttons +=
        act("新版本", () => uploadDialog(v)) +
        act("分类标签", () => editTags(v));
      return record(
        v.title + " · V" + v.version_no,
        [v.source_name, v.file_type, v.stage, v.job_reason]
          .filter(Boolean)
          .join(" · "),
        buttons,
        lab(v.state),
      );
    })
  );
}
let catalogTab = "categories";
async function dictModal(kind, row, parents) {
  const cat = kind === "categories",
    id = cat ? row?.category_id : row?.tag_id;
  const body =
    input("name", "名称", row?.name || "", "text", "required") +
    (cat
      ? select(
          "parent_id",
          "上级分类",
          [
            ["", "顶级分类"],
            ...types(parents.filter((x) => x.category_id !== id)),
          ],
          row?.parent_id,
        )
      : "");
  modal((row ? "编辑" : "新建") + (cat ? "分类" : "标签"), body, async (d) => {
    if (cat) d.parent_id = d.parent_id || null;
    await post(
      "/manage/" + kind + (id ? "/" + id : ""),
      d,
      id ? "PATCH" : "POST",
    );
    await refresh();
  });
}
async function faqModal(r, cats) {
  modal(
    r ? "编辑常见问题" : "新增常见问题",
    input("title", "标题", r?.title, "text", "required") +
      area("question", "问题正文", r?.question, "required") +
      select("category_id", "分类", types(cats), r?.category_id) +
      input(
        "sort_order",
        "排序",
        r?.sort_order ?? 0,
        "number",
        'min="0" required',
      ) +
      select(
        "published",
        "发布状态",
        [
          ["false", "草稿"],
          ["true", "已发布"],
        ],
        r?.published ?? false,
      ),
    async (d) => {
      d.sort_order = Number(d.sort_order);
      d.published = d.published === "true";
      await post(
        "/manage/faqs" + (r ? "/" + r.faq_id : ""),
        d,
        r ? "PATCH" : "POST",
      );
      await refresh();
    },
  );
}
async function catalogView() {
  const cats = await allItems("/manage/categories");
  const rows =
    catalogTab === "categories"
      ? cats
      : await allItems("/manage/" + catalogTab);
  const title = { categories: "分类", tags: "标签", faqs: "常见问题" };
  const nav = Object.entries(title)
    .map(([k, v]) =>
      act(
        v,
        () => {
          catalogTab = k;
          return refresh();
        },
        catalogTab === k ? "primary" : "",
      ),
    )
    .join("");
  const edit = (r) =>
    catalogTab === "faqs" ? faqModal(r, cats) : dictModal(catalogTab, r, cats);
  return (
    head(
      "分类与常见问题",
      act("新建" + title[catalogTab], () => edit(null), "primary"),
    ) +
    `<div class="subnav">${nav}</div>` +
    list(
      rows.map((r) => {
        let extra = act("编辑", () => edit(r));
        if (catalogTab !== "faqs")
          extra += act("删除", () =>
            askConfirm(
              "删除" + title[catalogTab],
              "请核对要删除的条目：" + r.name,
              "确认删除",
              async () => {
                await api(
                  "/manage/" + catalogTab + "/" + (r.category_id || r.tag_id),
                  { method: "DELETE" },
                );
                await refresh();
              },
            ),
          );
        return record(
          r.name || r.title,
          r.question ||
            (r.parent_id
              ? "上级分类 · " +
                (cats.find((c) => c.category_id === r.parent_id)?.name || "")
              : ""),
          extra,
          catalogTab === "faqs" ? (r.published ? "已发布" : "草稿") : "",
        );
      }),
    )
  );
}
async function inboxView() {
  const items = await paged("/manage/feedback");
  return (
    head("纠错处理") +
    pagedList(items, (r) =>
      record(
        r.comment || lab(r.rating),
        dt(r.updated_at) + " · " + (r.error_type || "问答评价"),
        act("查看与处理", async () => {
          const info = await get("/manage/feedback/" + r.feedback_id);
          const prefix = detail([
            ["原答复", info.answer],
            ["反馈", info.comment],
            ["当前状态", lab(info.status)],
          ]);
          if (["RESOLVED", "REJECTED"].includes(info.status)) {
            modal(
              "反馈处理记录",
              prefix +
                detail(info.actions.map((a) => [lab(a.to_status), a.note])),
            );
            return;
          }
          const versions =
            info.status === "PROCESSING"
              ? await allItems("/manage/versions")
              : [];
          modal(
            "处理纠错反馈",
            prefix +
              select(
                "to_status",
                "处理动作",
                info.status === "PENDING"
                  ? [["PROCESSING", "领取处理"]]
                  : [
                      ["RESOLVED", "已解决"],
                      ["REJECTED", "不采纳"],
                    ],
              ) +
              area("note", "记录说明", "", "required") +
              (versions.length
                ? select("new_version_id", "关联修订版本", [
                    ["", "无"],
                    ...versions.map((v) => [
                      v.version_id,
                      v.title + " · V" + v.version_no,
                    ]),
                  ])
                : ""),
            async (d) => {
              await post("/manage/feedback/" + r.feedback_id + "/actions", {
                ...d,
                revision: info.revision,
                new_version_id: d.new_version_id || null,
              });
              await refresh();
            },
          );
        }),
        lab(r.status),
      ),
    )
  );
}
async function usersView() {
  const data = await paged("/manage/users");
  return (
    head(
      "账号与权限",
      act(
        "新增账号",
        () =>
          modal(
            "新增账号",
            input("username", "用户名", "", "text", "required") +
              input("display_name", "显示名称", "", "text", "required") +
              input(
                "initial_password",
                "临时密码",
                "",
                "password",
                'required minlength="8"',
              ) +
              checks(["USER"]),
            async (d, f) => {
              d.role_ids = checked(f, "roles");
              delete d.roles;
              await post("/manage/users", d);
              await refresh();
            },
          ),
        "primary",
      ) +
        act("登录策略", async () => {
          const p = await get("/manage/auth-policy");
          modal(
            "登录策略",
            input(
              "max_failures",
              "连续失败上限",
              p.max_failures,
              "number",
              "min=1 required",
            ) +
              input(
                "lock_minutes",
                "限制时长（分钟）",
                p.lock_minutes,
                "number",
                "min=1 required",
              ) +
              input(
                "idle_minutes",
                "空闲过期（分钟）",
                p.idle_minutes,
                "number",
                "min=1 required",
              ),
            async (d) => {
              await post(
                "/manage/auth-policy",
                Object.fromEntries(
                  Object.entries(d).map(([k, v]) => [k, Number(v)]),
                ),
                "PUT",
              );
              await refresh();
            },
          );
        }),
    ) +
    pagedList(data, (r) =>
      record(
        r.display_name,
        r.username + " · " + r.roles.map((x) => lab(x.role_code)).join("、"),
        act("管理授权", () =>
          modal(
            "账号授权",
            select(
              "status",
              "账号状态",
              [
                ["ACTIVE", "启用"],
                ["DISABLED", "停用"],
              ],
              r.status,
            ) + checks(r.roles.map((x) => x.role_code)),
            async (d, f) => {
              await post(
                "/manage/users/" + r.user_id,
                { status: d.status, role_ids: checked(f, "roles") },
                "PATCH",
              );
              await refresh();
            },
          ),
        ) +
          act("重置密码", () =>
            modal(
              "重置临时密码",
              input(
                "temporary_password",
                "新临时密码",
                "",
                "password",
                'minlength="8" required',
              ),
              async (d) => {
                await post("/manage/users/" + r.user_id + "/reset", d);
                say("临时密码已重置。");
              },
            ),
          ),
        lab(r.status),
      ),
    )
  );
}
function ruleForm(r) {
  return (
    input("pattern", "匹配词条", r?.pattern, "text", "required") +
    select(
      "match_type",
      "匹配方式",
      [
        ["EXACT", "精确词项"],
        ["CONTAINS", "包含文本"],
      ],
      r?.match_type,
    ) +
    select(
      "action",
      "处置动作",
      [
        ["BLOCK", "阻断"],
        ["MASK", "脱敏"],
        ["LOG", "记录"],
      ],
      r?.action,
    ) +
    select(
      "enabled",
      "状态",
      [
        ["true", "启用"],
        ["false", "停用"],
      ],
      r?.enabled ?? true,
    ) +
    select(
      "allowlist",
      "复核白名单",
      [
        ["false", "否"],
        ["true", "是"],
      ],
      r?.allowlist ?? false,
    ) +
    input("description", "说明", r?.description)
  );
}
function ruleData(d) {
  return {
    ...d,
    enabled: d.enabled === "true",
    allowlist: d.allowlist === "true",
  };
}
async function safetyView() {
  const data = await paged("/manage/rules");
  const edit = (r) =>
    modal(
      r ? "编辑规则" : "新建规则",
      ruleForm(r) +
        area("sample", "试算文本（可选）") +
        act("预览匹配", async () => {
          const d = formData($("entry-form"));
          const x = await post("/manage/rules/preview", {
            text: d.sample,
            rule: ruleData(d),
          });
          $("preview-out").textContent =
            (x.blocked ? "阻断 · " : "通过 · ") +
            x.text +
            " · 命中 " +
            x.hits.length +
            " 条规则";
        }) +
        '<output id="preview-out"></output>',
      async (d) => {
        await post(
          "/manage/rules" + (r ? "/" + r.rule_id : ""),
          { ...ruleData(d), rule_version: r?.rule_version },
          r ? "PUT" : "POST",
        );
        await refresh();
      },
    );
  return (
    head(
      "安全规则",
      act("新建规则", () => edit(null), "primary"),
    ) +
    pagedList(data, (r) =>
      record(
        r.pattern,
        [r.match_type, r.action, "V" + r.rule_version, r.description]
          .filter(Boolean)
          .join(" · "),
        act("编辑", () => edit(r)),
        r.enabled ? "启用" : "停用",
      ),
    )
  );
}
async function configsView() {
  const items = await allItems("/manage/model-configs");
  const active = items.find((r) => r.status === "ACTIVE"),
    base = active || {};
  const add = () =>
    modal(
      "新建模型配置",
      select("provider", "提供方", [
        ["DEEPSEEK", "DeepSeek"],
        ["QWEN", "千问"],
      ]) +
        input(
          "model_name",
          "模型标识",
          base.model_name || "deepseek-flash",
          "text",
          "required",
        ) +
        input(
          "api_key",
          "API Key（留空沿用同一提供方凭据）",
          "",
          "password",
          'autocomplete="new-password"',
        ) +
        [
          ["timeout_seconds", "总时限（秒）", 60],
          ["top_k", "检索片段数", 5],
          ["threshold", "最低相关分", 0.6],
          ["context_rounds", "上下文轮数", 10],
          ["context_budget", "上下文预算（token）", 8000],
        ]
          .map(([k, v, n]) =>
            input(k, v, base[k] ?? n, "number", 'step="any" required'),
          )
          .join(""),
      async (d) => {
        for (const k of [
          "timeout_seconds",
          "top_k",
          "threshold",
          "context_rounds",
          "context_budget",
        ])
          d[k] = Number(d[k]);
        await post("/manage/model-configs", d);
        await refresh();
      },
    );
  return (
    head("模型配置", act("新建配置", add, "primary")) +
    list(
      items.map((r) =>
        record(
          r.model_name,
          r.provider +
            " · " +
            r.key_mask +
            " · " +
            r.timeout_seconds +
            " 秒 · " +
            (r.verified ? "已验证" : "待验证"),
          act("连接测试", async () => {
            await post("/manage/model-configs/" + r.config_id + "/test");
            say("真实接口与结构化输出验证成功。");
            await refresh();
          }) +
            (r.status !== "ACTIVE" && r.verified
              ? act("启用", async () => {
                  await post(
                    "/manage/model-configs/" + r.config_id + "/activate",
                    { expected_active_id: active?.config_id || null },
                  );
                  await refresh();
                })
              : ""),
          r.status === "ACTIVE" ? "当前生效" : "未启用",
        ),
      ),
    )
  );
}
function datesFilter(f) {
  return `<input type="date" name="from" aria-label="起始日期" value="${esc(f.from)}"><input type="date" name="to" aria-label="结束日期" value="${esc(f.to)}">`;
}
async function statsView() {
  const f = currentFilters(),
    query = new URLSearchParams(Object.entries(f).filter((x) => x[1]));
  const s = await get("/manage/statistics?" + query);
  const metrics = [
    ["提问总量", s.total],
    ["成功回答", s.counts.SUCCESS],
    ["未找到依据", s.counts.NO_EVIDENCE],
    ["拒答", s.counts.REFUSED],
    ["服务异常", s.counts.SERVICE_ERROR],
    ["反馈", s.feedback_count],
    ["有效发布", s.published_count],
  ];
  return (
    head("运营统计") +
    filterBar(
      datesFilter(f) +
        `<input name="topic" placeholder="主题" aria-label="主题" value="${esc(f.topic)}">`,
    ) +
    `<div class="kpis">${metrics.map(([l, n]) => `<div><small>${l}</small><b>${n}</b></div>`).join("")}</div><h2 class="section-title">热点主题</h2>` +
    (s.topics
      .map(
        (r) =>
          `<div class="plot-row"><span>${esc(r.topic)}</span><meter min="0" max="${s.total || 1}" value="${r.n}"></meter><b>${r.n}</b></div>`,
      )
      .join("") || "<p class='muted'>暂无问答事件。</p>") +
    `<h2 class="section-title">每日问答</h2><div class="table-wrap"><table><thead><tr><th>日期</th><th>成功</th><th>无依据</th><th>拒答</th><th>服务异常</th></tr></thead><tbody>${s.days.map((d) => `<tr><td>${d.day}</td><td>${d.SUCCESS}</td><td>${d.NO_EVIDENCE}</td><td>${d.REFUSED}</td><td>${d.SERVICE_ERROR}</td></tr>`).join("")}</tbody></table></div>`
  );
}
async function auditView() {
  const data = await paged("/manage/audit"),
    f = currentFilters();
  return (
    head("审计日志") +
    filterBar(
      datesFilter(f) +
        ["action", "actor_id", "request_id"]
          .map(
            (k, i) =>
              `<input name="${k}" placeholder="${["操作类型", "操作者 ID", "请求标识"][i]}" aria-label="${["操作类型", "操作者 ID", "请求标识"][i]}" value="${esc(f[k])}">`,
          )
          .join("") +
        select(
          "result",
          "结果",
          [
            ["", "全部"],
            ["SUCCESS", "成功"],
            ["FAILURE", "失败"],
          ],
          f.result,
        ),
    ) +
    pagedList(data, (r) =>
      record(
        r.action,
        (r.display_name || "系统任务") + " · " + dt(r.occurred_at),
        act("详情", () =>
          modal(
            "审计详情",
            detail([
              ["对象", r.target],
              ["摘要", r.summary],
              ["操作者 ID", r.actor_id],
              ["请求标识", r.request_id],
              ["时间", dt(r.occurred_at)],
            ]),
          ),
        ),
        r.result === "SUCCESS" ? "成功" : "失败",
      ),
    )
  );
}
const manageView = (key) =>
  ({
    versions: versionView,
    catalog: catalogView,
    inbox: inboxView,
    users: usersView,
    safety: safetyView,
    configs: configsView,
    stats: statsView,
    audit: auditView,
  })[key]();
