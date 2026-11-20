from datetime import timedelta
from decimal import Decimal, InvalidOperation
from fastapi import APIRouter, Request
from .auth import acceptable, create_user, principal, role_list
from .core import *
from .knowledge import date_value
from .library_api import K, S, write
from . import model
from .retrieval import active_rules, screen
from .vault import store_secret

routes = APIRouter()


@routes.get("/manage/users")
def users(req: Request):
    with engine().begin() as db:
        principal(db, req, S)
        out = page(
            db,
            "SELECT user_id,username,display_name,status,must_change_password,created_at FROM user_account",
            {},
            req,
            "created_at,username",
        )
        for row in out["items"]:
            row["roles"] = role_list(db, row["user_id"])
        return result(req, out)


@routes.post("/manage/users")
def add_user(req: Request, data: dict):
    return write(
        req,
        data,
        S,
        "创建账号",
        lambda db, u: create_user(
            db,
            field(data, "username", 64),
            field(data, "display_name", 64),
            data.get("initial_password"),
            data.get("role_ids"),
        ),
        201,
    )


@routes.patch("/manage/users/{user_id}")
def change_user(req: Request, user_id: str, data: dict):
    def action(db, u):
        run(db, "SELECT pg_advisory_xact_lock(673103)")
        old = found(
            one(
                db,
                "SELECT * FROM user_account WHERE user_id=:i FOR UPDATE",
                i=uid(user_id),
            )
        )
        status = data.get("status", old["status"])
        require(status in ("ACTIVE", "DISABLED"), "账号状态无效。")
        roles = (
            ids_for_roles(db, data["role_ids"])
            if "role_ids" in data
            else [r["role_id"] for r in role_list(db, user_id)]
        )
        admin_role = one(db, "SELECT role_id FROM role WHERE role_code='SYSTEM_ADMIN'")[
            "role_id"
        ]
        if status != "ACTIVE" or admin_role not in roles:
            other = one(
                db,
                "SELECT 1 FROM user_account a JOIN user_role ur USING(user_id) WHERE a.status='ACTIVE' AND ur.role_id=:r AND a.user_id<>:i",
                r=admin_role,
                i=user_id,
            )
            require(other, "至少保留一个有效系统管理员。", 409, "VERSION_CONFLICT")
        run(
            db,
            "UPDATE user_account SET status=:s WHERE user_id=:i",
            s=status,
            i=user_id,
        )
        run(db, "DELETE FROM user_role WHERE user_id=:i", i=user_id)
        for role in roles:
            run(db, "INSERT INTO user_role VALUES(:i,:r)", i=user_id, r=role)
        run(
            db,
            "DELETE FROM auth_session WHERE user_id=:i AND (:s='DISABLED' OR NOT(current_role_id=ANY(:roles)))",
            i=user_id,
            s=status,
            roles=roles,
        )
        return {"user_id": user_id, "status": status}

    return write(req, data | {"uid": user_id}, S, "更新账号授权", action)


@routes.post("/manage/users/{user_id}/reset")
def reset(req: Request, user_id: str, data: dict):
    def action(db, u):
        found(
            one(
                db,
                "SELECT 1 FROM user_account WHERE user_id=:i FOR UPDATE",
                i=uid(user_id),
            )
        )
        run(
            db,
            "UPDATE user_account SET password_hash=:p,must_change_password=true,failed_count=0,locked_until=NULL WHERE user_id=:i",
            p=acceptable(data.get("temporary_password")),
            i=user_id,
        )
        run(db, "DELETE FROM auth_session WHERE user_id=:i", i=user_id)
        return {"user_id": user_id}

    return write(req, data | {"uid": user_id}, S, "重置账号", action)


@routes.get("/manage/auth-policy")
def policy(req: Request):
    with engine().begin() as db:
        principal(db, req, S)
        return result(req, one(db, "SELECT * FROM auth_policy"))


