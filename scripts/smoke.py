"""Exercise the running application using its local reader account, without logging a credential."""

import json
import os
from pathlib import Path
from uuid import uuid4
import httpx
from app.core import STATE

if __name__ == "__main__":
    line = next(
        s
        for s in (STATE / "演示账号.txt").read_text("utf-8").splitlines()
        if s.split()[1] == "reader"
    )
    _, user, password = line.split()
    base = "http://127.0.0.1:" + os.environ.get("DANGJIAN_PORT", "8768") + "/api/v1"
    with httpx.Client(base_url=base, trust_env=False, timeout=75) as client:

        def call(method, path, body=None):
            res = client.request(
                method, path, json=body, headers={"Idempotency-Key": str(uuid4())}
            )
            if res.status_code >= 400:
                raise RuntimeError(str(res.status_code) + " " + res.json()["message"])
            return res.json()["data"]

        me = call("POST", "/auth/login", {"username": user, "password": password})
        client.headers["X-CSRF-Token"] = me["csrf_token"]
        docs = call("GET", "/knowledge")
        sess = call("POST", "/sessions", {"title": "党章原文核验"})
        answer = call(
            "POST",
            "/sessions/" + sess["session_id"] + "/questions",
            {"question": "预备党员的预备期为多长时间？"},
        )
        if not answer["citations"]:
            raise RuntimeError(answer["status"] + " " + answer["reason_code"])
        ref = call("GET", "/citations/" + answer["citations"][0]["citation_id"])
        file = client.get(
            "/versions/" + ref["version_id"] + "/file",
            params={"citation_id": ref["citation_id"]},
        )
        file.raise_for_status()
        assert file.content.startswith(b"%PDF") and "一年" in answer["answer"]
        print(
            json.dumps(
                {
                    "source": ref["source"]["title"],
                    "documents": docs["total"],
                    "state": answer["status"],
                    "location": ref["locator"],
                    "quote": ref["snippet"],
                    "pdf_bytes": len(file.content),
                },
                ensure_ascii=False,
            )
        )
