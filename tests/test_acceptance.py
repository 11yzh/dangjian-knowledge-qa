import asyncio
import io
import json
import os
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from uuid import uuid4
import pytest
from fastapi.testclient import TestClient


def status(r):
    return r.json()["data"]["status"]


def data(r):
    assert r.is_success, (r.status_code, r.text)
    return r.json()["data"]


def key():
    return str(uuid4())


def sqlread(q, **p):
    from app.core import engine, one

    with engine().connect() as db:
        return one(db, q, **p)


def execute(q, **p):
    from app.core import engine, run

    with engine(True).begin() as db:
        run(db, q, **p)


def reject(r):
    assert r.status_code in (400, 401, 403, 404, 409, 413, 415), r.text


def answer(s):
    r = s.ask()
    assert status(r) == "SUCCESS", r.text
    return data(r)


def adminmodel(s, **params):
    return s.req(
        "O1",
        "POST",
        "/manage/model-configs",
        {"provider": "DEEPSEEK", "model_name": "deepseek-flash", **params},
    )


def add_rule(s, pattern="松塔测试词", **params):
    return s.obj(
        "O1",
        "POST",
        "/manage/rules",
        {"pattern": pattern, "match_type": "CONTAINS", "action": "BLOCK", **params},
    )


def test_tc01_login_logout(s):
    c = s.client("U1")
    assert c.get("/api/v1/me").json()["data"]["role_code"] == "USER"
    assert c.post("/api/v1/auth/logout").is_success
    assert c.get("/api/v1/me").status_code == 401


def test_tc02_bad_or_disabled(s):
    from app.main import app

    with TestClient(app) as c:
        for n, p in [
            ("", "p"),
            ("U1", ""),
            ("U99", "bad"),
            ("U1", "bad"),
            ("U4", s.seed["passwords"]["U4"]),
        ]:
            reject(c.post("/api/v1/auth/login", json={"username": n, "password": p}))
        assert c.get("/api/v1/me").status_code == 401


def test_tc03_login_policy_and_expiry(s):
    from app.main import app

    with TestClient(app) as c:
        for _ in range(5):
            reject(
                c.post("/api/v1/auth/login", json={"username": "U2", "password": "bad"})
            )
        assert sqlread(
            "SELECT locked_until>now() AS locked FROM user_account WHERE username='U2'"
        )["locked"]
        reject(
            c.post(
                "/api/v1/auth/login",
                json={"username": "U2", "password": s.seed["passwords"]["U2"]},
            )
        )
        execute(
            "UPDATE user_account SET locked_until=now()-interval '1 second' WHERE username='U2'"
        )
        assert s.client("U2").get("/api/v1/me").is_success
        execute(
            "UPDATE auth_session SET expires_at=now()-interval '1 second' WHERE user_id=:u",
            u=s.seed["users"]["U2"],
        )
        assert s.client("U2").get("/api/v1/me").status_code == 401
        assert (
            s.obj(
                "O1",
                "PUT",
                "/manage/auth-policy",
                dict(max_failures=3, lock_minutes=15, idle_minutes=30),
            )["max_failures"]
            == 3
        )


def test_tc04_accounts(s):
    from app.main import app

    payload = dict(
        username="qa_new",
        display_name="测试",
        initial_password="Course-pass-21",
        role_ids=["USER"],
    )
    acct = s.obj("O1", "POST", "/manage/users", payload)
    reject(s.req("O1", "POST", "/manage/users", payload | {"username": " QA_NEW "}))
    with TestClient(app) as c:
        m = data(
            c.post(
                "/api/v1/auth/login",
                json={"username": "qa_new", "password": payload["initial_password"]},
            )
        )
        assert m["must_change_password"]
        c.headers["X-CSRF-Token"] = m["csrf_token"]
        reject(c.get("/api/v1/sessions"))
        assert c.put(
            "/api/v1/auth/password",
            json={
                "current_password": payload["initial_password"],
                "new_password": "Course-changed-23",
            },
        ).is_success
        s.obj(
            "O1",
            "POST",
            "/manage/users/" + acct["user_id"] + "/reset",
            {"temporary_password": "Course-reset-27"},
        )
        reject(
            c.post(
                "/api/v1/auth/login",
                json={"username": "qa_new", "password": "Course-changed-23"},
            )
        )
        assert data(
            c.post(
                "/api/v1/auth/login",
                json={"username": "qa_new", "password": "Course-reset-27"},
            )
        )["must_change_password"]
        s.obj("O1", "PATCH", "/manage/users/" + acct["user_id"], {"status": "DISABLED"})
        reject(c.get("/api/v1/me"))


