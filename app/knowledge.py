import io
import os
import re
import unicodedata
import zipfile
from datetime import date
from pathlib import Path
from uuid import uuid4

import jieba
from docx import Document
from pypdf import PdfReader
from sqlalchemy import event
from sqlalchemy.engine import Engine
from .core import *

TOKEN = re.compile(r"[a-z0-9]+|[\u3400-\u9fff]+", re.I)
ARTICLE = re.compile(r"第[零〇一二三四五六七八九十百千万\d]+[条章节款项]")
STOP = set(
    "的 了 和 及 与 为 是 在 有 由 或 对 请 这 那 它 我 我们 一份 一条 怎么 怎样 如何 哪些 哪个 什么 多少 时候 什么时候 是否 能否 可以 需要 请问 请列出 根据 这次 每 该 等 中 后 里 时 还 再 想 要 吗 呢 么 来 也 按 把 得 从 和谁 哪天 几点 哪一 第 规定 写明 样例 样稿 分别 能 所 哪 什么样 应该 那么 询问".split()
)
ALIASES = {
    "多长时间": "期限",
    "多久": "期限",
    "多少天": "期限",
    "几天": "期限",
    "时限": "期限",
    "多长": "期限",
    "提交期限": "提交 期限",
    "开始接受": "开始",
    "截至": "截止",
    "哪儿": "地点",
    "在哪里": "地点",
    "人数上限": "名额",
    "谁可以参加": "参加对象",
    "适用于谁": "参加对象",
    "怎样定义": "定义",
    "怎么查": "检索",
    "党章": "中国共产党章程",
    "预备期": "预备期",
    "入党条件": "申请入党",
    "需要满足什么条件": "条件",
}
STOP.update(("给出", "时间", "怎么办", "列举", "列出"))
ALIASES.update({"截止": "结束", "截至": "结束", "满员": "额满"})
jieba.setLogLevel(40)
jieba.add_word("额满")


def normal(value):
    value = unicodedata.normalize("NFKC", value).lower()
    for term, replacement in ALIASES.items():
        if term != replacement:
            value = value.replace(term, replacement)
    return value


def tokens(value):
    raw = normal(value)
    words = [
        w
        for seg in TOKEN.findall(raw)
        for w in jieba.cut_for_search(seg)
        if w and w not in STOP
    ]
    return list(dict.fromkeys(words + ARTICLE.findall(raw)))


def sentences(value):
    return [
        x.strip()
        for x in re.findall(
            r"[^。！？!?\n]+(?:[。！？!?]+[”’」』]?|$)|[^。！？!?\n]+", value
        )
        if x.strip()
    ]


def windows(text, limit=380):
    current = []
    for sentence in sentences(text):
        if len(sentence) > limit * 2:
            units = [sentence[i : i + limit] for i in range(0, len(sentence), limit)]
        else:
            units = [sentence]
        for unit in units:
            if current and sum(map(len, current)) + len(unit) > limit:
                yield "".join(current)
                current = (
                    current[-1:]
                    if len(current) > 1 and len(current[-1]) + len(unit) <= limit
                    else []
                )
            current.append(unit)
    if current:
        yield "".join(current)


def check_docx(raw):
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        infos = archive.infolist()
        require(
            len(infos) <= 2500 and sum(x.file_size for x in infos) <= 80 * 1024**2,
            "Word 文件解压后过大。",
        )
        require(
            "word/document.xml" in archive.namelist()
            and "[Content_Types].xml" in archive.namelist(),
            "文件内容与 DOCX 格式不符。",
        )


def validate_file(filename, raw):
    kind = Path(filename or "").suffix.lstrip(".").upper()
    require(
        kind in ("PDF", "DOCX", "TXT"),
        "支持文本 PDF、DOCX 和 UTF-8 TXT。",
        415,
        "UNSUPPORTED_FILE",
    )
    require(0 < len(raw), "文件为空。")
    require(len(raw) <= MAX_BYTES, "文件超过 20 MB。", 413, "FILE_TOO_LARGE")
    try:
        if kind == "PDF":
            require(
                raw.startswith(b"%PDF-"),
                "文件内容与 PDF 格式不符。",
                415,
                "UNSUPPORTED_FILE",
            )
        elif kind == "DOCX":
            check_docx(raw)
        else:
            require("\x00" not in raw.decode("utf-8-sig"), "TXT 必须是 UTF-8 纯文本。")
    except (UnicodeError, zipfile.BadZipFile):
        raise Problem("文件格式或文字编码无效。", 415, "UNSUPPORTED_FILE") from None
    return kind


