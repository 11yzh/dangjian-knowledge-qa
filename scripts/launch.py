import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request

from app.core import ROOT, STATE
from scripts.localdb import ensure
from scripts.seed import setup


def launch():
    port = int(os.environ.get("DANGJIAN_PORT", "8768"))
    base = f"http://127.0.0.1:{port}"

    def healthy():
        try:
            with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(
                base + "/health", timeout=2
            ) as r:
                return json.load(r).get("project") == "dangjian-knowledge-qa"
        except (OSError, ValueError):
            return False

    ensure()
    setup()
    jobs = [("worker", ["-m", "app.worker"])]
    if not healthy():
        jobs.append(
            (
                "web",
                [
                    "-m",
                    "uvicorn",
                    "app.main:app",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(port),
                    "--no-access-log",
                ],
            )
        )
    for name, args in jobs:
        with (STATE / (name + ".log")).open("ab") as log:
            options = (
                {
                    "creationflags": subprocess.CREATE_NO_WINDOW
                    | subprocess.CREATE_NEW_PROCESS_GROUP
                }
                if os.name == "nt"
                else {"start_new_session": True}
            )
            subprocess.Popen(
                [sys.executable, *args],
                cwd=ROOT,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                **options,
            )
    for attempt in range(40):
        if healthy():
            print(base)
            return
        time.sleep(0.35)
    raise RuntimeError("应用未能启动，请检查 .local/web.log 与端口。")


if __name__ == "__main__":
    launch()