def test_tc05_roles(s):
    assert (
        s.obj("R1", "PUT", "/auth/role", {"role_code": "KNOWLEDGE_ADMIN"})["role_code"]
        == "KNOWLEDGE_ADMIN"
    )
    assert s.req("R1", "GET", "/manage/versions").is_success
    reject(s.req("U1", "PUT", "/auth/role", {"role_code": "KNOWLEDGE_ADMIN"}))
    s.obj(
        "O1",
        "PATCH",
        "/manage/users/" + str(s.seed["users"]["R1"]),
        {"role_ids": ["USER"]},
    )
    reject(s.req("R1", "GET", "/manage/versions"))


def test_tc06_ownership(s):
    sid = s.session()
    a = data(s.ask(sid=sid))
    for path in [
        "/sessions/" + sid + "/messages",
        "/citations/" + a["citations"][0]["citation_id"],
    ]:
        reject(s.req("U2", "GET", path))
    reject(
        s.req(
            "U2",
            "PUT",
            "/messages/" + a["message_id"] + "/feedback",
            {"rating": "USEFUL"},
        )
    )
    reject(s.req("K1", "GET", "/manage/users"))
    for changes in [{"status": "DISABLED"}, {"role_ids": ["USER"]}, {"role_ids": []}]:
        reject(
            s.req("O1", "PATCH", "/manage/users/" + str(s.seed["users"]["O1"]), changes)
        )


def test_tc07_answer_and_formats(s):
    for q in (
        "《支部学习资料归档样例》第一条要求在活动结束后多久提交？",
        "《党员学习活动报名样例》规定哪些人可以参加？",
        "《党建学习资料库使用样例》怎样定义主题索引？",
    ):
        a = data(s.ask(q))
        assert a["status"] == "SUCCESS", a
        for ref in a["citations"]:
            c = s.obj("U1", "GET", "/citations/" + ref["citation_id"])
            assert c["locator"] and c["source"]["version_no"] == 1
            assert s.req(
                "U1",
                "GET",
                "/versions/"
                + c["version_id"]
                + "/file?citation_id="
                + c["citation_id"],
            ).is_success


def test_tc08_idempotence(s):
    sid = s.session()
    k = key()
    with ThreadPoolExecutor(max_workers=2) as pool:
        r = list(pool.map(lambda _: s.ask(sid=sid, key=k), range(2)))
    assert all(x.status_code in (200, 202) for x in r)
    a = data(s.ask(sid=sid, key=k))
    assert a["status"] == "SUCCESS"
    reject(s.ask("其他问题", sid=sid, key=k))
    assert (
        sqlread("SELECT count(*) AS n FROM query_event WHERE request_id=:k", k=k)["n"]
        == 1
    )
    assert (
        sqlread("SELECT count(*) AS n FROM message WHERE request_id=:k", k=k)["n"] == 2
    )


def test_tc09_question_limits(s):
    for q in ("", "  ", "测" * 2001):
        reject(s.ask(q))
    for q in ("字", "a" * 2000):
        assert s.ask(q).status_code == 200


def test_tc10_no_evidence_and_filter(s):
    assert status(s.ask("《旧版活动器材借用样例》的借用编号是什么？")) == "NO_EVIDENCE"
    r = s.req(
        "U1",
        "POST",
        "/sessions/" + s.session() + "/questions",
        {
            "question": "《支部学习资料归档样例》第一条要求在活动结束后多久提交？",
            "category_id": str(s.seed["cats"]["活动报名"]),
        },
    )
    assert status(r) == "NO_EVIDENCE" and not data(r)["citations"]