def parse(raw, kind):
    if kind == "PDF":
        doc = PdfReader(io.BytesIO(raw))
        require(not doc.is_encrypted, "加密 PDF 无法解析，请提供可读取原文。")
        for i, p in enumerate(doc.pages, 1):
            content = p.extract_text() or ""
            lines = [x.strip() for x in content.splitlines() if x.strip()]
            if lines and unicodedata.normalize("NFKC", lines[-1]).isdigit():
                lines.pop()
            page_text = re.sub(
                r"(?<=[\u3400-\u9fff，。；：、？！）])[\s\u3000]+(?=[\u3400-\u9fff，。；：、？！（])",
                "",
                "".join(lines),
            )
            for piece in windows(page_text):
                yield f"第 {i} 页", piece
    elif kind == "DOCX":
        doc = Document(io.BytesIO(raw))
        heading = ""
        n = 0
        for paragraph in doc.paragraphs:
            content = paragraph.text.strip()
            if not content:
                continue
            if paragraph.style.name.startswith(("Heading", "标题", "Title")):
                heading = content[:100]
                n = 0
                continue
            n += 1
            for piece in windows(content):
                yield (heading + " · " if heading else "") + f"第 {n} 段", piece
        for i, table in enumerate(doc.tables, 1):
            for j, row in enumerate(table.rows, 1):
                for piece in windows(" ".join(c.text for c in row.cells)):
                    yield f"表 {i} 第 {j} 行", piece
    else:
        for i, line in enumerate(raw.decode("utf-8-sig").splitlines(), 1):
            for piece in windows(line.strip()):
                yield f"第 {i} 行", piece


@event.listens_for(Engine, "rollback")
def clean_files(db):
    for path in db.info.pop("new_originals", []):
        path.unlink(missing_ok=True)


@event.listens_for(Engine, "commit")
def confirm_files(db):
    db.info.pop("new_originals", None)


def file_for(key):
    path = (FILES / key).resolve()
    require(
        path.is_relative_to(FILES) and path != FILES, "原文路径无效。", 404, "NOT_FOUND"
    )
    return path


def date_value(value):
    if not value:
        return None
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        raise Problem("日期格式应为 YYYY-MM-DD。") from None


def add_version(db, user, filename, raw, data):
    run(db, "SELECT pg_advisory_xact_lock_shared(673101)")
    kind = validate_file(filename, raw)
    title = field(data, "title", 200)
    source = field(data, "source_name", 200)
    url = field(data, "source_url", 2048, required=False) or None
    require(
        not url or re.fullmatch(r"https?://[^\s]+", url),
        "来源网址须使用 HTTP 或 HTTPS。",
    )
    start, end = date_value(data.get("valid_from")), date_value(data.get("valid_to"))
    require(not (start and end) or end >= start, "适用结束日期早于开始日期。")
    category = uid(data.get("category_id"))
    found(one(db, "SELECT 1 FROM category WHERE category_id=:i", i=category))
    sha = digest(raw)
    if data.get("document_id"):
        did = uid(data["document_id"])
        found(
            one(
                db,
                "SELECT 1 FROM knowledge_document WHERE document_id=:i FOR UPDATE",
                i=did,
            )
        )
        old = one(
            db,
            """SELECT v.document_id,v.version_id,v.version_no,j.job_id FROM document_version v JOIN ingest_job j USING(version_id)
            WHERE v.document_id=:i AND v.sha256=:h""",
            i=did,
            h=sha,
        )
        if old:
            return old | {"duplicate": True}
    else:
        did = str(uuid4())
        run(
            db,
            "INSERT INTO knowledge_document(document_id,category_id,title,source_name,source_url) VALUES(:d,:c,:t,:s,:url)",
            d=did,
            c=category,
            t=title,
            s=source,
            url=url,
        )
    version = str(uuid4())
    number = run(
        db,
        "SELECT coalesce(max(version_no),0)+1 FROM document_version WHERE document_id=:d",
        d=did,
    ).scalar_one()
    key = f"{did}/{version}.{kind.lower()}"
    dest = file_for(key)
    dest.parent.mkdir(parents=True, exist_ok=True)
    temp = dest.with_suffix(".tmp")
    try:
        temp.write_bytes(raw)
        os.replace(temp, dest)
        db.info.setdefault("new_originals", []).append(dest)
    finally:
        temp.unlink(missing_ok=True)
    run(
        db,
        """INSERT INTO document_version(version_id,document_id,version_no,file_type,file_path,sha256,size_bytes,
        state,valid_from,valid_to,uploader_id) VALUES(:v,:d,:n,:k,:p,:h,:sz,'UPLOADED',:a,:b,:u)""",
        v=version,
        d=did,
        n=number,
        k=kind,
        p=key,
        h=sha,
        sz=len(raw),
        a=start,
        b=end,
        u=user,
    )
    job = one(
        db, "INSERT INTO ingest_job(version_id) VALUES(:v) RETURNING job_id", v=version
    )
    return {
        "document_id": did,
        "version_id": version,
        "version_no": number,
        **job,
        "state": "UPLOADED",
    }


