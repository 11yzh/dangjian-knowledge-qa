import hmac
import secrets
from datetime import timedelta

from argon2 import PasswordHasher
from argon2.exceptions import VerificationError
from fastapi import APIRouter, Request
from .core import *

routes = APIRouter()
passwords = PasswordHasher()
DUMMY_HASH = passwords.hash(secrets.token_hex(24))


def match_password(encoded, raw):
    try:
        return passwords.verify(encoded, raw)
    except (VerificationError, ValueError):
        return False


def acceptable(raw):
    require(
        isinstance(raw, str) and 8 <= len(raw) <= 128, "密码长度应为 8 至 128 个字符。"
    )
    return passwords.hash(raw)


def role_list(db, user_id):
    return many(
        db,
        """SELECT r.role_id,r.role_code FROM user_role x JOIN role r USING(role_id)
        WHERE x.user_id=:u ORDER BY CASE r.role_code WHEN 'USER' THEN 0 WHEN 'KNOWLEDGE_ADMIN' THEN 1 ELSE 2 END""",
        u=user_id,
    )


def principal(db, request, required=None, allow_reset=False):
    token = request.cookies.get("dq_session", "")
    person = one(
        db,
        """SELECT a.auth_id,a.user_id,a.current_role_id,u.username,u.display_name,
        u.must_change_password,r.role_code,p.idle_minutes FROM auth_session a JOIN user_account u USING(user_id)
        JOIN user_role ur ON ur.user_id=a.user_id AND ur.role_id=a.current_role_id
        JOIN role r ON r.role_id=a.current_role_id CROSS JOIN auth_policy p
        WHERE a.token_hash=:h AND u.status='ACTIVE' AND a.expires_at>clock_timestamp()""",
        h=digest(token),
    )
    require(person, "请重新登录。", 401, "AUTH_REQUIRED")
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        expected = digest("csrf:" + token)
        require(
            hmac.compare_digest(request.headers.get("X-CSRF-Token", ""), expected),
            "操作校验失败，请刷新页面。",
            403,
            "ACCESS_DENIED",
        )
    require(
        allow_reset or not person["must_change_password"],
        "请先修改初始密码。",
        403,
        "PASSWORD_CHANGE_REQUIRED",
    )
    require(
        not required or person["role_code"] in required,
        "当前角色没有此操作权限。",
        403,
        "ACCESS_DENIED",
    )
    run(
        db,
        "UPDATE auth_session SET last_active_at=now(),expires_at=now()+make_interval(mins=>:mins) WHERE auth_id=:a",
        mins=person["idle_minutes"],
        a=person["auth_id"],
    )
    return person


def profile(db, user, token):
    return {
        k: user[k]
        for k in (
            "user_id",
            "username",
            "display_name",
            "role_code",
            "must_change_password",
        )
    } | {"roles": role_list(db, user["user_id"]), "csrf_token": digest("csrf:" + token)}


def create_user(db, name, display, password, roles, must_change=True):
    key = name.strip().casefold()
    require(0 < len(key) <= 64 and 0 < len(display) <= 64, "用户名或显示名称过长。")
    have = ids_for_roles(db, roles)
    row = one(
        db,
        """INSERT INTO user_account(username,username_key,display_name,password_hash,must_change_password)
        VALUES(:n,:k,:d,:p,:m) ON CONFLICT(username_key) DO NOTHING RETURNING user_id""",
        n=name.strip(),
        k=key,
        d=display,
        p=acceptable(password),
        m=must_change,
    )
    require(row, "用户名已存在。", 409, "REQUEST_CONFLICT")
    for role_id in have:
        run(db, "INSERT INTO user_role VALUES(:u,:r)", u=row["user_id"], r=role_id)
    return row


