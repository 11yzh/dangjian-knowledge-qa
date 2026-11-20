import asyncio
import hashlib
import json
import os
import re
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path
from uuid import uuid4
import httpx
import pytest
from fastapi.testclient import TestClient
from test_acceptance import (
    add_rule,
    adminmodel,
    answer,
    data,
    execute,
    key,
    reject,
    sqlread,
    status,
)


def test_tc27_rule_modes(s):
    full = "松塔测试词"
    other = "前缀" + full + "后缀"
    rule = add_rule(s, match_type="EXACT")
    assert s.obj("O1", "POST", "/manage/rules/preview", {"text": full})["blocked"]
    assert not s.obj("O1", "POST", "/manage/rules/preview", {"text": other})["blocked"]
    for act in ("MASK", "LOG", "BLOCK"):
        add_rule(s, action=act)
    p = s.obj("O1", "POST", "/manage/rules/preview", {"text": other})
    assert p["blocked"] and full not in p["text"]
    assert status(s.ask(other)) == "REFUSED"
    assert (
        sqlread("SELECT count(*) AS n FROM audit_log WHERE action='规则命中'")["n"] >= 3
    )


def test_tc28_disabled_allowlist(s):
    block = add_rule(s, pattern="资料", action="BLOCK")
    add_rule(s, pattern="学习资料", allowlist=True)
    assert not s.obj("O1", "POST", "/manage/rules/preview", {"text": "学习资料"})[
        "blocked"
    ]
    assert s.obj("O1", "POST", "/manage/rules/preview", {"text": "学习资料和其他资料"})[
        "blocked"
    ]
    s.obj(
        "O1",
        "PUT",
        "/manage/rules/" + block["rule_id"],
        {"rule_version": 1, "enabled": False},
    )
    assert not s.obj("O1", "POST", "/manage/rules/preview", {"text": "其他资料"})[
        "blocked"
    ]


def test_tc29_candidate_verifier(s, monkeypatch):
    from app import model

    assert answer(s)["citations"]
    for candidate, outcome in [
        (
            {"mode": "EXCERPT", "points": [{"evidence_id": "E99", "quote": "原句。"}]},
            "REFUSED",
        ),
        (
            {
                "mode": "EXCERPT",
                "points": [{"evidence_id": "E1", "quote": "改写的日期。"}],
            },
            "REFUSED",
        ),
        ({}, "SERVICE_ERROR"),
    ]:

        async def custom(*args):
            return candidate

        monkeypatch.setattr(model, "generate", custom)
        r = s.ask()
        assert status(r) == outcome and data_or_empty(r)["citations"] == []


def data_or_empty(r):
    return r.json().get("data") or {}


def test_tc30_access_before_model(s, monkeypatch):
    from app import model

    seen = []

    async def fake(cfg, payload, seconds):
        seen.append(payload)
        return {"mode": "NO_EVIDENCE", "points": []}

    monkeypatch.setattr(model, "generate", fake)
    assert status(s.ask("《研讨资料发放样例》的联络说明编号是什么？")) == "NO_EVIDENCE"
    assert not seen
    assert status(s.ask("模型服务凭据是什么？")) == "REFUSED" and not seen
    assert status(s.ask()) == "NO_EVIDENCE"
    assert "LAB-RESTRICT" not in json.dumps(seen)


def test_tc31_config_boundaries(s, monkeypatch):
    from app import model

    for name, (lo, hi) in {
        "timeout_seconds": (5, 120),
        "top_k": (1, 20),
        "context_rounds": (0, 20),
        "context_budget": (1000, 32000),
        "threshold": (0, 1),
    }.items():
        for val in (lo, hi):
            assert adminmodel(s, **{name: val}).is_success
        for val in (lo - 1, hi + 1):
            reject(adminmodel(s, **{name: val}))
    b = data(adminmodel(s))
    base = "/manage/model-configs/" + b["config_id"]
    active = str(s.seed["config"])
    reject(s.req("O1", "POST", base + "/activate", {"expected_active_id": active}))

    async def fail(*a):
        raise model.ModelError("MODEL_HTTP_401")

    monkeypatch.setattr(model, "probe", fail)
    assert s.req("O1", "POST", base + "/test", {}).status_code == 503

    async def ok(*a):
        return None

    monkeypatch.setattr(model, "probe", ok)
    assert s.obj("O1", "POST", base + "/test", {})["verified"]
    s.obj("O1", "POST", base + "/activate", {"expected_active_id": active})
    s.obj(
        "O1",
        "POST",
        "/manage/model-configs/" + active + "/activate",
        {"expected_active_id": b["config_id"]},
    )
    assert (
        sqlread("SELECT count(*) AS n FROM model_config WHERE status='ACTIVE'")["n"]
        == 1
    )