def work_one():
    with engine().begin() as db:
        job = one(
            db,
            """SELECT j.*,v.file_path,v.file_type,v.sha256,d.title FROM ingest_job j JOIN document_version v
            USING(version_id) JOIN knowledge_document d USING(document_id) WHERE j.state='QUEUED'
            OR (j.state='RUNNING' AND j.lease_until<clock_timestamp()) ORDER BY j.updated_at LIMIT 1 FOR UPDATE OF j SKIP LOCKED""",
        )
        if not job:
            return False
        lease = str(uuid4())
        run(
            db,
            """UPDATE ingest_job SET state='RUNNING',stage='解析原文',lease_token=:lease,
            lease_until=now()+interval '10 minutes',attempt=attempt+1,updated_at=now() WHERE job_id=:j""",
            lease=lease,
            j=job["job_id"],
        )
        run(
            db,
            "UPDATE document_version SET state='PROCESSING' WHERE version_id=:v",
            v=job["version_id"],
        )
    try:
        raw = file_for(job["file_path"]).read_bytes()
        require(digest(raw) == job["sha256"], "原文摘要与登记值不同。")
        iterator = parse(raw, job["file_type"])
        with engine().begin() as db:
            run(db, "SELECT pg_advisory_xact_lock_shared(673101)")
            current = one(
                db,
                "SELECT lease_token FROM ingest_job WHERE job_id=:j FOR UPDATE",
                j=job["job_id"],
            )
            if str(current["lease_token"]) != lease:
                return True
            run(
                db,
                "DELETE FROM knowledge_chunk WHERE version_id=:v",
                v=job["version_id"],
            )
            count = 0
            batch = []
            statement = text("""INSERT INTO knowledge_chunk(version_id,ordinal,locator,content,search_vector)
                VALUES(:v,:n,:loc,:body,to_tsvector('simple',:search))""")
            for count, (loc, body) in enumerate(iterator, 1):
                batch.append(
                    dict(
                        v=job["version_id"],
                        n=count,
                        loc=loc,
                        body=body,
                        search=" ".join(tokens(job["title"] + " " + loc + " " + body)),
                    )
                )
                if len(batch) >= 250:
                    db.execute(statement, batch)
                    batch = []
            if batch:
                db.execute(statement, batch)
            require(count, "未提取到文字。扫描件须先转换为文本型文件。")
            run(
                db,
                """UPDATE document_version SET state='REVIEW_PENDING',index_state='READY',parser_version='text-1',
                index_version='jieba-fts-1',revision=revision+1 WHERE version_id=:v""",
                v=job["version_id"],
            )
            run(
                db,
                """UPDATE ingest_job SET state='SUCCEEDED',stage='等待审核',reason_code=NULL,lease_token=NULL,
                lease_until=NULL,updated_at=now() WHERE job_id=:j""",
                j=job["job_id"],
            )
    except Exception as exc:
        reason = (
            exc.message if isinstance(exc, Problem) else "原文损坏或解析、索引失败。"
        )
        with engine().begin() as db:
            changed = run(
                db,
                """UPDATE ingest_job SET state='FAILED',stage='处理失败',reason_code=:why,lease_token=NULL,
                lease_until=NULL,updated_at=now() WHERE job_id=:j AND lease_token=:lease""",
                why=reason,
                j=job["job_id"],
                lease=lease,
            )
            if changed.rowcount:
                run(
                    db,
                    "UPDATE document_version SET state='FAILED',index_state='FAILED',revision=revision+1 WHERE version_id=:v",
                    v=job["version_id"],
                )
    return True