@routes.put("/manage/auth-policy")
def set_policy(req: Request, data: dict):
    def action(db, u):
        vals = {
            k: integer(data.get(k), 1, 10080)
            for k in ("max_failures", "lock_minutes", "idle_minutes")
        }
        return one(
            db,
            """UPDATE auth_policy SET max_failures=:max_failures,lock_minutes=:lock_minutes,idle_minutes=:idle_minutes,
            updated_by=:u,updated_at=now() WHERE policy_id=1 RETURNING *""",
            **vals,
            u=u["user_id"],
        )

    return write(req, data, S, "登录策略", action)


@routes.get("/faqs")
@routes.get("/manage/faqs")
def faqs(req: Request):
    admin = "/manage/" in req.url.path
    with engine().begin() as db:
        principal(db, req, K if admin else None)
        q = (
            "SELECT f.*,c.name AS category FROM faq f JOIN category c USING(category_id) WHERE "
            + ("TRUE" if admin else "published")
        )
        params = {}
        if req.query_params.get("category_id"):
            q += " AND f.category_id=:c"
            params["c"] = uid(req.query_params["category_id"])
        return result(req, page(db, q, params, req, "sort_order,title,faq_id"))


@routes.post("/manage/faqs")
@routes.patch("/manage/faqs/{fid}")
def save_faq(req: Request, data: dict, fid: str | None = None):
    def action(db, u):
        current = (
            found(one(db, "SELECT * FROM faq WHERE faq_id=:i FOR UPDATE", i=uid(fid)))
            if fid
            else {}
        )
        entry = current | data
        values = dict(
            t=field(entry, "title", 100),
            q=field(entry, "question", 2000),
            c=uid(entry.get("category_id")),
            o=integer(entry.get("sort_order", 0), 0, 1000000),
            p=boolean(entry.get("published", False)),
            u=u["user_id"],
        )
        found(one(db, "SELECT 1 FROM category WHERE category_id=:c", c=values["c"]))
        statement = (
            "UPDATE faq SET title=:t,question=:q,category_id=:c,sort_order=:o,published=:p,updated_by=:u,updated_at=now() WHERE faq_id=:i"
            if fid
            else "INSERT INTO faq(title,question,category_id,sort_order,published,updated_by) VALUES(:t,:q,:c,:o,:p,:u)"
        )
        return one(db, statement + " RETURNING *", **values, i=fid)

    return write(req, data | {"id": fid}, K, "维护常见问题", action)


@routes.put("/messages/{mid}/feedback")
def feedback(req: Request, mid: str, data: dict):
    def action(db, u):
        found(
            one(
                db,
                "SELECT 1 FROM message m JOIN conversation s USING(session_id) WHERE message_id=:m AND owner_id=:u AND s.status='ACTIVE' AND speaker='ASSISTANT'",
                m=uid(mid),
                u=u["user_id"],
            )
        )
        rating = data.get("rating")
        require(rating in ("USEFUL", "NOT_USEFUL"), "请选择反馈评价。")
        return one(
            db,
            """INSERT INTO feedback(message_id,user_id,rating,error_type,comment) VALUES(:m,:u,:r,:e,:c)
            ON CONFLICT(message_id,user_id) DO UPDATE SET rating=excluded.rating,error_type=excluded.error_type,
            comment=excluded.comment,status='PENDING',revision=feedback.revision+1,updated_at=now() RETURNING feedback_id,status,revision""",
            m=mid,
            u=u["user_id"],
            r=rating,
            e=field(data, "error_type", 32, required=False),
            c=field(data, "comment", 1000, required=False),
        )

    return write(req, data | {"mid": mid}, None, "提交反馈", action)


FEEDBACK = """SELECT f.*,m.content AS answer,
 (SELECT coalesce(jsonb_agg(jsonb_build_object('to_status',a.to_status,'note',a.note,'created_at',a.created_at,
 'new_version_id',a.new_version_id,'version_state',v.state) ORDER BY a.created_at),'[]')
 FROM feedback_action a LEFT JOIN document_version v ON v.version_id=a.new_version_id WHERE a.feedback_id=f.feedback_id) AS actions
 FROM feedback f JOIN message m USING(message_id)"""


