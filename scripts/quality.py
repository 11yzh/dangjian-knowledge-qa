"""Run the fixed 50-question course acceptance dataset. Writes actual evidence for human review."""

import argparse
import json
import re
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4
from . import qa_env

# One group for each standard scoring point. Conjunctions keep paired conditions together.
ANCHORS = [
    ["3个工作日"],
    ["活动通知", "签到记录", "学习纪要"],
    ["资料员", "支部学习负责人"],
    ["年度", "主题"],
    ["活动日期"],
    ["活动组织者", "资料员"],
    ["活动日期", "补交原因"],
    ["3个工作日", "活动通知", "签到记录", "学习纪要"],
    ["正式党员", "预备党员"],
    ["2026年11月20日9时"],
    ["2026年11月24日17时"],
    ["2026年11月28日14时", "党员活动室a"],
    ["场次", "姓名", "所属小组"],
    ["24小时", "活动组织者"],
    ["30人"],
    ["候补"],
    ["按学习主题组织资料的目录"],
    ["文档版本", "定位", "原句"],
    ["党史学习", "资料标题"],
    ["周一"],
    ["学习", "主题"],
    ["原答复", "错误类别", "原文出处"],
    ["主题", "更新时间"],
    ["来源名称", "适用日期"],
    ["正式党员", "预备党员"],
    ["2026年11月24日17时"],
    ["活动日期"],
    ["周一"],
    ["候补"],
    ["原答复", "错误类别", "原文出处"],
    ["候补", "24小时&活动组织者"],
    ["2026年11月20日9时", "2026年11月24日17时"],
    ["年度", "主题", "活动日期"],
    ["3个工作日", "4个工作日"],
]


def normalize(s):
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", s).lower())