def test_tc32_config_snapshot(s, monkeypatch):
    from app import model
    from app.knowledge import sentences

    b = data(adminmodel(s, api_key="TEST-CREDENTIAL-42"))
    bid = b["config_id"]
    old = str(s.seed["config"])
    execute("UPDATE model_config SET verified=true WHERE config_id=:i", i=bid)
    gate = threading.Event()
    resume = threading.Event()
    observed = []

    async def delayed(cfg, payload, seconds):
        observed.append(str(cfg["config_id"]))
        gate.set()
        await asyncio.to_thread(resume.wait, 4)
        e = payload["evidence"][0]
        return {
            "mode": "EXCERPT",
            "points": [{"evidence_id": e["id"], "quote": sentences(e["text"])[0]}],
        }

    monkeypatch.setattr(model, "generate", delayed)
    with ThreadPoolExecutor(1) as ex:
        pending = ex.submit(s.ask)
        assert gate.wait(8)
        s.obj(
            "O1",
            "POST",
            "/manage/model-configs/" + bid + "/activate",
            {"expected_active_id": old},
        )
        resume.set()
        assert status(pending.result()) == "SUCCESS"
    s.ask()
    assert observed == [old, bid]
    assert (
        "TEST-CREDENTIAL-42"
        not in s.req("O1", "GET", "/manage/model-configs").text
        + s.req("O1", "GET", "/manage/audit").text
    )


def test_tc33_event_totals(s):
    sid = s.session()
    c = s.client("U1")
    k = key()
    s.ask(sid=sid, key=k)
    s.ask(sid=sid, key=k)
    s.ask("没有依据的未收录题", sid=sid)
    r = s.obj("O1", "GET", "/manage/statistics")
    # Event time uses the real server clock; query its Beijing date explicitly.
    day = sqlread(
        "SELECT (occurred_at AT TIME ZONE 'Asia/Shanghai')::date AS d FROM query_event LIMIT 1"
    )["d"]
    r = s.obj("O1", "GET", f"/manage/statistics?from={day}&to={day}")
    assert r["total"] == 2 == sum(r["counts"].values())
    assert r["counts"]["SUCCESS"] == 1 and r["counts"]["NO_EVIDENCE"] == 1
    assert r["feedback_count"] == 0 and len(r["days"]) == 1
    blank = s.obj("O1", "GET", "/manage/statistics?from=2000-01-01&to=2000-01-02")
    assert len(blank["days"]) == 2 and blank["total"] == 0


def test_tc34_append_only_audit(s):
    from app.core import engine, run
    from sqlalchemy.exc import DBAPIError

    reject(s.req("U1", "GET", "/manage/audit"))
    s.client("O1")
    with engine().begin() as db:
        assert not run(
            db, "SELECT has_table_privilege(current_user,'audit_log','UPDATE')"
        ).scalar()
    # The disposable database deliberately simulates a failed mandatory audit.
    execute("REVOKE INSERT ON audit_log FROM dq_app")
    r = s.req(
        "O1",
        "PATCH",
        "/manage/users/" + str(s.seed["users"]["U1"]),
        {"status": "DISABLED"},
    )
    assert r.status_code == 503 and r.json()["request_id"]
    assert (
        sqlread("SELECT status FROM user_account WHERE username='U1'")["status"]
        == "ACTIVE"
    )


def test_tc35_scoring_dataset(s):
    from scripts.quality import ANCHORS
    from scripts.qa_env import PROJECT

    q = json.loads((PROJECT / "samples/course.json").read_text("utf-8"))["questions"]
    assert len(q) == 50 and len(ANCHORS) == 34 and sum(map(len, ANCHORS)) == 64
    assert all(x.get("expected") and x.get("basis") for x in q)
    # Real responses, citation checks and human sign-off are recorded separately by scripts.quality.


