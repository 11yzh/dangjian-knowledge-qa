"""Provision a disposable PostgreSQL fixture from the course's own sample texts."""

import json
import os
import shutil
import uuid
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
import psycopg
from psycopg import sql

PROJECT = Path(__file__).resolve().parents[1]
BASE = PROJECT / ".local"


def with_db(url, name):
    parts = urlsplit(url)
    return urlunsplit(
        (parts.scheme, parts.netloc, "/" + name, parts.query, parts.fragment)
    )


def create_database():
    cfg = json.loads((BASE / "database.json").read_text("utf-8"))
    owner = urlsplit(cfg["owner_url"])
    unique = "dq_qa_" + uuid.uuid4().hex[:12]
    root = BASE / "verification" / unique
    root.mkdir(parents=True)
    connection = dict(
        host=owner.hostname,
        port=owner.port,
        user=owner.username,
        password=owner.password,
        dbname="postgres",
    )
    with psycopg.connect(**connection, autocommit=True) as pg:
        pg.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(unique)))
    os.environ.update(
        DANGJIAN_DATA=str(root),
        DATABASE_URL=with_db(cfg["app_url"], unique),
        DANGJIAN_OWNER_URL=with_db(cfg["owner_url"], unique),
    )
    (root / "vault").mkdir()
    if (BASE / "vault/deepseek").exists():
        shutil.copyfile(BASE / "vault/deepseek", root / "vault/deepseek")
    (root / "database.json").write_text(
        json.dumps(
            {
                "app_url": os.environ["DATABASE_URL"],
                "owner_url": os.environ["DANGJIAN_OWNER_URL"],
            }
        ),
        "utf-8",
    )
    (BASE / "latest-qa.txt").write_text(str(root), "utf-8")
    from alembic import command
    from alembic.config import Config

    command.upgrade(Config(str(PROJECT / "alembic.ini")), "head")
    with psycopg.connect(**(connection | {"dbname": unique})) as pg:
        pg.execute("GRANT USAGE ON SCHEMA public TO dq_app")
        pg.execute(
            "GRANT SELECT,INSERT,UPDATE,DELETE ON ALL TABLES IN SCHEMA public TO dq_app"
        )
        pg.execute("REVOKE UPDATE,DELETE,TRUNCATE ON audit_log FROM dq_app")
    return root


def sample_bytes(key, revision=1):
    import io
    from docx import Document
    from reportlab.pdfgen.canvas import Canvas
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont

    data = json.loads((PROJECT / "samples/course.json").read_text("utf-8"))[
        "materials"
    ][key]
    texts = [x["text"] for x in data]
    if key == "M01":
        if revision == 2:
            texts[0] = texts[0].replace("3 个", "5 个")
        out = io.BytesIO()
        pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
        doc = Canvas(out, pagesize=(595, 842))
        for start in (0, 3):
            doc.setFont("STSong-Light", 13)
            for i, line in enumerate(texts[start : start + 3]):
                doc.drawString(36, 770 - 42 * i, line)
            doc.showPage()
        doc.save()
        return "pdf", out.getvalue()
    if key == "M02":
        doc = Document()
        doc.add_heading("报名说明", 1)
        for line in texts:
            doc.add_paragraph(line)
        out = io.BytesIO()
        doc.save(out)
        return "docx", out.getvalue()
    return "txt", "\n".join(texts).encode()


def freeze_day():
    from datetime import date
    from app import core

    def fixed():
        return date(2026, 11, 19)

    for module in ("core", "knowledge", "chat", "retrieval", "operations"):
        __import__("app." + module, fromlist=["today"]).today = fixed
    import scripts.maintenance as maint

    maint.today = fixed


