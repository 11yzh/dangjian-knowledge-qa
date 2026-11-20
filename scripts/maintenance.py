"""Verified logical backups of the database and the original documents."""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import zipfile
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import psycopg
from psycopg import sql
from app.core import *
from app.knowledge import file_for, tokens
from .localdb import pgtool

TABLES = (
    "user_account",
    "knowledge_document",
    "document_version",
    "knowledge_chunk",
    "conversation",
    "message",
    "citation",
)


def conn_args():
    u = urlsplit(database_url(True))
    return dict(
        host=u.hostname,
        port=u.port or 5432,
        user=u.username,
        password=u.password,
        dbname=u.path.lstrip("/"),
    )


def command(name, database, *args):
    p = conn_args()
    env = os.environ.copy()
    env["PGPASSWORD"] = p["password"]
    env["PGCLIENTENCODING"] = "UTF8"
    subprocess.run(
        [
            pgtool(name),
            "-h",
            p["host"],
            "-p",
            str(p["port"]),
            "-U",
            p["user"],
            "-d",
            database,
            *map(str, args),
        ],
        check=True,
        env=env,
        stdout=subprocess.DEVNULL,
    )


def inspect(pg):
    return {
        "counts": {
            t: pg.execute(
                sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(t))
            ).fetchone()[0]
            for t in TABLES
        },
        "files": dict(
            pg.execute(
                "SELECT file_path,sha256 FROM document_version ORDER BY file_path"
            ).fetchall()
        ),
        "pointers": {
            str(k): str(v) if v else None
            for k, v in pg.execute(
                "SELECT document_id,current_version_id FROM knowledge_document"
            )
        },
        "active": [
            str(row[0])
            for row in pg.execute(
                "SELECT config_id FROM model_config WHERE status='ACTIVE'"
            )
        ],
        "migration": pg.execute("SELECT version_num FROM alembic_version").fetchone()[
            0
        ],
    }


def backup():
    folder = STATE / "backups"
    folder.mkdir(parents=True, exist_ok=True)
    stamp = now().astimezone(BJ).strftime("%Y%m%d-%H%M%S-%f")
    out = folder / (stamp + ".zip")
    partial = out.with_suffix(".tmp")
    dump = folder / (stamp + ".dump")
    try:
        with psycopg.connect(**conn_args()) as pg:
            pg.execute("SELECT pg_advisory_xact_lock(673101)")
            with psycopg.connect(**conn_args()) as snap:
                snap.execute(
                    "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"
                )
                snapshot = snap.execute("SELECT pg_export_snapshot()").fetchone()[0]
                meta = inspect(snap)
                command(
                    "pg_dump",
                    conn_args()["dbname"],
                    "-Fc",
                    "--no-owner",
                    "--no-acl",
                    "--snapshot",
                    snapshot,
                    "-f",
                    dump,
                )
            with zipfile.ZipFile(
                partial, "w", zipfile.ZIP_DEFLATED, compresslevel=2
            ) as z:
                z.write(dump, "database.dump")
                for name, sha in meta["files"].items():
                    p = file_for(name)
                    require(digest(p.read_bytes()) == sha, "备份原文摘要不符。")
                    z.write(p, "originals/" + name)
                vault = STATE / "vault"
                if vault.is_dir():
                    for p in vault.iterdir():
                        if p.is_file() and not p.name.endswith(".tmp"):
                            z.write(p, "vault/" + p.name)
                z.writestr("manifest.json", json_text(meta))
        os.replace(partial, out)
        for previous in sorted(folder.glob("*.zip"), reverse=True)[7:]:
            previous.unlink()
        return out
    finally:
        dump.unlink(missing_ok=True)
        partial.unlink(missing_ok=True)