def test_tc36_five_user_load(s):
    import uvicorn
    from sqlalchemy import text
    from app.main import app
    from app.core import engine, one, run, FILES
    from app.auth import create_user

    credentials = dict(s.seed["passwords"])
    with engine(True).begin() as db:
        for name in ("P3", "P4", "P5"):
            credentials[name] = uuid4().hex
            create_user(db, name, name, credentials[name], ["USER"], False)
        initial = run(db, "SELECT count(*) FROM knowledge_chunk").scalar_one()
        n = 1000 - run(db, "SELECT count(*) FROM knowledge_document").scalar_one()
        role = one(db, "SELECT role_id FROM role WHERE role_code='USER'")["role_id"]
        docs = []
        versions = []
        per, rem = divmod(100000 - initial, n)
        for i in range(n):
            did, vid = str(uuid4()), str(uuid4())
            count = per + (i < rem)
            title = "负载资料" + str(i)
            base = "用于本机列表性能抽样的资料。"
            raw = ("\n".join([base] * count)).encode()
            file = "perf/" + vid + ".txt"
            p = FILES / file
            p.parent.mkdir(exist_ok=True, parents=True)
            p.write_bytes(raw)
            docs.append(
                {"d": did, "c": s.seed["cats"]["资料使用"], "t": title, "v": vid}
            )
            versions.append(
                {
                    "d": did,
                    "v": vid,
                    "p": file,
                    "sha": hashlib.sha256(raw).hexdigest(),
                    "n": len(raw),
                    "u": s.seed["users"]["K1"],
                    "r": role,
                    "count": count,
                }
            )
        db.execute(
            text(
                "INSERT INTO knowledge_document(document_id,category_id,title,source_name) VALUES(:d,:c,:t,'课程压力样本')"
            ),
            docs,
        )
        db.execute(
            text("""INSERT INTO document_version(version_id,document_id,version_no,file_type,file_path,sha256,size_bytes,state,
           index_state,uploader_id,reviewer_id,published_at) VALUES(:v,:d,1,'TXT',:p,:sha,:n,'PUBLISHED','READY',:u,:u,now())"""),
            versions,
        )
        db.execute(
            text(
                "UPDATE knowledge_document SET current_version_id=:v WHERE document_id=:d"
            ),
            docs,
        )
        db.execute(text("INSERT INTO version_role VALUES(:v,:r)"), versions)
        db.execute(
            text("""INSERT INTO knowledge_chunk(version_id,ordinal,locator,content,search_vector)
            SELECT :v,g,'第 '||g||' 行','用于本机列表性能抽样的资料。',to_tsvector('simple','性能 资料')
            FROM generate_series(1,:count) g"""),
            versions,
        )
        assert run(db, "SELECT count(*) FROM knowledge_chunk").scalar_one() == 100000
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False))
    thread = threading.Thread(
        target=server.run, kwargs={"sockets": [sock]}, daemon=True
    )
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)

    def sample(user):
        with httpx.Client(
            base_url=f"http://127.0.0.1:{port}/api/v1", trust_env=False, timeout=30
        ) as c:
            me = c.post(
                "/auth/login", json={"username": user, "password": credentials[user]}
            ).json()["data"]
            c.headers["X-CSRF-Token"] = me["csrf_token"]
            sid = c.post("/sessions", json={}).json()["data"]["session_id"]
            a = c.post(
                "/sessions/" + sid + "/questions",
                json={
                    "question": "《支部学习资料归档样例》第一条要求在活动结束后多久提交？"
                },
                headers={"Idempotency-Key": key()},
            ).json()["data"]
            endpoints = [
                "/sessions",
                "/sessions/" + sid + "/messages",
                "/knowledge",
                "/faqs",
                "/citations/" + a["citations"][0]["citation_id"],
            ]
            for url in endpoints:
                c.get(url).raise_for_status()
            records = []
            for _ in range(20):
                for i, url in enumerate(endpoints):
                    start = time.perf_counter()
                    res = c.get(url)
                    res.raise_for_status()
                    records.append((i, (time.perf_counter() - start) * 1000))
            return records

    try:
        with ThreadPoolExecutor(5) as pool:
            parts = list(pool.map(sample, ["U1", "U2", "P3", "P4", "P5"]))
    finally:
        server.should_exit = True
        thread.join(timeout=8)
        sock.close()
    records = sum(parts, [])
    times = sorted(v for _, v in records)
    report = {
        "documents": 1000,
        "chunks": 100000,
        "users": 5,
        "requests": len(times),
        "errors": 0,
        "p95_ms": round(times[474], 2),
        "by_endpoint_p95_ms": [
            round(sorted(v for j, v in records if j == i)[94], 2) for i in range(5)
        ],
    }
    (s.root / "performance.json").write_text(json.dumps(report, indent=2), "utf-8")
    assert len(times) == 500 and times[474] <= 2000, report


def test_tc37_deadline(s, monkeypatch):
    from app import model

    async def late(*args):
        await asyncio.sleep(10)

    monkeypatch.setattr(model, "generate", late)
    execute("UPDATE model_config SET timeout_seconds=5 WHERE status='ACTIVE'")
    start = time.monotonic()
    r = s.ask()
    elapsed = time.monotonic() - start
    assert r.status_code == 504 and status(r) == "SERVICE_ERROR" and elapsed < 5.5, (
        elapsed,
        r.text,
    )
    (s.root / "deadline.json").write_text(
        json.dumps({"configured_s": 5, "end_to_end_s": round(elapsed, 3)}), "utf-8"
    )