def seed_fixture():
    import secrets
    from app.core import engine, one, run, USER, ADMIN, KNOWLEDGE
    from app.auth import create_user
    from app.knowledge import add_version, transition, work_one
    from app.vault import store_secret

    freeze_day()
    users = {}
    passwords = {}
    cats = {}
    versions = {}
    roles = {
        "U1": [USER],
        "U2": [USER],
        "U4": [USER],
        "R1": [USER, KNOWLEDGE],
        "K1": [KNOWLEDGE],
        "K2": [KNOWLEDGE],
        "O1": [ADMIN],
    }
    docs = {
        "M01": ("支部学习资料归档样例", "资料归档"),
        "M02": ("党员学习活动报名样例", "活动报名"),
        "M03": ("党建学习资料库使用样例", "资料使用"),
        "M04": ("研讨资料发放样例", "资料使用"),
        "M05": ("支部学习资料归档补充样例", "资料归档"),
        "M06": ("旧版活动器材借用样例", "资料使用"),
        "M07": ("撤回版学习笔记登记样例", "资料使用"),
        "M08": ("资料联络岗位样例", "资料使用"),
        "M09": ("演示标记词样例", "资料使用"),
    }
    with engine().begin() as db:
        for user, scope in roles.items():
            pwd = secrets.token_urlsafe(12)
            users[user] = create_user(db, user, user, pwd, scope, False)["user_id"]
            passwords[user] = pwd
        run(
            db,
            "UPDATE user_account SET status='DISABLED' WHERE user_id=:u",
            u=users["U4"],
        )
        for name in ("资料归档", "活动报名", "资料使用"):
            cats[name] = one(
                db,
                "INSERT INTO category(name) VALUES(:n) RETURNING category_id",
                n=name,
            )["category_id"]
        for k, (title, cat) in docs.items():
            ext, b = sample_bytes(k)
            versions[k] = add_version(
                db,
                users["K1"],
                k + "." + ext,
                b,
                {
                    "title": title,
                    "source_name": "课程测试样稿",
                    "category_id": str(cats[cat]),
                    "valid_from": "2026-11-19" if k == "M01" else None,
                    "valid_to": "2026-11-30" if k == "M01" else None,
                },
            )
        ext, b = sample_bytes("M01", 2)
        versions["V2"] = add_version(
            db,
            users["K1"],
            "修订.pdf",
            b,
            {
                "document_id": versions["M01"]["document_id"],
                "title": docs["M01"][0],
                "source_name": "课程测试样稿",
                "category_id": str(cats["资料归档"]),
            },
        )
    while work_one():
        pass
    with engine().begin() as db:
        for k, item in versions.items():
            if k in ("V2", "M05", "M08", "M09"):
                continue
            row = one(
                db,
                "SELECT revision,state FROM document_version WHERE version_id=:v",
                v=item["version_id"],
            )
            assert row["state"] == "REVIEW_PENDING", (k, row)
            approved = transition(
                db,
                users["K1"],
                item["version_id"],
                "review",
                {
                    "revision": row["revision"],
                    "decision": "PUBLISH",
                    "role_ids": [KNOWLEDGE] if k == "M04" else [USER],
                    "base_current_version_id": None,
                    "review_note": "测试初始版本",
                },
            )
            if k == "M06":
                run(
                    db,
                    "UPDATE document_version SET valid_to='2026-11-18' WHERE version_id=:v",
                    v=item["version_id"],
                )
            if k == "M07":
                transition(
                    db,
                    users["K1"],
                    item["version_id"],
                    "withdraw",
                    {"revision": approved["revision"], "reason": "测试撤回"},
                )
        config = one(
            db,
            """INSERT INTO model_config(provider,model_name,secret_ref,status,verified,timeout_seconds,top_k,threshold,context_rounds,context_budget,updated_by)
          VALUES('DEEPSEEK','deepseek-flash','deepseek','ACTIVE',true,60,5,0.6,10,8000,:u) RETURNING config_id""",
            u=users["O1"],
        )
        run(
            db,
            "INSERT INTO safety_rule(pattern,match_type,action,updated_by) VALUES('模型服务凭据','CONTAINS','BLOCK',:u)",
            u=users["O1"],
        )
        for k in ("归档", "流程", "报名", "索引"):
            run(db, "INSERT INTO tag(name) VALUES(:t)", t=k)
    return {
        "users": users,
        "passwords": passwords,
        "cats": cats,
        "versions": versions,
        "config": config["config_id"],
    }
