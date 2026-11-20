import asyncio
import time
from datetime import timedelta
from fastapi import APIRouter, Request
from .auth import principal
from .core import *
from .knowledge import VISIBLE
from . import model
from .retrieval import active_rules, retrieve, screen, verify

routes = APIRouter()
NOTES = {
    "NO_EVIDENCE": "未找到可支持这个问题的有效依据，请补充文件名称或主题。",
    "REFUSED": "本次内容或引用未通过核验。",
    "SERVICE_ERROR": "模型服务暂时不可用，可稍后重试。",
}


def owned(db, user, sid):
    return found(
        one(
            db,
            "SELECT * FROM conversation WHERE session_id=:s AND owner_id=:u AND status='ACTIVE'",
            s=uid(sid),
            u=user,
        )
    )


def citations(db, mid, role):
    return many(
        db,
        """SELECT c.citation_id,c.order_no,c.locator_snapshot AS locator,c.snippet_snapshot AS snippet,
        c.source_snapshot AS source,v.state AS version_state FROM citation c JOIN knowledge_chunk k USING(chunk_id)
        JOIN document_version v USING(version_id) JOIN version_role vr ON vr.version_id=v.version_id
        WHERE c.message_id=:m AND vr.role_id=:r ORDER BY c.order_no""",
        m=mid,
        r=role,
    )


def materialize(db, saved, role):
    info = dict(saved)
    if info.get("message_id"):
        refs = citations(db, info["message_id"], role)
        if info.get("status") == "SUCCESS" and len(refs) != info.get("citation_count"):
            info.update(answer="相关原文的访问权限已变更。", citations=[])
        else:
            info["citations"] = refs
    return info


@routes.get("/sessions")
def sessions(req: Request):
    with engine().begin() as db:
        u = principal(db, req)
        q = "SELECT session_id,title,created_at,updated_at FROM conversation WHERE owner_id=:u AND status='ACTIVE'"
        args = {"u": u["user_id"]}
        if req.query_params.get("q"):
            q += " AND title ILIKE :q"
            args["q"] = "%" + req.query_params["q"] + "%"
        for k, op in [("from", ">="), ("to", "<=")]:
            if req.query_params.get(k):
                from .knowledge import date_value

                q += f" AND (updated_at AT TIME ZONE 'Asia/Shanghai')::date {op} :{k}_day"
                args[k + "_day"] = date_value(req.query_params[k])
        return result(req, page(db, q, args, req, "updated_at DESC,session_id"))


@routes.post("/sessions")
def new_session(req: Request, data: dict):
    with engine().begin() as db:
        u = principal(db, req)
        row = one(
            db,
            "INSERT INTO conversation(owner_id,title) VALUES(:u,:t) RETURNING session_id,title",
            u=u["user_id"],
            t=field(data, "title", 100, required=False) or "新会话",
        )
        return result(req, row, status=201, code="CREATED")


@routes.patch("/sessions/{sid}")
def rename(req: Request, sid: str, data: dict):
    with engine().begin() as db:
        u = principal(db, req)
        owned(db, u["user_id"], sid)
        run(
            db,
            "UPDATE conversation SET title=:t,updated_at=now() WHERE session_id=:s",
            s=sid,
            t=field(data, "title", 100),
        )
        return result(req)


@routes.delete("/sessions/{sid}")
def remove(req: Request, sid: str, confirm: bool = False):
    with engine().begin() as db:
        u = principal(db, req)
        owned(db, u["user_id"], sid)
        require(confirm, "请确认删除会话。")
        run(
            db,
            "UPDATE conversation SET status='DELETED',deleted_at=now(),updated_at=now() WHERE session_id=:s",
            s=sid,
        )
        audit(db, u["user_id"], "删除会话", sid, rid=req.state.request_id)
        return result(req)