@routes.post("/auth/login")
def login(req: Request, data: dict):
    name = field(data, "username", 64).casefold()
    password = data.get("password")
    require(isinstance(password, str) and 0 < len(password) <= 128, "请输入密码。")
    with engine().begin() as db:
        policy = one(db, "SELECT * FROM auth_policy")
        u = one(
            db, "SELECT * FROM user_account WHERE username_key=:n FOR UPDATE", n=name
        )
        instant = now()
        if u and u["locked_until"] and u["locked_until"] <= instant:
            run(
                db,
                "UPDATE user_account SET locked_until=NULL,failed_count=0 WHERE user_id=:u",
                u=u["user_id"],
            )
            u.update(failed_count=0, locked_until=None)
        good = match_password(u["password_hash"] if u else DUMMY_HASH, password)
        if not u or u["status"] != "ACTIVE" or u["locked_until"] or not good:
            if u and u["status"] == "ACTIVE" and not u["locked_until"]:
                attempts = u["failed_count"] + 1
                until = (
                    instant + timedelta(minutes=policy["lock_minutes"])
                    if attempts >= policy["max_failures"]
                    else None
                )
                run(
                    db,
                    "UPDATE user_account SET failed_count=:n,locked_until=:t WHERE user_id=:u",
                    n=attempts,
                    t=until,
                    u=u["user_id"],
                )
            audit(
                db,
                u["user_id"] if u else None,
                "登录",
                result="FAILURE",
                rid=req.state.request_id,
            )
            return result(
                req,
                code="AUTH_REQUIRED",
                message="账号或密码无效，或账号暂时受限。",
                status=401,
            )
        roles = role_list(db, u["user_id"])
        require(roles, "账号缺少可用角色。", 403, "ACCESS_DENIED")
        token = secrets.token_urlsafe(40)
        run(
            db,
            "UPDATE user_account SET failed_count=0,locked_until=NULL WHERE user_id=:u",
            u=u["user_id"],
        )
        run(
            db,
            "INSERT INTO auth_session(user_id,current_role_id,token_hash,expires_at) VALUES(:u,:r,:h,:e)",
            u=u["user_id"],
            r=roles[0]["role_id"],
            h=digest(token),
            e=instant + timedelta(minutes=policy["idle_minutes"]),
        )
        audit(db, u["user_id"], "登录", rid=req.state.request_id)
        u["role_code"] = roles[0]["role_code"]
        resp = result(req, profile(db, u, token))
        resp.set_cookie(
            "dq_session",
            token,
            httponly=True,
            samesite="strict",
            secure=req.url.scheme == "https",
            path="/",
        )
        return resp


@routes.get("/me")
def me(req: Request):
    with engine().begin() as db:
        return result(
            req,
            profile(
                db,
                principal(db, req, allow_reset=True),
                req.cookies.get("dq_session", ""),
            ),
        )


@routes.post("/auth/logout")
def logout(req: Request):
    with engine().begin() as db:
        u = principal(db, req, allow_reset=True)
        run(db, "DELETE FROM auth_session WHERE auth_id=:a", a=u["auth_id"])
        audit(db, u["user_id"], "退出", rid=req.state.request_id)
    resp = result(req)
    resp.delete_cookie("dq_session", path="/")
    return resp


@routes.put("/auth/role")
def switch_role(req: Request, data: dict):
    with engine().begin() as db:
        u = principal(db, req)
        role = next(
            (
                x
                for x in role_list(db, u["user_id"])
                if x["role_code"] == data.get("role_code")
            ),
            None,
        )
        require(role, "未获得该角色授权。", 403, "ACCESS_DENIED")
        run(
            db,
            "UPDATE auth_session SET current_role_id=:r WHERE auth_id=:a",
            r=role["role_id"],
            a=u["auth_id"],
        )
        audit(db, u["user_id"], "角色切换", role["role_code"], rid=req.state.request_id)
        return result(req, profile(db, u | role, req.cookies["dq_session"]))


@routes.put("/auth/password")
def change_password(req: Request, data: dict):
    with engine().begin() as db:
        u = principal(db, req, allow_reset=True)
        old = one(
            db,
            "SELECT password_hash FROM user_account WHERE user_id=:u FOR UPDATE",
            u=u["user_id"],
        )
        require(
            match_password(old["password_hash"], str(data.get("current_password", ""))),
            "当前密码错误。",
        )
        require(
            data.get("new_password") != data.get("current_password"),
            "新密码应与旧密码不同。",
        )
        run(
            db,
            "UPDATE user_account SET password_hash=:p,must_change_password=false WHERE user_id=:u",
            p=acceptable(data.get("new_password")),
            u=u["user_id"],
        )
        run(db, "DELETE FROM auth_session WHERE user_id=:u", u=u["user_id"])
        audit(db, u["user_id"], "修改密码", rid=req.state.request_id)
    resp = result(req, message="密码已修改，请重新登录。")
    resp.delete_cookie("dq_session", path="/")
    return resp
