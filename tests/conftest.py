"""Each case receives its own PostgreSQL database cloned from the frozen course fixture."""

import json
import os
from pathlib import Path
from uuid import uuid4
from urllib.parse import urlsplit
import psycopg
from psycopg import sql
import pytest
from fastapi.testclient import TestClient
from scripts import qa_env


@pytest.fixture(scope="session")
def baseline():
    root = qa_env.create_database()
    seed = qa_env.seed_fixture()
    from app.core import engine

    for pool in (engine(), engine(True)):
        pool.dispose()
    engine.cache_clear()
    return root, seed, os.environ["DATABASE_URL"], os.environ["DANGJIAN_OWNER_URL"]


class Scenario:
    def __init__(self, root, seed):
        self.root, self.seed = root, seed
        self.clients = {}

    def client(self, name):
        from app.main import app

        if name not in self.clients:
            client = TestClient(app)
            response = client.post(
                "/api/v1/auth/login",
                json={"username": name, "password": self.seed["passwords"][name]},
            )
            assert response.status_code == 200, response.text
            client.headers["X-CSRF-Token"] = response.json()["data"]["csrf_token"]
            self.clients[name] = client
        return self.clients[name]

    def req(self, user, method, path, data=None, key=None, **kw):
        return self.client(user).request(
            method,
            "/api/v1" + path,
            json=data,
            headers={"Idempotency-Key": key or str(uuid4())},
            **kw,
        )

    def obj(self, user, method, path, data=None, **kw):
        r = self.req(user, method, path, data, **kw)
        assert r.is_success, (r.status_code, r.text)
        return r.json()["data"]

    def session(self, user="U1"):
        return self.obj(user, "POST", "/sessions", {})["session_id"]

    def ask(
        self,
        q="《支部学习资料归档样例》第一条要求在活动结束后多久提交？",
        sid=None,
        user="U1",
        key=None,
    ):
        return self.req(
            user,
            "POST",
            "/sessions/" + (sid or self.session(user)) + "/questions",
            {"question": q},
            key=key,
        )

    def upload(self, filename, bits, **extra):
        data = {
            "title": "新资料",
            "source_name": "课程原文",
            "category_id": str(self.seed["cats"]["资料归档"]),
        } | extra
        key = data.pop("key", None) or str(uuid4())
        return self.client("K1").post(
            "/api/v1/manage/versions",
            files={"file": (filename, bits)},
            data=data,
            headers={"Idempotency-Key": key},
        )

    def version(self, k):
        return str(self.seed["versions"][k]["version_id"])

    def lifecycle(self, k, action, payload, user="K1"):
        return self.req(
            user, "POST", "/manage/versions/" + self.version(k) + "/" + action, payload
        )

    def review(self, k="V2", **params):
        from app.core import engine, one

        vid = self.version(k)
        with engine().connect() as db:
            row = one(
                db,
                "SELECT v.revision,d.current_version_id FROM document_version v JOIN knowledge_document d USING(document_id) WHERE v.version_id=:v",
                v=vid,
            )
        return {
            "decision": "PUBLISH",
            "review_note": "核对来源与原文",
            "role_ids": ["USER"],
            "revision": row["revision"],
            "base_current_version_id": str(row["current_version_id"])
            if row["current_version_id"]
            else None,
        } | params


@pytest.fixture
def s(baseline, monkeypatch):
    root, seed, app_url, owner_url = baseline
    parts = urlsplit(owner_url)
    base_db = parts.path.strip("/")
    name = "dq_qa_case_" + uuid4().hex[:12]
    assert name.startswith("dq_qa_case_") and base_db.startswith("dq_qa_")
    conn = dict(
        host=parts.hostname,
        port=parts.port,
        user=parts.username,
        password=parts.password,
        dbname="postgres",
    )
    with psycopg.connect(**conn, autocommit=True) as pg:
        pg.execute(
            sql.SQL("CREATE DATABASE {} TEMPLATE {}").format(
                sql.Identifier(name), sql.Identifier(base_db)
            )
        )
    monkeypatch.setenv("DATABASE_URL", qa_env.with_db(app_url, name))
    monkeypatch.setenv("DANGJIAN_OWNER_URL", qa_env.with_db(owner_url, name))
    from app.core import engine
    from app import model
    from app.knowledge import sentences

    async def excerpt(cfg, payload, timeout):
        e = payload["evidence"][0]
        return {
            "mode": "EXCERPT",
            "points": [{"evidence_id": e["id"], "quote": sentences(e["text"])[0]}],
        }

    monkeypatch.setattr(model, "generate", excerpt)
    scenario = Scenario(root, seed)
    yield scenario
    for client in scenario.clients.values():
        client.close()
    for pool in (engine(), engine(True)):
        pool.dispose()
    engine.cache_clear()
    with psycopg.connect(**conn, autocommit=True) as pg:
        pg.execute(
            sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name))
        )