@routes.get("/sessions/{sid}/messages")
def messages(req: Request, sid: str, request_id: str | None = None):
    with engine().begin() as db:
        u = principal(db, req)
        session = owned(db, u["user_id"], sid)
        query = "SELECT message_id,speaker,status,content,created_at,request_id FROM message WHERE session_id=:s"
        args = {"s": sid}
        if request_id:
            args["key"] = uid(request_id)
            query += " AND request_id=:key"
        data = page(
            db,
            query,
            args,
            req,
            "created_at,CASE speaker WHEN 'USER' THEN 0 ELSE 1 END,message_id",
        )
        data["session_title"] = session["title"]
        if request_id:
            data["request_state"] = run(
                db,
                """SELECT CASE WHEN state='RUNNING' AND deadline_at < now() - interval '5 seconds'
                THEN 'EXPIRED' ELSE state END FROM request_guard WHERE session_id=:s AND request_id=:key
                AND role_id=:role AND actor_id=:actor""",
                **args,
                role=u["current_role_id"],
                actor=u["user_id"],
            ).scalar_one_or_none()
        for item in data["items"]:
            if item["speaker"] == "ASSISTANT":
                item["citations"] = citations(
                    db, item["message_id"], u["current_role_id"]
                )
                count = run(
                    db,
                    "SELECT count(*) FROM citation WHERE message_id=:i",
                    i=item["message_id"],
                ).scalar_one()
                if count != len(item["citations"]):
                    item.update(content="相关原文的访问权限已变更。", citations=[])
        return result(req, data)


def history(db, sid, role, rounds):
    if not rounds:
        return []
    return many(
        db,
        """SELECT m.content AS question,d.title FROM message m JOIN message a ON a.request_id=m.request_id
        AND a.speaker='ASSISTANT' JOIN citation c ON c.message_id=a.message_id AND c.order_no=1
        JOIN knowledge_chunk k USING(chunk_id) JOIN document_version v USING(version_id)
        JOIN knowledge_document d USING(document_id) JOIN version_role vr ON vr.version_id=v.version_id
        WHERE m.speaker='USER' AND m.status='SUCCESS' AND m.session_id=:s AND vr.role_id=:r
        ORDER BY m.created_at DESC LIMIT :n""",
        s=sid,
        r=role,
        n=rounds,
    )


def record_hits(db, user_id, key, stage, hits):
    for rule_id, revision, action in sorted(set(hits)):
        audit(
            db,
            user_id,
            "规则命中",
            rule_id,
            summary=f"{stage} · {action} · V{revision}",
            rid=key,
        )


def prompt_data(q, rows, past, rules, config):
    budget = int(config["context_budget"])
    # UTF-8 bytes give a conservative upper bound for byte-level model tokens.
    left = budget - len((model.PROMPT + q).encode()) - 80
    evidence = {}
    rule_hits = set()
    trimmed = False
    for row in rows:
        blocked, body, hits = screen(row["content"], rules)
        rule_hits.update(hits)
        if blocked:
            continue
        label = "E" + str(len(evidence) + 1)
        item = {
            "id": label,
            "title": row["title"],
            "location": row["locator"],
            "text": body,
        }
        cost = len(json_text(item).encode())
        if cost > left:
            trimmed = True
            continue
        evidence[label] = row | {"model_text": body, "input": item}
        left -= cost
    short = []
    for turn in past:
        blocked, clean, hits = screen(turn["question"], rules)
        rule_hits.update(hits)
        if blocked:
            continue
        item = {"question": clean, "subject": turn["title"]}
        cost = len(json_text(item).encode())
        if cost > left:
            trimmed = True
            break
        short.append(item)
        left -= cost
    return (
        evidence,
        {
            "question": q,
            "history": short[::-1],
            "evidence": [r["input"] for r in evidence.values()],
        },
        trimmed,
        rule_hits,
    )