@routes.get("/feedback")
@routes.get("/manage/feedback")
def feedbacks(req: Request):
    managed = "/manage/" in req.url.path
    with engine().begin() as db:
        u = principal(db, req, K if managed else None)
        q = FEEDBACK + " WHERE " + ("TRUE" if managed else "f.user_id=:u")
        args = {} if managed else {"u": u["user_id"]}
        if req.query_params.get("status"):
            q += " AND f.status=:s"
            args["s"] = req.query_params["status"]
        out = page(db, q, args, req, "f.updated_at DESC,f.feedback_id")
        if managed:
            for item in out["items"]:
                item.pop("answer", None)
        return result(req, out)


@routes.get("/manage/feedback/{fid}")
def one_feedback(req: Request, fid: str):
    with engine().begin() as db:
        u = principal(db, req, K)
        info = found(one(db, FEEDBACK + " WHERE f.feedback_id=:id", id=uid(fid)))
        audit(db, u["user_id"], "查看反馈上下文", fid, rid=req.state.request_id)
        return result(req, info)


@routes.post("/manage/feedback/{fid}/actions")
def handle_feedback(req: Request, fid: str, data: dict):
    def action(db, u):
        current = found(
            one(
                db, "SELECT * FROM feedback WHERE feedback_id=:i FOR UPDATE", i=uid(fid)
            )
        )
        require(
            current["revision"] == data.get("revision"),
            "反馈已更新，请刷新。",
            409,
            "VERSION_CONFLICT",
        )
        status = data.get("to_status")
        allowed = {"PENDING": {"PROCESSING"}, "PROCESSING": {"RESOLVED", "REJECTED"}}
        require(
            status in allowed.get(current["status"], set()),
            "当前处理状态不允许此操作。",
        )
        note = field(data, "note", 1000)
        vid = data.get("new_version_id") or None
        if vid:
            found(
                one(
                    db, "SELECT 1 FROM document_version WHERE version_id=:v", v=uid(vid)
                )
            )
        run(
            db,
            "INSERT INTO feedback_action(feedback_id,handler_id,to_status,note,new_version_id) VALUES(:i,:u,:s,:n,:v)",
            i=fid,
            u=u["user_id"],
            s=status,
            n=note,
            v=vid,
        )
        return one(
            db,
            "UPDATE feedback SET status=:s,revision=revision+1,updated_at=now() WHERE feedback_id=:i RETURNING feedback_id,status,revision",
            s=status,
            i=fid,
        )

    return write(req, data | {"fid": fid}, K, "处理反馈", action)


@routes.get("/manage/rules")
def rules(req: Request):
    with engine().begin() as db:
        principal(db, req, S)
        return result(
            req,
            page(
                db,
                "SELECT * FROM (SELECT DISTINCT ON(rule_id) * FROM safety_rule ORDER BY rule_id,rule_version DESC) t",
                {},
                req,
                "updated_at DESC,rule_id",
            ),
        )


def rule_values(data):
    pattern = field(data, "pattern", 200)
    mt = data.get("match_type", "CONTAINS")
    act = data.get("action", "BLOCK")
    require(
        mt in ("CONTAINS", "EXACT") and act in ("BLOCK", "MASK", "LOG"),
        "匹配方式或处置动作无效。",
    )
    return dict(
        pattern=pattern,
        match_type=mt,
        action=act,
        enabled=boolean(data.get("enabled", True)),
        allowlist=boolean(data.get("allowlist", False)),
        description=field(data, "description", 200, required=False),
    )


@routes.post("/manage/rules/preview")
def preview(req: Request, data: dict):
    with engine().begin() as db:
        principal(db, req, S)
        ruleset = active_rules(db)
        if data.get("rule"):
            ruleset.append(
                rule_values(data["rule"]) | {"rule_id": "preview", "rule_version": 1}
            )
        blocked, clean, hits = screen(field(data, "text", 2000), ruleset)
        return result(req, {"blocked": blocked, "text": clean, "hits": hits})