def test_tc11_context(s, monkeypatch):
    from app import model
    from app.knowledge import sentences

    calls = []

    async def monitor(cfg, payload, timeout):
        calls.append(payload)
        row = payload["evidence"][0]
        return {
            "mode": "EXCERPT",
            "points": [{"evidence_id": row["id"], "quote": sentences(row["text"])[0]}],
        }

    monkeypatch.setattr(model, "generate", monitor)
    sid = s.session()
    assert (
        status(s.ask("《党员学习活动报名样例》每个场次名额为多少人？", sid))
        == "SUCCESS"
    )
    assert status(s.ask("那它适用于谁？", sid)) == "SUCCESS" and calls[-1]["history"]
    assert status(s.ask("那它适用于谁？", s.session())) == "NO_EVIDENCE"
    r = s.obj(
        "U1",
        "POST",
        "/sessions/" + sid + "/questions",
        {
            "question": "《党建学习资料库使用样例》什么是主题索引？",
            "reset_context": True,
        },
    )
    assert r["status"] == "SUCCESS" and not calls[-1]["history"]


def test_tc12_history(s):
    sid = s.session()
    s.obj("U1", "PATCH", "/sessions/" + sid, {"title": "归档学习记录"})
    assert s.obj("U1", "GET", "/sessions?q=归档学习记录")["total"] == 1
    assert s.obj("U2", "GET", "/sessions?q=归档学习记录")["total"] == 0
    reject(s.req("U1", "DELETE", "/sessions/" + sid))
    s.obj("U1", "DELETE", "/sessions/" + sid + "?confirm=true")
    reject(s.req("U1", "GET", "/sessions/" + sid + "/messages"))


def test_tc13_faq(s):
    f = s.obj(
        "K1",
        "POST",
        "/manage/faqs",
        dict(
            title="归档",
            question="《支部学习资料归档样例》截止期限是什么？",
            category_id=str(s.seed["cats"]["资料归档"]),
            published=False,
        ),
    )
    assert s.obj("U1", "GET", "/faqs")["total"] == 0
    s.obj(
        "K1",
        "PATCH",
        "/manage/faqs/" + f["faq_id"],
        {"published": True, "sort_order": 2},
    )
    assert s.obj("U1", "GET", "/faqs")["total"] == 1
    path = "/sessions/" + s.session() + "/questions"
    assert (
        data(s.req("U1", "POST", path, {"faq_id": f["faq_id"]}))["status"] == "SUCCESS"
    )
    s.obj("K1", "PATCH", "/manage/faqs/" + f["faq_id"], {"published": False})
    reject(s.req("U1", "POST", path, {"faq_id": f["faq_id"]}))


def feedback(s):
    a = answer(s)
    path = "/messages/" + a["message_id"] + "/feedback"
    old = s.obj("U1", "PUT", path, {"rating": "USEFUL"})
    f = s.obj(
        "U1",
        "PUT",
        path,
        {"rating": "NOT_USEFUL", "error_type": "原文修订", "comment": "复核适用期"},
    )
    assert f["feedback_id"] == old["feedback_id"]
    return f


def test_tc14_feedback_workflow(s):
    f = feedback(s)
    path = "/manage/feedback/" + f["feedback_id"]
    assert s.obj("K1", "GET", path)["comment"] == "复核适用期"
    v = s.obj(
        "K1",
        "POST",
        path + "/actions",
        dict(to_status="PROCESSING", note="领取", revision=f["revision"]),
    )
    s.obj(
        "K1",
        "POST",
        path + "/actions",
        dict(
            to_status="RESOLVED",
            note="关联新版本",
            revision=v["revision"],
            new_version_id=s.version("V2"),
        ),
    )
    assert s.obj("U1", "GET", "/feedback")["items"][0]["actions"][-1][
        "new_version_id"
    ] == s.version("V2")


def test_tc15_feedback_rejection(s):
    f = feedback(s)
    path = "/manage/feedback/" + f["feedback_id"] + "/actions"
    assert s.obj("U2", "GET", "/feedback")["total"] == 0
    v = s.obj(
        "K1",
        "POST",
        path,
        dict(to_status="PROCESSING", note="核对", revision=f["revision"]),
    )
    s.obj(
        "K1",
        "POST",
        path,
        dict(to_status="REJECTED", note="原文对应正确", revision=v["revision"]),
    )
    assert (
        s.obj("U1", "GET", "/feedback")["items"][0]["actions"][-1]["note"]
        == "原文对应正确"
    )