def persist(
    db, u, sid, key, q, status, reason, points, config, started, topic, trimmed
):
    answer = NOTES.get(status, "")
    if status == "SUCCESS":
        lead = {
            "SUMMARY": "依据原文，可归纳为以下要点。",
            "COMPARE": "各来源的表述如下。",
        }.get(reason, "")
        answer = "\n".join(
            ([lead] if lead else [])
            + [f"〔{i}〕{p['quote']}" for i, p in enumerate(points, 1)]
        )
        require(len(answer) <= 8000, "回答超过长度限制。")
    args = dict(s=sid, k=key, st=status, cfg=config["config_id"] if config else None)
    run(
        db,
        "INSERT INTO message(session_id,request_id,speaker,status,content) VALUES(:s,:k,'USER',:st,:q)",
        **args,
        q=q,
    )
    msg = one(
        db,
        """INSERT INTO message(session_id,request_id,speaker,status,content,config_version)
        VALUES(:s,:k,'ASSISTANT',:st,:ans,:cfg) RETURNING message_id,created_at""",
        **args,
        ans=answer,
    )
    for n, p in enumerate(points, 1):
        source = {
            k: p[k]
            for k in (
                "document_id",
                "version_id",
                "version_no",
                "title",
                "source_name",
                "source_url",
                "valid_from",
                "valid_to",
            )
        }
        run(
            db,
            """INSERT INTO citation(message_id,chunk_id,order_no,snippet_snapshot,locator_snapshot,source_snapshot)
            VALUES(:m,:c,:n,:snap,:loc,CAST(:src AS jsonb))""",
            m=msg["message_id"],
            c=p["chunk_id"],
            n=n,
            snap=p["quote"],
            loc=p["locator"],
            src=json_text(source),
        )
    run(
        db,
        "UPDATE conversation SET updated_at=now(),title=CASE WHEN title='新会话' THEN :q ELSE title END WHERE session_id=:s",
        q=q[:36],
        s=sid,
    )
    run(
        db,
        """INSERT INTO query_event(request_id,session_id,outcome,reason_code,latency_ms,topic)
        VALUES(:k,:s,:st,:why,:ms,:topic)""",
        **args,
        why=reason,
        ms=max(0, int((time.monotonic() - started) * 1000)),
        topic=topic,
    )
    info = dict(
        msg,
        answer=answer,
        status=status,
        reason_code=reason,
        citation_count=len(points),
        context_truncated=trimmed,
    )
    run(
        db,
        "UPDATE request_guard SET state='DONE',result_ref=CAST(:d AS jsonb) WHERE request_id=:k",
        k=key,
        d=json_text(info),
    )
    audit(
        db,
        u["user_id"],
        "问答",
        sid,
        "SUCCESS" if status == "SUCCESS" else "FAILURE",
        reason,
        key,
    )
    return materialize(db, info, u["current_role_id"])


def response(req, info):
    status = info["status"]
    return result(
        req,
        info,
        code=status,
        message=NOTES.get(status, ""),
        status=(504 if info.get("reason_code") == "MODEL_TIMEOUT" else 503)
        if status == "SERVICE_ERROR"
        else 200,
    )