@routes.post("/manage/rules")
@routes.put("/manage/rules/{rid}")
def save_rule(req: Request, data: dict, rid: str | None = None):
    def action(db, u):
        current = {}
        ident = uid(rid) if rid else str(uuid4())
        run(db, "SELECT pg_advisory_xact_lock(hashtextextended(:k,0))", k=ident)
        if rid:
            current = found(
                one(
                    db,
                    "SELECT * FROM safety_rule WHERE rule_id=:i ORDER BY rule_version DESC LIMIT 1",
                    i=ident,
                )
            )
            require(
                current["rule_version"] == data.get("rule_version"),
                "规则版本已变化。",
                409,
                "VERSION_CONFLICT",
            )
        vals = rule_values(current | data)
        return one(
            db,
            """INSERT INTO safety_rule(rule_id,rule_version,pattern,match_type,action,enabled,allowlist,description,updated_by)
            VALUES(:i,:ver,:pattern,:match_type,:action,:enabled,:allowlist,:description,:u) RETURNING *""",
            **vals,
            i=ident,
            ver=current.get("rule_version", 0) + 1,
            u=u["user_id"],
        )

    return write(req, data | {"rid": rid}, S, "安全规则", action)


CONFIG_COLS = "config_id,provider,model_name,status,verified,timeout_seconds,top_k,threshold,context_rounds,context_budget,updated_at,'••••••••' AS key_mask"


@routes.get("/manage/model-configs")
def configs(req: Request):
    with engine().begin() as db:
        principal(db, req, S)
        return result(
            req,
            page(
                db,
                "SELECT " + CONFIG_COLS + " FROM model_config",
                {},
                req,
                "updated_at DESC,config_id",
            ),
        )


@routes.post("/manage/model-configs")
def config_create(req: Request, data: dict):
    def action(db, u):
        provider = data.get("provider", "DEEPSEEK")
        require(provider in ("DEEPSEEK", "QWEN"), "请选择受支持的提供方。")
        name = field(data, "model_name", 128)
        try:
            threshold = Decimal(str(data.get("threshold", "0.6")))
            require(
                threshold.is_finite() and 0 <= threshold <= 1,
                "相关阈值应在 0 至 1 之间。",
            )
        except (InvalidOperation, ValueError):
            raise Problem("相关阈值无效。") from None
        vals = {
            k: integer(data.get(k, default), a, b)
            for k, default, a, b in [
                ("timeout_seconds", 60, 5, 120),
                ("top_k", 5, 1, 20),
                ("context_rounds", 10, 0, 20),
                ("context_budget", 8000, 1000, 32000),
            ]
        }
        secret = field(data, "api_key", 512, required=False)
        if secret:
            ref = store_secret(secret)
        else:
            previous = one(
                db,
                "SELECT secret_ref FROM model_config WHERE provider=:p ORDER BY updated_at DESC LIMIT 1",
                p=provider,
            )
            ref = (
                previous["secret_ref"]
                if previous
                else ("deepseek" if provider == "DEEPSEEK" else None)
            )
            from .vault import read_secret

            require(ref and read_secret(ref), "该提供方尚未配置凭据。")
        return one(
            db,
            """INSERT INTO model_config(provider,model_name,secret_ref,timeout_seconds,top_k,threshold,context_rounds,context_budget,updated_by)
            VALUES(:p,:m,:secret,:timeout_seconds,:top_k,:threshold,:context_rounds,:context_budget,:u) RETURNING """
            + CONFIG_COLS,
            **vals,
            p=provider,
            m=name,
            secret=ref,
            threshold=threshold,
            u=u["user_id"],
        )

    return write(req, data, S, "创建模型配置", action, 201)


@routes.post("/manage/model-configs/{cid}/test")
async def config_test(req: Request, cid: str, data: dict):
    with engine().begin() as db:
        u = principal(db, req, S)
        cfg = found(
            one(db, "SELECT * FROM model_config WHERE config_id=:i", i=uid(cid))
        )
    try:
        await model.probe(cfg)
        passed, why = True, "连接及 JSON 校验成功。"
    except model.ModelError as exc:
        passed, why = False, str(exc)
    with engine().begin() as db:
        u = principal(db, req, S)
        run(
            db,
            "UPDATE model_config SET verified=:v WHERE config_id=:i",
            v=passed,
            i=cid,
        )
        audit(
            db,
            u["user_id"],
            "模型连通测试",
            cid,
            "SUCCESS" if passed else "FAILURE",
            why,
            rid=req.state.request_id,
        )
    return result(
        req,
        {"verified": passed},
        code="OK" if passed else "SERVICE_ERROR",
        message=why,
        status=200 if passed else 503,
    )