def test_tc16_parse_formats(s):
    from scripts.qa_env import sample_bytes
    from app.knowledge import work_one

    for k in ("M01", "M02", "M03"):
        ext, b = sample_bytes(k)
        a = s.upload("source." + ext, b)
        assert a.status_code == 202
        while work_one():
            pass
        chunks = s.obj(
            "K1", "GET", "/manage/versions/" + data(a)["version_id"] + "/chunks"
        )
        assert chunks["total"] and any(
            x in chunks["items"][0]["locator"] for x in ("页", "段", "行")
        )


def test_tc17_file_boundaries(s):
    from app.knowledge import work_one
    from pypdf import PdfWriter

    limit = 20 * 1024 * 1024
    for clean in (b"a", b"a" * limit):
        assert s.upload("demo.txt", clean).status_code == 202
    assert s.upload("demo.txt", b"a" * (limit + 1)).status_code == 413
    for name, b in [
        ("x.txt", b""),
        ("x.jpg", b"xx"),
        ("x.pdf", b"xx"),
        ("x.txt", b"\xff"),
        ("x.docx", b"not word"),
    ]:
        reject(s.upload(name, b))
    for encrypted in (False, True):
        p = PdfWriter()
        p.add_blank_page(width=100, height=100)
        if encrypted:
            p.encrypt("test-only")
        f = io.BytesIO()
        p.write(f)
        j = data(s.upload("blank.pdf", f.getvalue()))["job_id"]
        # Only process this job; the size boundary already proved intake.
        execute(
            "UPDATE ingest_job SET state='SUCCEEDED' WHERE state='QUEUED' AND job_id<>:j",
            j=j,
        )
        while work_one():
            pass
        assert s.obj("K1", "GET", "/manage/jobs/" + j)["state"] == "FAILED"


def test_tc18_duplicate(s):
    k = key()
    doc = str(s.seed["versions"]["M03"]["document_id"])
    opts = {"document_id": doc, "key": k}
    a = data(s.upload("new.txt", b"new", **opts))
    b = data(s.upload("new.txt", b"new", **opts))
    c = data(s.upload("new.txt", b"new", document_id=doc))
    assert a["version_id"] == b["version_id"] == c["version_id"] and c["duplicate"]
    assert (
        data(s.upload("new.txt", b"newer", document_id=doc))["version_no"]
        == a["version_no"] + 1
    )


def test_tc19_job_retry(s, monkeypatch):
    from app import knowledge

    job = data(s.upload("data.txt", "可正常解析的一句。".encode()))
    original = knowledge.parse

    def broken(*args):
        raise RuntimeError("controlled failure")

    monkeypatch.setattr(knowledge, "parse", broken)
    assert knowledge.work_one()
    assert s.obj("K1", "GET", "/manage/jobs/" + job["job_id"])["state"] == "FAILED"
    s.obj("K1", "POST", "/manage/jobs/" + job["job_id"] + "/retry", {})
    monkeypatch.setattr(knowledge, "parse", original)
    knowledge.work_one()
    j = s.obj("K1", "GET", "/manage/jobs/" + job["job_id"])
    assert j["attempt"] == 2 and j["state"] == "SUCCEEDED"
    assert (
        s.obj("K1", "GET", "/manage/versions/" + job["version_id"] + "/chunks")["total"]
        == 1
    )


def test_tc20_publish(s):
    p = s.review()
    reject(s.lifecycle("V2", "review", p | {"role_ids": []}))
    execute(
        "UPDATE document_version SET valid_from='2027-01-01' WHERE version_id=:v",
        v=s.version("V2"),
    )
    reject(s.lifecycle("V2", "review", p))
    execute(
        "UPDATE document_version SET valid_from=NULL WHERE version_id=:v",
        v=s.version("V2"),
    )
    assert data(s.lifecycle("V2", "review", p))["state"] == "PUBLISHED"


def test_tc21_reject(s):
    p = s.review(decision="REJECT")
    assert data(s.lifecycle("V2", "review", p))["state"] == "REJECTED"
    reject(s.lifecycle("V2", "review", s.review()))
    from app.knowledge import work_one

    v = data(
        s.upload(
            "revision.txt",
            "修订材料。".encode(),
            document_id=str(s.seed["versions"]["M01"]["document_id"]),
        )
    )
    assert v["version_no"] == 3
    work_one()
    assert (
        sqlread(
            "SELECT state FROM document_version WHERE version_id=:v", v=v["version_id"]
        )["state"]
        == "REVIEW_PENDING"
    )


