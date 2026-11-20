"""Start the project-owned PostgreSQL cluster on loopback without installing a service."""

import json
import os
import secrets
import shutil
import subprocess
import hashlib
import tempfile
import zipfile
from pathlib import Path
from urllib.parse import urlsplit

import psycopg
from psycopg import sql
from alembic import command
from alembic.config import Config
from app.core import ROOT, STATE, engine

PORT = int(os.environ.get("DANGJIAN_PGPORT", "55439"))


def pg_home():
    if os.name != "nt":
        return STATE
    return Path(
        os.environ.get(
            "DANGJIAN_PG_HOME", Path(os.environ["LOCALAPPDATA"]) / "DangjianQA"
        )
    )


def pgtool(name):
    exe = name + (".exe" if os.name == "nt" else "")
    choices = [
        Path(os.environ.get("PGBIN", ".")) / exe,
        pg_home() / "pgsql" / "bin" / exe,
    ]
    installed = shutil.which(exe)
    if installed:
        choices.append(Path(installed))
    for path in choices:
        if path.is_file():
            return str(path.resolve())
    raise RuntimeError(f"缺少 {name}，请安装 PostgreSQL 15 或更新版本并设置 PGBIN。")


def dbcli(name, *args, **kw):
    return subprocess.run([pgtool(name), *map(str, args)], check=True, **kw)


def ensure():
    STATE.mkdir(parents=True, exist_ok=True)
    home = pg_home()
    home.mkdir(parents=True, exist_ok=True)
    archive = STATE / "postgres-binaries.zip"
    packaged = ROOT / ".runtime" / "pgsql"
    installed = home / "pgsql"
    if (
        os.name == "nt"
        and not (installed / "bin/postgres.exe").is_file()
        and (packaged.is_dir() or archive.is_file())
    ):
        # initdb expands its share directory into SQL. A multibyte checkout path can
        # be encoded using the Windows ANSI code page during UTF-8 initialization.
        with tempfile.TemporaryDirectory(prefix="pg-install-", dir=home) as staging:
            prepared = Path(staging) / "pgsql"
            if packaged.is_dir():
                shutil.copytree(packaged, prepared)
            else:
                with zipfile.ZipFile(archive) as z:
                    items = [
                        n
                        for n in z.namelist()
                        if n.startswith(("pgsql/bin/", "pgsql/lib/", "pgsql/share/"))
                    ]
                    z.extractall(staging, items)
            try:
                prepared.rename(installed)
            except FileExistsError:
                if not (installed / "bin/postgres.exe").is_file():
                    raise
    config = STATE / "database.json"
    if os.getenv("DATABASE_URL") and os.getenv("DANGJIAN_OWNER_URL"):
        command.upgrade(Config(str(ROOT / "alembic.ini")), "head")
        return
    if not config.exists():
        admin, app = secrets.token_hex(24), secrets.token_hex(24)
        urls = {
            key: f"postgresql+psycopg://{name}:{pw}@127.0.0.1:{PORT}/dangjian"
            for key, name, pw in [
                ("owner_url", "dq_owner", admin),
                ("app_url", "dq_app", app),
            ]
        }
        with os.fdopen(
            os.open(config, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600),
            "w",
            encoding="utf-8",
        ) as f:
            json.dump(urls, f)
    cfg = json.loads(config.read_text("utf-8"))
    owner = urlsplit(cfg["owner_url"])
    app = urlsplit(cfg["app_url"])
    data = home / ("cluster-" + hashlib.sha256(str(STATE).encode()).hexdigest()[:12])
    if not (data / "PG_VERSION").exists():
        pwd = home / (data.name + ".pw.tmp")
        try:
            pwd.write_text(owner.password + "\n", encoding="ascii")
            dbcli(
                "initdb",
                "-D",
                data,
                "-U",
                owner.username,
                "--pwfile",
                pwd,
                "-A",
                "scram-sha-256",
                "-E",
                "UTF8",
                "--locale=C",
            )
        finally:
            pwd.unlink(missing_ok=True)
    status = subprocess.run(
        [pgtool("pg_ctl"), "-D", str(data), "status"], stdout=subprocess.DEVNULL
    )
    if status.returncode:
        dbcli(
            "pg_ctl",
            "-D",
            data,
            "-l",
            home / (data.name + ".log"),
            "-o",
            f"-p {owner.port} -h 127.0.0.1",
            "-w",
            "start",
        )
    conn = dict(
        host="127.0.0.1", port=owner.port, user=owner.username, password=owner.password
    )
    with psycopg.connect(**conn, dbname="postgres", autocommit=True) as pg:
        if not pg.execute(
            "SELECT 1 FROM pg_roles WHERE rolname=%s", (app.username,)
        ).fetchone():
            pg.execute(
                sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                    sql.Identifier(app.username), sql.Literal(app.password)
                )
            )
        if not pg.execute(
            "SELECT 1 FROM pg_database WHERE datname='dangjian'"
        ).fetchone():
            pg.execute("CREATE DATABASE dangjian")
    command.upgrade(Config(str(ROOT / "alembic.ini")), "head")
    with psycopg.connect(**conn, dbname="dangjian") as pg:
        pg.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
        pg.execute("GRANT USAGE ON SCHEMA public TO dq_app")
        pg.execute(
            "GRANT SELECT,INSERT,UPDATE,DELETE ON ALL TABLES IN SCHEMA public TO dq_app"
        )
        pg.execute("REVOKE UPDATE,DELETE,TRUNCATE ON audit_log FROM dq_app")
        pg.execute(
            "REVOKE INSERT,UPDATE,DELETE,TRUNCATE ON role,alembic_version FROM dq_app"
        )
    print("PostgreSQL 已就绪。")


if __name__ == "__main__":
    ensure()