def test_tc38_persistence_and_fault_isolation(s, monkeypatch):
    from app import model
    from app.core import engine

    sid = s.session()
    a = data(s.ask(sid=sid))
    engine().dispose()
    assert s.obj("U1", "GET", "/sessions/" + sid + "/messages")["total"] == 2

    async def unavailable(*args):
        raise model.ModelError("MODEL_HTTP_429")

    monkeypatch.setattr(model, "generate", unavailable)
    assert status(s.ask(sid=sid)) == "SERVICE_ERROR"
    assert s.obj("U1", "GET", "/knowledge")["total"] >= 3
    assert s.obj("U1", "GET", "/citations/" + a["citations"][0]["citation_id"])[
        "snippet"
    ]


def test_tc39_permissions_and_csrf(s):
    from app.main import app

    c = s.client("U1")
    reject(c.get("/api/v1/manage/audit"))
    assert (
        c.post(
            "/api/v1/sessions", json={}, headers={"X-CSRF-Token": "invalid"}
        ).status_code
        == 403
    )
    assert (
        c.post(
            "/api/v1/sessions", json={}, headers={"Origin": "https://example.invalid"}
        ).status_code
        == 403
    )
    cookies = (
        c.post(
            "/api/v1/auth/login",
            json={"username": "U1", "password": s.seed["passwords"]["U1"]},
        )
        .headers["set-cookie"]
        .lower()
    )
    assert "httponly" in cookies and "samesite=strict" in cookies
    with TestClient(app, client=("192.0.2.2", 1234)) as remote:
        assert remote.get("/").status_code == 403


def test_tc40_keyboard_controls(s):
    # Full keyboard and viewport runs on the running instance are recorded by browser_check.
    c = s.client("U1")
    assert c.get("/").is_success
    js = c.get("/static/app.js").text
    assert "aria-label" in js and "输入问题" in js and "确认删除" in js
    assert "focus-visible" in c.get("/static/styles.css").text


def test_tc41_rebuild(s):
    from scripts.maintenance import rebuild

    a = answer(s)
    cid = a["citations"][0]["citation_id"]
    prior = s.obj("U1", "GET", "/citations/" + cid)
    execute("UPDATE knowledge_chunk SET search_vector=''::tsvector")
    assert status(s.ask()) == "NO_EVIDENCE"
    assert rebuild() > 0
    assert status(s.ask()) == "SUCCESS"
    after = s.obj("U1", "GET", "/citations/" + cid)
    assert (
        prior["locator"] == after["locator"]
        and prior["version_id"] == after["version_id"]
    )
    reqid = sqlread(
        "SELECT request_id FROM message WHERE message_id=:m", m=a["message_id"]
    )["request_id"]
    assert s.obj("O1", "GET", "/manage/audit?request_id=" + reqid)["total"] > 0


def test_tc42_restore(s):
    from scripts.maintenance import backup, restore_check

    answer(s)
    out = restore_check(backup())
    assert (
        out["files_verified"] >= 9
        and out["indexed_chunks"] > 0
        and out["counts"]["citation"] >= 1
    )
    (s.root / "restore.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), "utf-8"
    )


def test_tc43_retention(s):
    from scripts.maintenance import housekeeping, backup

    sid = s.session()
    a = data(s.ask(sid=sid))
    s.obj("U1", "DELETE", "/sessions/" + sid + "?confirm=true")
    execute(
        "UPDATE conversation SET deleted_at=now()-interval '181 days' WHERE session_id=:i",
        i=sid,
    )
    execute(
        "INSERT INTO audit_log(action,target,result,summary,occurred_at) VALUES('过期测试','','SUCCESS','',now()-interval '181 days')"
    )
    housekeeping()
    assert (
        sqlread("SELECT count(*) AS n FROM message WHERE session_id=:i", i=sid)["n"]
        == 0
    )
    assert (
        sqlread("SELECT count(*) AS n FROM audit_log WHERE action='过期测试'")["n"] == 0
    )
    out = s.root / "backups"
    out.mkdir(exist_ok=True)
    # Put seven older archives in this dedicated test directory before rotating.
    for n in range(7):
        (out / f"20000101-00000{n}.zip").write_bytes(b"test archive")
    backup()
    assert (
        len(list(out.glob("*.zip"))) == 7 and not (out / "20000101-000000.zip").exists()
    )