def test_tc22_categories_and_filters(s):
    tag = s.obj("K1", "POST", "/manage/tags", {"name": "课程标签"})
    doc = str(s.seed["versions"]["M01"]["document_id"])
    s.obj("K1", "PATCH", "/manage/documents/" + doc, {"tag_ids": [tag["tag_id"]]})
    listing = s.obj(
        "U1",
        "GET",
        "/knowledge?tag_id="
        + tag["tag_id"]
        + "&category_id="
        + str(s.seed["cats"]["资料归档"])
        + "&page_size=1",
    )
    assert listing["total"] == 1 and listing["items"][0]["document_id"] == doc
    assert all(
        r["title"] != "研讨资料发放样例"
        for r in s.obj("U1", "GET", "/knowledge")["items"]
    )
    reject(s.req("U1", "GET", "/knowledge?page_size=101"))


def test_tc23_dictionary_cycles(s):
    p = s.obj("K1", "POST", "/manage/categories", {"name": "父分类"})
    c = s.obj(
        "K1",
        "POST",
        "/manage/categories",
        {"name": "子分类", "parent_id": p["category_id"]},
    )
    reject(
        s.req(
            "K1",
            "PATCH",
            "/manage/categories/" + p["category_id"],
            {"name": "父分类", "parent_id": c["category_id"]},
        )
    )
    reject(s.req("K1", "DELETE", "/manage/categories/" + p["category_id"]))
    s.obj("K1", "DELETE", "/manage/categories/" + c["category_id"])
    s.obj("K1", "DELETE", "/manage/categories/" + p["category_id"])


def test_tc24_version_and_snapshot(s):
    a = answer(s)
    ref = a["citations"][0]
    assert "3" in a["answer"]
    assert data(s.lifecycle("V2", "review", s.review()))["state"] == "PUBLISHED"
    b = answer(s)
    assert "5" in b["answer"] and b["citations"][0]["source"]["version_no"] == 2
    old = s.obj("U1", "GET", "/citations/" + ref["citation_id"])
    assert old["source"]["version_no"] == 1 and old["version_state"] == "ARCHIVED"
    execute("DELETE FROM version_role WHERE version_id=:v", v=s.version("M01"))
    reject(s.req("U1", "GET", "/citations/" + ref["citation_id"]))


def test_tc25_withdraw_expire(s, monkeypatch):
    p = s.review("M01")
    w = data(
        s.lifecycle("M01", "withdraw", {"revision": p["revision"], "reason": "核验"})
    )
    assert status(s.ask()) == "NO_EVIDENCE"
    re = data(
        s.lifecycle("M01", "reopen", {"revision": w["revision"], "reason": "重审"})
    )
    assert re["state"] == "REVIEW_PENDING"
    data(s.lifecycle("M01", "review", s.review("M01")))
    from app import retrieval
    from scripts import maintenance

    monkeypatch.setattr(retrieval, "today", lambda: date(2026, 11, 30))
    assert status(s.ask()) == "SUCCESS"
    monkeypatch.setattr(retrieval, "today", lambda: date(2026, 12, 1))
    assert status(s.ask()) == "NO_EVIDENCE"
    monkeypatch.setattr(maintenance, "today", lambda: date(2026, 12, 1))
    maintenance.housekeeping()
    assert (
        sqlread(
            "SELECT state FROM document_version WHERE version_id=:v", v=s.version("M01")
        )["state"]
        == "WITHDRAWN"
    )


def test_tc26_concurrent_publish(s):
    p = s.review()
    with ThreadPoolExecutor(2) as pool:
        rs = list(
            pool.map(lambda who: s.lifecycle("V2", "review", p, who), ["K1", "K2"])
        )
    assert sorted(r.status_code for r in rs) == [200, 409]
    assert (
        sqlread(
            "SELECT count(*) AS n FROM document_version WHERE document_id=:d AND state='PUBLISHED'",
            d=s.seed["versions"]["M01"]["document_id"],
        )["n"]
        == 1
    )