def restore_check(archive):
    cfg = conn_args()
    name = "dq_restore_" + uuid4().hex[:12]
    dest = STATE / "restores" / name
    dest.mkdir(parents=True)
    with zipfile.ZipFile(archive) as z:
        for item in z.infolist():
            require(
                (dest / item.filename).resolve().is_relative_to(dest.resolve()),
                "备份文件路径无效。",
            )
        z.extractall(dest)
    expected = json.loads((dest / "manifest.json").read_text("utf-8"))
    with psycopg.connect(**(cfg | {"dbname": "postgres"}), autocommit=True) as pg:
        pg.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    command(
        "pg_restore",
        name,
        "--exit-on-error",
        "--no-owner",
        "--no-acl",
        dest / "database.dump",
    )
    with psycopg.connect(**(cfg | {"dbname": name})) as pg:
        actual = inspect(pg)
        require(expected == actual, "恢复后的数据库清单不一致。")
        for file, sha in actual["files"].items():
            require(
                digest((dest / "originals" / file).read_bytes()) == sha,
                "恢复后的原文摘要不一致。",
            )
        sample = pg.execute(
            "SELECT count(*) FROM knowledge_chunk k JOIN document_version v USING(version_id) WHERE v.state='PUBLISHED' AND length(k.locator)>0 AND k.search_vector<>''::tsvector"
        ).fetchone()[0]
        pg.execute("SELECT count(*) FROM user_role JOIN role USING(role_id)").fetchone()
    return {
        "database": name,
        "directory": str(dest),
        "counts": actual["counts"],
        "files_verified": len(actual["files"]),
        "indexed_chunks": sample,
        "migration": actual["migration"],
    }


def rebuild():
    with engine(True).begin() as db:
        run(db, "SELECT pg_advisory_xact_lock(673101)")
        rows = many(
            db,
            "SELECT k.chunk_id,k.locator,k.content,d.title FROM knowledge_chunk k JOIN document_version v USING(version_id) JOIN knowledge_document d USING(document_id)",
        )
        update = text(
            "UPDATE knowledge_chunk SET search_vector=to_tsvector('simple',:s) WHERE chunk_id=:i"
        )
        if rows:
            db.execute(
                update,
                [
                    {
                        "i": r["chunk_id"],
                        "s": " ".join(
                            tokens(r["title"] + " " + r["locator"] + " " + r["content"])
                        ),
                    }
                    for r in rows
                ],
            )
        return len(rows)


def housekeeping():
    from app.knowledge import transition

    with engine(True).begin() as db:
        for v in many(
            db,
            "SELECT version_id,revision,reviewer_id FROM document_version WHERE state='PUBLISHED' AND valid_to<:d",
            d=today(),
        ):
            transition(
                db,
                v["reviewer_id"],
                v["version_id"],
                "withdraw",
                {"revision": v["revision"], "reason": "适用期已结束"},
            )
            audit(db, None, "知识版本到期", v["version_id"])
        run(
            db,
            "DELETE FROM message m USING conversation c WHERE m.session_id=c.session_id AND c.deleted_at<now()-interval '180 days'",
        )
        run(db, "DELETE FROM audit_log WHERE occurred_at<now()-interval '180 days'")
        run(db, "DELETE FROM auth_session WHERE expires_at<now()-interval '1 day'")
        for g in many(
            db,
            "SELECT * FROM request_guard WHERE state='RUNNING' AND deadline_at<now()-interval '5 seconds' FOR UPDATE SKIP LOCKED",
        ):
            info = {
                "status": "SERVICE_ERROR",
                "reason_code": "REQUEST_INTERRUPTED",
                "answer": "请求中断，请重新提问。",
                "citations": [],
            }
            run(
                db,
                "UPDATE request_guard SET state='DONE',result_ref=CAST(:j AS jsonb) WHERE request_id=:k",
                j=json_text(info),
                k=g["request_id"],
            )
            run(
                db,
                """INSERT INTO query_event(request_id,session_id,outcome,reason_code,latency_ms,topic)
                VALUES(:k,:s,'SERVICE_ERROR','REQUEST_INTERRUPTED',0,'未分类') ON CONFLICT DO NOTHING""",
                k=g["request_id"],
                s=g["session_id"],
            )
            audit(
                db,
                g["actor_id"],
                "问答中断",
                g["session_id"],
                "FAILURE",
                "REQUEST_INTERRUPTED",
                g["request_id"],
            )


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("action", choices=("backup", "restore", "rebuild", "clean"))
    p.add_argument("archive", nargs="?")
    args = p.parse_args()
    if args.action == "backup":
        print(backup())
    elif args.action == "restore":
        print(json_text(restore_check(Path(args.archive))))
    elif args.action == "rebuild":
        print(rebuild())
    else:
        housekeeping()