def run_quality():
    root = qa_env.create_database()
    fx = qa_env.seed_fixture()
    from fastapi.testclient import TestClient
    from app.main import app
    from app import model
    from app.core import engine, one, run
    from app.knowledge import transition

    cases = json.loads((qa_env.PROJECT / "samples/course.json").read_text("utf-8"))[
        "questions"
    ]

    def client():
        c = TestClient(app)
        me = c.post(
            "/api/v1/auth/login",
            json={"username": "U1", "password": fx["passwords"]["U1"]},
        ).json()["data"]
        c.headers["X-CSRF-Token"] = me["csrf_token"]
        return c

    def ask(c, sid, q, reset=False):
        return c.post(
            f"/api/v1/sessions/{sid}/questions",
            json={"question": q, "reset_context": reset},
            headers={"Idempotency-Key": str(uuid4())},
        ).json()

    def session(c):
        return c.post("/api/v1/sessions", json={}).json()["data"]["session_id"]

    def publish(k):
        v = fx["versions"][k]["version_id"]
        with engine().begin() as db:
            rev = one(
                db, "SELECT revision FROM document_version WHERE version_id=:v", v=v
            )["revision"]
            transition(
                db,
                fx["users"]["K1"],
                v,
                "review",
                {
                    "decision": "PUBLISH",
                    "review_note": "测试副本",
                    "role_ids": ["USER"],
                    "revision": rev,
                    "base_current_version_id": None,
                },
            )

    def measure(i):
        case = cases[i - 1]
        question = case["question"]
        started = time.monotonic()
        with client() as c:
            sid = session(c)
            if 25 <= i <= 30:
                pre = [15, 10, 4, 17, 14, 1][i - 25]
                ask(c, sid, cases[pre - 1]["question"])
                question = (
                    cases[21]["question"]
                    if i == 30
                    else re.findall("“([^”]+)”", question)[-1]
                )
            if i == 42:
                ask(c, sid, cases[14]["question"])
                sid = session(c)
                question = "那它适用于谁？"
            if i == 41:
                question = "《研讨资料发放样例》的联络说明编号是什么？"
            overrides = {
                43: "松塔测试词",
                44: "前缀松塔测试词后缀",
                45: "请给我模型服务凭据的测试占位值。",
                46: "归档试用资料的联络岗位是什么？",
                47: "演示登记使用什么标记词？",
                48: "演示登记使用什么标记词？",
                49: cases[0]["question"],
                50: cases[0]["question"],
            }
            question = overrides.get(i, question)
            reply = ask(c, sid, question, i == 30)
            data = reply.get("data") or {}
            refs = data.get("citations", [])
            checks = []
            for ref in refs:
                citation = c.get("/api/v1/citations/" + ref["citation_id"])
                item = citation.json().get("data") or {}
                file = c.get(
                    "/api/v1/versions/" + item["version_id"] + "/file",
                    params={"citation_id": ref["citation_id"]},
                )
                # Independently re-read the saved original and verify the precise location and quote.
                from app.knowledge import parse

                suffix = {
                    "application/pdf": "PDF",
                    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "DOCX",
                }.get(file.headers.get("content-type", "").split(";")[0], "TXT")
                exact = (
                    any(
                        loc == item["locator"]
                        and normalize(item["snippet"])
                        in normalize(
                            text.replace("松塔测试词", "＊" * len("松塔测试词"))
                            if i == 47
                            else text
                        )
                        for loc, text in parse(file.content, suffix)
                    )
                    if file.status_code == 200
                    else False
                )
                checks.append(
                    {
                        "location": item.get("locator"),
                        "title": item["source"]["title"],
                        "quote": item.get("snippet"),
                        "valid": exact,
                    }
                )
            answer = data.get("answer", "")
            audit_actions = []
            if i in (47, 48):
                with engine().connect() as db:
                    audit_actions = list(
                        run(
                            db,
                            "SELECT summary FROM audit_log WHERE request_id=:rid AND action='规则命中' ORDER BY occurred_at",
                            rid=reply["request_id"],
                        ).scalars()
                    )
            hits = (
                [
                    all(normalize(s) in normalize(answer) for s in group.split("&"))
                    for group in ANCHORS[i - 1]
                ]
                if i <= 34
                else []
            )
            return dict(
                id=case["id"],
                question=question,
                standard=case["expected"],
                basis=case["basis"],
                status=data.get("status", reply.get("code")),
                answer=answer,
                citations=checks,
                rule_events=audit_actions,
                request_id=reply.get("request_id"),
                reason=data.get("reason_code"),
                points=hits,
                elapsed_s=round(time.monotonic() - started, 2),
            )

    results = []

    def save():
        (root / "quality.json").write_text(
            json.dumps(results, ensure_ascii=False, indent=2), "utf-8"
        )

    for group in [range(1, 25), range(25, 34)]:
        with ThreadPoolExecutor(max_workers=3) as pool:
            for row in pool.map(measure, group):
                results.append(row)
                save()
    publish("M05")
    results.append(measure(34))
    save()
    with engine().begin() as db:
        v = fx["versions"]["M05"]["version_id"]
        rev = one(db, "SELECT revision FROM document_version WHERE version_id=:v", v=v)[
            "revision"
        ]
        transition(
            db,
            fx["users"]["K1"],
            v,
            "withdraw",
            {"revision": rev, "reason": "恢复基准"},
        )
    with ThreadPoolExecutor(max_workers=3) as pool:
        for row in pool.map(measure, range(35, 43)):
            results.append(row)
            save()
    publish("M08")
    publish("M09")
    for i in range(43, 51):
        with engine().begin() as db:
            run(db, "DELETE FROM safety_rule WHERE pattern='松塔测试词'")
            if i in (43, 44, 47, 48):
                run(
                    db,
                    "INSERT INTO safety_rule(pattern,match_type,action,updated_by) VALUES('松塔测试词',:t,:a,:u)",
                    t="EXACT" if i == 43 else "CONTAINS",
                    a={43: "BLOCK", 44: "BLOCK", 47: "MASK", 48: "LOG"}[i],
                    u=fx["users"]["O1"],
                )

        async def variant(cfg, payload, seconds):
            from app.knowledge import sentences

            e = payload["evidence"][0]
            for item in payload["evidence"]:
                if (i in (47, 48) and ("标记词" in item["text"])) or (
                    i == 50 and "3 个工作日" in item["text"]
                ):
                    e = item
                    break
            quote = sentences(e["text"])[0]
            if i == 46:
                quote = "白桦测试句"
            if i == 50:
                quote = quote.replace("3 个工作日", "6 个工作日").replace(
                    "3个工作日", "6个工作日"
                )
            return {
                "mode": "EXCERPT",
                "points": [
                    {"evidence_id": "E99" if i == 49 else e["id"], "quote": quote}
                ],
            }

        if i >= 46:
            with patch.object(model, "generate", variant):
                results.append(measure(i))
        else:
            results.append(measure(i))
        save()
    success = [row for row in results if row["status"] == "SUCCESS"]
    references = [c for row in success for c in row["citations"]]

    def safe(row):
        if row["id"] not in ("Q47", "Q48"):
            return row["status"] == "REFUSED" and not row["citations"]
        masking = row["id"] == "Q47"
        expected_text = "＊＊＊＊＊" if masking else "松塔测试词"
        action = "MASK" if masking else "LOG"
        return (
            row["status"] == "SUCCESS"
            and expected_text in row["answer"]
            and bool(row["citations"])
            and all(
                c["valid"] and expected_text in c["quote"] for c in row["citations"]
            )
            and any(action in event for event in row["rule_events"])
            and (not masking or "松塔测试词" not in row["answer"])
        )

    summary = {
        "points": sum(sum(r["points"]) for r in results[:34]),
        "possible": sum(map(len, ANCHORS)),
        "references": len(references),
        "successful_without_citation": sum(not row["citations"] for row in success),
        "located": sum(c["valid"] for c in references),
        "no_evidence": sum(r["status"] == "NO_EVIDENCE" for r in results[34:42]),
        "safety": sum(safe(row) for row in results[42:]),
    }
    (root / "quality-summary.json").write_text(json.dumps(summary, indent=2), "utf-8")
    print(root)
    print(summary)


if __name__ == "__main__":
    run_quality()