@routes.post("/manage/model-configs/{cid}/activate")
def activate(req: Request, cid: str, data: dict):
    def action(db, u):
        run(db, "SELECT pg_advisory_xact_lock(673104)")
        cfg = found(
            one(db, "SELECT * FROM model_config WHERE config_id=:i", i=uid(cid))
        )
        require(cfg["verified"], "请先通过真实接口连接测试。")
        active = one(db, "SELECT config_id FROM model_config WHERE status='ACTIVE'")
        require(
            "expected_active_id" in data
            and str(data["expected_active_id"])
            == str(active["config_id"] if active else None),
            "生效配置已变化。",
            409,
            "VERSION_CONFLICT",
        )
        run(db, "UPDATE model_config SET status='INACTIVE' WHERE status='ACTIVE'")
        run(
            db,
            "UPDATE model_config SET status='ACTIVE',updated_at=now() WHERE config_id=:i",
            i=cid,
        )
        return {"config_id": cid, "status": "ACTIVE"}

    return write(req, data | {"cid": cid}, S, "启用模型配置", action)


@routes.get("/manage/audit")
def audit_list(req: Request):
    with engine().begin() as db:
        principal(db, req, S)
        q = "SELECT a.*,u.display_name FROM audit_log a LEFT JOIN user_account u ON u.user_id=a.actor_id WHERE TRUE"
        args = {}
        for k in ("actor_id", "action", "result", "request_id", "target"):
            if req.query_params.get(k):
                q += " AND a." + k + "=:" + k
                args[k] = req.query_params[k]
        for k, op in [("from", ">="), ("to", "<=")]:
            if req.query_params.get(k):
                q += f" AND (a.occurred_at AT TIME ZONE 'Asia/Shanghai')::date {op} :{k}_day"
                args[k + "_day"] = date_value(req.query_params[k])
        return result(req, page(db, q, args, req, "a.occurred_at DESC,a.audit_id"))


@routes.get("/manage/statistics")
def stats(req: Request):
    with engine().begin() as db:
        principal(db, req, S)
        end = date_value(req.query_params.get("to")) or today()
        start = date_value(req.query_params.get("from")) or end - timedelta(days=6)
        require(0 <= (end - start).days < 366, "统计日期范围无效。")
        topic = req.query_params.get("topic")
        cond = (
            " WHERE (occurred_at AT TIME ZONE 'Asia/Shanghai')::date BETWEEN :a AND :b"
        )
        args = dict(a=start, b=end)
        if topic:
            cond += " AND topic=:topic"
            args["topic"] = topic
        counts = many(
            db,
            "SELECT (occurred_at AT TIME ZONE 'Asia/Shanghai')::date AS day,outcome,count(*) AS n FROM query_event"
            + cond
            + " GROUP BY 1,2",
            **args,
        )
        totals = {k: 0 for k in ("SUCCESS", "NO_EVIDENCE", "REFUSED", "SERVICE_ERROR")}
        days = {
            start + timedelta(days=i): dict(totals)
            for i in range((end - start).days + 1)
        }
        for row in counts:
            totals[row["outcome"]] += row["n"]
            days[row["day"]][row["outcome"]] = row["n"]
        hotspots = many(
            db,
            "SELECT topic,count(*) AS n FROM query_event"
            + cond
            + " GROUP BY topic ORDER BY n DESC,topic LIMIT 20",
            **args,
        )
        feedback_count = run(
            db,
            "SELECT count(*) FROM feedback WHERE (created_at AT TIME ZONE 'Asia/Shanghai')::date BETWEEN :a AND :b",
            a=start,
            b=end,
        ).scalar_one()
        published = run(
            db,
            "SELECT count(*) FROM document_version WHERE state='PUBLISHED' AND (valid_from IS NULL OR valid_from<=:d) AND (valid_to IS NULL OR valid_to>=:d)",
            d=today(),
        ).scalar_one()
        return result(
            req,
            {
                "counts": totals,
                "total": sum(totals.values()),
                "topics": hotspots,
                "feedback_count": feedback_count,
                "published_count": published,
                "days": [{"day": day, **vals} for day, vals in days.items()],
            },
        )