def transition(db, user, vid, action, data):
    run(db, "SELECT pg_advisory_xact_lock_shared(673101)")
    did = found(
        one(db, "SELECT document_id FROM document_version WHERE version_id=:v", v=vid)
    )["document_id"]
    doc = found(
        one(
            db,
            "SELECT * FROM knowledge_document WHERE document_id=:d FOR UPDATE",
            d=did,
        )
    )
    v = found(
        one(db, "SELECT * FROM document_version WHERE version_id=:v FOR UPDATE", v=vid)
    )
    require(
        v["revision"] == data.get("revision"),
        "记录已变化，请刷新后重试。",
        409,
        "VERSION_CONFLICT",
    )
    note = field(data, "review_note" if action == "review" else "reason", 1000)
    new_state = {
        "withdraw": "WITHDRAWN",
        "reopen": "REVIEW_PENDING",
        "archive": "ARCHIVED",
    }.get(action)
    if action == "review":
        require(
            v["state"] == "REVIEW_PENDING" and v["index_state"] == "READY",
            "仅可审核索引就绪的待审版本。",
            409,
            "VERSION_CONFLICT",
        )
        require(
            "base_current_version_id" in data
            and str(data["base_current_version_id"]) == str(doc["current_version_id"]),
            "当前发布版本已变化，请刷新。",
            409,
            "VERSION_CONFLICT",
        )
        decision = data.get("decision")
        require(decision in ("PUBLISH", "REJECT"), "请选择审核结果。")
        new_state = "PUBLISHED" if decision == "PUBLISH" else "REJECTED"
        if decision == "PUBLISH":
            require(
                (not v["valid_from"] or v["valid_from"] <= today())
                and (not v["valid_to"] or v["valid_to"] >= today()),
                "此版本不在适用期。",
            )
            roles = ids_for_roles(db, data.get("role_ids"))
            run(db, "DELETE FROM version_role WHERE version_id=:v", v=vid)
            for role_id in roles:
                run(db, "INSERT INTO version_role VALUES(:v,:r)", v=vid, r=role_id)
            run(
                db,
                "UPDATE document_version SET state='ARCHIVED',revision=revision+1 WHERE document_id=:d AND state='PUBLISHED'",
                d=did,
            )
            run(
                db,
                "UPDATE knowledge_document SET current_version_id=:v WHERE document_id=:d",
                v=vid,
                d=did,
            )
    else:
        valid = {
            "withdraw": {"PUBLISHED"},
            "reopen": {"WITHDRAWN"},
            "archive": {"REJECTED", "WITHDRAWN"},
        }
        require(
            action in valid and v["state"] in valid[action],
            "当前状态不允许该操作。",
            409,
            "VERSION_CONFLICT",
        )
        if action == "withdraw":
            run(
                db,
                "UPDATE knowledge_document SET current_version_id=NULL WHERE document_id=:d AND current_version_id=:v",
                d=did,
                v=vid,
            )
    return one(
        db,
        """UPDATE document_version SET state=:s,reviewer_id=:u,review_note=:n,reviewed_at=now(),
        published_at=CASE WHEN :s='PUBLISHED' THEN now() ELSE published_at END,revision=revision+1
        WHERE version_id=:v RETURNING version_id,state,revision""",
        s=new_state,
        u=user,
        n=note,
        v=vid,
    )


VISIBLE = """v.version_id=d.current_version_id AND v.state='PUBLISHED' AND v.index_state='READY'
 AND (v.valid_from IS NULL OR v.valid_from<=:day) AND (v.valid_to IS NULL OR v.valid_to>=:day)
 AND EXISTS (SELECT 1 FROM version_role vr WHERE vr.version_id=v.version_id AND vr.role_id=:role)"""


def file_granted(db, vid, role):
    return one(
        db,
        "SELECT 1 FROM version_role WHERE version_id=:v AND role_id=:r",
        v=vid,
        r=role,
    )
