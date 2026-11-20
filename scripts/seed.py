import asyncio
import json
import os
import secrets
import urllib.request
from pathlib import Path
from app.core import *
from app.auth import create_user
from app.knowledge import add_version, work_one, transition
from app.model import probe, ModelError
from app.vault import read_secret

MANIFEST = json.loads((ROOT / "samples/constitution.json").read_text("utf-8"))


def source_file():
    path = STATE / "constitution.pdf"
    packaged = ROOT / "samples" / "constitution.pdf"
    if not path.is_file() and packaged.is_file():
        path = packaged
    if not path.is_file():
        with urllib.request.urlopen(MANIFEST["download"], timeout=75) as response:
            raw = response.read(MAX_BYTES + 1)
        require(
            digest(raw) == MANIFEST["sha256"],
            "党章 PDF 摘要与清单不符，请核对来源版本。",
        )
        path.write_bytes(raw)
    data = path.read_bytes()
    require(digest(data) == MANIFEST["sha256"], "党章原文校验失败。")
    return data


def setup():
    STATE.mkdir(parents=True, exist_ok=True)
    credentials = STATE / "演示账号.txt"
    people = {}
    with engine().begin() as db:
        run(db, "SELECT pg_advisory_xact_lock(673105)")
        for name, display, roles in [
            ("reader", "学习读者", [USER]),
            ("editor", "资料管理员", [KNOWLEDGE]),
            ("admin", "系统管理员", [ADMIN]),
        ]:
            person = one(
                db, "SELECT user_id FROM user_account WHERE username_key=:n", n=name
            )
            if not person:
                pwd = secrets.token_urlsafe(12)
                person = create_user(db, name, display, pwd, roles, False)
                with os.fdopen(
                    os.open(credentials, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600),
                    "a",
                    encoding="utf-8",
                ) as f:
                    f.write(f"{display}    {name}    {pwd}\n")
            people[name] = person["user_id"]
        cat = one(
            db,
            "SELECT category_id FROM category WHERE name='党章党规' AND parent_id IS NULL",
        )
        if not cat:
            cat = one(
                db,
                "INSERT INTO category(name) VALUES('党章党规') RETURNING category_id",
            )
        tag = one(
            db,
            "INSERT INTO tag(name) VALUES('党章') ON CONFLICT(name) DO UPDATE SET name=excluded.name RETURNING tag_id",
        )
        for pattern in (
            "API 密钥",
            "API密钥",
            "模型服务凭据",
            "系统密钥",
            "API Key",
            "模型密钥",
            "服务密钥",
        ):
            if not one(db, "SELECT 1 FROM safety_rule WHERE pattern=:t", t=pattern):
                run(
                    db,
                    "INSERT INTO safety_rule(pattern,match_type,action,updated_by,description) VALUES(:t,'CONTAINS','BLOCK',:u,'保护服务端凭据')",
                    t=pattern,
                    u=people["admin"],
                )
        for seq, (title, q) in enumerate(
            [
                ("党员的义务", "党章第三条规定党员必须履行哪些义务？"),
                ("预备党员的预备期", "预备党员的预备期为多长时间？"),
                ("党旗的图案", "中国共产党的党旗是什么样的？"),
            ],
            1,
        ):
            if not one(db, "SELECT 1 FROM faq WHERE title=:t", t=title):
                run(
                    db,
                    "INSERT INTO faq(title,question,category_id,sort_order,published,updated_by) VALUES(:t,:q,:c,:n,true,:u)",
                    t=title,
                    q=q,
                    c=cat["category_id"],
                    n=seq,
                    u=people["editor"],
                )
        exists = one(
            db,
            "SELECT version_id FROM document_version WHERE sha256=:s",
            s=MANIFEST["sha256"],
        )
    if not exists:
        raw = source_file()
        with engine().begin() as db:
            obj = add_version(
                db,
                people["editor"],
                "中国共产党章程.pdf",
                raw,
                {
                    "title": MANIFEST["title"],
                    "category_id": str(cat["category_id"]),
                    "source_name": MANIFEST["source_name"],
                    "source_url": MANIFEST["source_url"],
                    "valid_from": MANIFEST["approved"],
                },
            )
            run(
                db,
                "INSERT INTO document_tag VALUES(:d,:t)",
                d=obj["document_id"],
                t=tag["tag_id"],
            )
        while work_one():
            pass
        with engine().begin() as db:
            v = one(
                db,
                "SELECT state,revision FROM document_version WHERE version_id=:v",
                v=obj["version_id"],
            )
            require(v["state"] == "REVIEW_PENDING", "党章原文未完成索引。")
            transition(
                db,
                people["editor"],
                obj["version_id"],
                "review",
                {
                    "decision": "PUBLISH",
                    "role_ids": [USER, KNOWLEDGE, ADMIN],
                    "revision": v["revision"],
                    "base_current_version_id": None,
                    "review_note": "已核对共产党员网正式版本、47 页原文及下载摘要。",
                },
            )
            audit(
                db,
                people["editor"],
                "初始化发布",
                obj["version_id"],
                summary="中国共产党章程（2022年通过）",
            )
    with engine().begin() as db:
        active = one(db, "SELECT config_id FROM model_config WHERE status='ACTIVE'")
    if not active and read_secret("deepseek"):
        cfg = dict(
            provider="DEEPSEEK",
            model_name="deepseek-flash",
            secret_ref="deepseek",
            timeout_seconds=60,
        )
        try:
            asyncio.run(probe(cfg))
        except ModelError as exc:
            print("模型暂未启用：" + str(exc))
        else:
            with engine().begin() as db:
                run(db, "SELECT pg_advisory_xact_lock(673104)")
                if not one(db, "SELECT 1 FROM model_config WHERE status='ACTIVE'"):
                    run(
                        db,
                        """INSERT INTO model_config(provider,model_name,secret_ref,status,verified,timeout_seconds,top_k,threshold,context_rounds,context_budget,updated_by)
                        VALUES('DEEPSEEK',:m,'deepseek','ACTIVE',true,60,5,0.6,10,8000,:u)""",
                        m=cfg["model_name"],
                        u=people["admin"],
                    )
                    audit(
                        db,
                        people["admin"],
                        "启用模型配置",
                        cfg["model_name"],
                        summary="固定样句经真实 API 校验",
                    )
    print("演示账号和党章知识库已就绪。")


if __name__ == "__main__":
    setup()
