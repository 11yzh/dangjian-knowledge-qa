import hashlib
import json
import os
import re
from datetime import datetime, timezone, timedelta
from functools import lru_cache
from pathlib import Path
from uuid import UUID, uuid4

from fastapi import Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from sqlalchemy import create_engine, text

ROOT = Path(__file__).resolve().parents[1]
STATE = Path(os.environ.get("DANGJIAN_DATA", ROOT / ".local")).resolve()
FILES = STATE / "originals"
BJ = timezone(timedelta(hours=8))
USER, KNOWLEDGE, ADMIN = "USER", "KNOWLEDGE_ADMIN", "SYSTEM_ADMIN"
MAX_BYTES = 20 * 1024 * 1024


def database_url(owner=False):
    env = "DANGJIAN_OWNER_URL" if owner else "DATABASE_URL"
    if os.getenv(env):
        return os.environ[env]
    cfg = json.loads((STATE / "database.json").read_text("utf-8"))
    return cfg["owner_url" if owner else "app_url"]


@lru_cache
def engine(owner=False):
    return create_engine(
        database_url(owner),
        pool_pre_ping=True,
        hide_parameters=True,
        pool_size=10,
        max_overflow=12,
        connect_args={"connect_timeout": 5},
    )


def run(db, sql, **args):
    return db.execute(text(sql), args)


def one(db, sql, **args):
    row = run(db, sql, **args).mappings().first()
    return dict(row) if row else None


def many(db, sql, **args):
    return [dict(row) for row in run(db, sql, **args).mappings()]


def now():
    return datetime.now(timezone.utc)


def today():
    return now().astimezone(BJ).date()


def json_text(value):
    return json.dumps(
        jsonable_encoder(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def digest(value):
    return hashlib.sha256(
        value if isinstance(value, bytes) else value.encode()
    ).hexdigest()


class Problem(Exception):
    def __init__(self, message, status=400, code="INVALID_ARGUMENT"):
        self.message, self.status, self.code = message, status, code


def require(condition, message, status=400, code="INVALID_ARGUMENT"):
    if not condition:
        raise Problem(message, status, code)


def uid(value):
    try:
        return str(UUID(str(value)))
    except (ValueError, TypeError):
        raise Problem("对象编号格式无效。") from None


def field(data, key, limit, *, required=True):
    val = data.get(key)
    require(val is None or isinstance(val, str), f"{key} 必须是文本。")
    value = (val or "").strip()
    require(
        (value or not required) and len(value) <= limit, f"请检查 {key} 的内容和长度。"
    )
    return value


def integer(value, lower, upper):
    require(
        type(value) is int and lower <= value <= upper,
        f"数值应在 {lower} 至 {upper} 之间。",
    )
    return value


def boolean(value):
    require(isinstance(value, bool), "请选择有效的开关状态。")
    return value


def found(row):
    require(row is not None, "对象不存在或当前角色无权访问。", 404, "NOT_FOUND")
    return row


def page(db, query, params, request, order):
    try:
        number = int(request.query_params.get("page", 1))
        size = int(request.query_params.get("page_size", 20))
    except ValueError:
        raise Problem("分页参数无效。") from None
    require(number >= 1 and 1 <= size <= 100, "页码从 1 开始，每页可显示 1 至 100 条。")
    total = run(
        db, f"SELECT count(*) FROM ({query}) AS candidates", **params
    ).scalar_one()
    rows = many(
        db,
        query + " ORDER BY " + order + " LIMIT :sz OFFSET :off",
        **params,
        sz=size,
        off=(number - 1) * size,
    )
    return {"items": rows, "page": number, "page_size": size, "total": total}


def result(request, data=None, *, code="OK", message="", status=200):
    return JSONResponse(
        jsonable_encoder(
            {
                "request_id": getattr(request.state, "request_id", None),
                "code": code,
                "message": message,
                "data": data,
            }
        ),
        status_code=status,
    )


def audit(db, actor, action, target="", result="SUCCESS", summary="", rid=None):
    run(
        db,
        """INSERT INTO audit_log(actor_id,request_id,action,target,result,summary)
        VALUES(:u,:r,:a,:t,:ok,:s)""",
        u=actor,
        r=rid,
        a=action,
        t=str(target),
        ok=result,
        s=summary,
    )


def ids_for_roles(db, choices):
    require(
        isinstance(choices, list) and 0 < len(choices) <= 3, "请至少选择一个有效角色。"
    )
    values = list(dict.fromkeys(map(str, choices)))
    rows = many(
        db,
        "SELECT * FROM role WHERE role_code=ANY(:x) OR role_id::text=ANY(:x)",
        x=values,
    )
    require(len(rows) == len(values), "授权角色无效。")
    return [r["role_id"] for r in rows]


def mutation(db, request, actor, action, payload, execute):
    key = uid(request.headers.get("Idempotency-Key"))
    signature = digest(json_text(payload))
    run(db, "SELECT pg_advisory_xact_lock(hashtextextended(:key,0))", key=key)
    old = one(db, "SELECT * FROM request_guard WHERE request_id=:k", k=key)
    if old:
        require(
            old["actor_id"] == actor["user_id"]
            and old["role_id"] == actor["current_role_id"]
            and old["action"] == action
            and old["input_digest"] == signature,
            "这个请求标识已用于其他操作。",
            409,
            "REQUEST_CONFLICT",
        )
        return old["result_ref"]
    data = execute()
    run(
        db,
        """INSERT INTO request_guard(request_id,actor_id,role_id,action,input_digest,state,deadline_at,result_ref)
        VALUES(:k,:u,:r,:a,:sig,'DONE',now(),CAST(:data AS jsonb))""",
        k=key,
        u=actor["user_id"],
        r=actor["current_role_id"],
        a=action,
        sig=signature,
        data=json_text(data),
    )
    target = next(
        (str(v) for k, v in request.path_params.items() if k not in ("action", "kind")),
        "",
    )
    if not target and isinstance(data, dict):
        target = next((str(v) for k, v in data.items() if k.endswith("_id") and v), "")
    audit(db, actor["user_id"], action, target or request.url.path, rid=key)
    return data