@routes.post("/sessions/{sid}/questions")
async def ask(req: Request, sid: str, data: dict):
    started = time.monotonic()
    key = uid(req.headers.get("Idempotency-Key"))
    signature = digest(json_text(data))
    trimmed = False
    topic = "未分类"
    points = []
    evidence = {}
    with engine().begin() as db:
        u = principal(db, req)
        owned(db, u["user_id"], sid)
        if data.get("faq_id"):
            require(not data.get("question"), "常见问题与自由提问应单独提交。")
            q = found(
                one(
                    db,
                    "SELECT question FROM faq WHERE faq_id=:i AND published",
                    i=uid(data["faq_id"]),
                )
            )["question"]
        else:
            q = field(data, "question", 2000)
        run(db, "SELECT pg_advisory_xact_lock(hashtextextended(:k,0))", k=key)
        existing = one(db, "SELECT * FROM request_guard WHERE request_id=:k", k=key)
        if existing:
            require(
                existing["actor_id"] == u["user_id"]
                and str(existing["session_id"]) == sid
                and existing["role_id"] == u["current_role_id"]
                and signature == existing["input_digest"],
                "请求标识与已提交内容不一致。",
                409,
                "REQUEST_CONFLICT",
            )
            if existing["state"] == "RUNNING":
                return result(
                    req, {"status": "RUNNING"}, code="REQUEST_RUNNING", status=202
                )
            return response(
                req, materialize(db, existing["result_ref"], u["current_role_id"])
            )
        cfg = one(db, "SELECT * FROM model_config WHERE status='ACTIVE'")
        timeout = int(cfg["timeout_seconds"]) if cfg else 60
        run(
            db,
            """INSERT INTO request_guard(request_id,actor_id,role_id,session_id,action,input_digest,state,deadline_at,config_version)
            VALUES(:k,:u,:r,:s,'问答',:sig,'RUNNING',:deadline,:cfg)""",
            k=key,
            u=u["user_id"],
            r=u["current_role_id"],
            s=sid,
            sig=signature,
            deadline=now() + timedelta(seconds=timeout),
            cfg=cfg["config_id"] if cfg else None,
        )
        rules = active_rules(db)
        blocked, clean, hits = screen(q, rules)
        record_hits(db, u["user_id"], key, "输入", hits)
        if blocked:
            q = "〔已拦截的问题〕"
            status, reason = "REFUSED", "INPUT_RULE"
        elif not cfg:
            status, reason = "SERVICE_ERROR", "MODEL_CONFIG_MISSING"
        else:
            past = (
                []
                if data.get("reset_context")
                else history(db, sid, u["current_role_id"], cfg["context_rounds"] + 1)
            )
            if len(past) > cfg["context_rounds"]:
                trimmed = True
                past = past[: cfg["context_rounds"]]
            rows = retrieve(
                db,
                u["current_role_id"],
                clean,
                data.get("category_id"),
                past[0]["title"] if past else None,
                cfg,
            )
            if rows:
                topic = rows[0]["category"]
            evidence, payload, budget_trim, evidence_hits = prompt_data(
                clean, rows, past, rules, cfg
            )
            record_hits(db, u["user_id"], key, "模型上下文", evidence_hits)
            trimmed = trimmed or budget_trim
            status, reason = (
                ("PENDING", "") if evidence else ("NO_EVIDENCE", "NO_VISIBLE_MATCH")
            )
            q = clean
    output_hits = set()
    if status == "PENDING":
        try:
            remaining = timeout - (time.monotonic() - started) - 0.22
            if remaining <= 0:
                raise model.ModelError("MODEL_TIMEOUT")
            async with asyncio.timeout(remaining):
                candidate = await model.generate(cfg, payload, remaining)
            status, reason, points = verify(candidate, evidence, rules, output_hits)
        except (model.ModelError, TimeoutError) as exc:
            status, reason = "SERVICE_ERROR", str(exc) or "MODEL_TIMEOUT"
        except Exception:
            status, reason = "SERVICE_ERROR", "VERIFICATION_ERROR"
    with engine().begin() as db:
        guard = one(
            db,
            "SELECT state,result_ref FROM request_guard WHERE request_id=:k FOR UPDATE",
            k=key,
        )
        live = principal(db, req)
        owned(db, u["user_id"], sid)
        require(
            live["current_role_id"] == u["current_role_id"],
            "当前角色已变更，请重新提问。",
            409,
            "REQUEST_CONFLICT",
        )
        if guard["state"] == "DONE":
            return response(
                req, materialize(db, guard["result_ref"], live["current_role_id"])
            )
        record_hits(db, u["user_id"], key, "模型输出", output_hits)
        if points:
            for doc in sorted({p["document_id"] for p in points}):
                run(
                    db,
                    "SELECT document_id FROM knowledge_document WHERE document_id=:d FOR UPDATE",
                    d=doc,
                )
            vids = list({p["version_id"] for p in points})
            cnt = run(
                db,
                "SELECT count(*) FROM document_version v JOIN knowledge_document d USING(document_id) WHERE "
                + VISIBLE
                + " AND v.version_id=ANY(:vids)",
                role=u["current_role_id"],
                day=today(),
                vids=vids,
            ).scalar_one()
            if cnt != len(vids):
                status, reason, points = "NO_EVIDENCE", "VERSION_CHANGED", []
        info = persist(
            db, u, sid, key, q, status, reason, points, cfg, started, topic, trimmed
        )
        return response(req, info)
