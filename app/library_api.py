from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, HTMLResponse
from .auth import principal
from .core import *
from .knowledge import (
    VISIBLE,
    add_version,
    date_value,
    file_for,
    file_granted,
    transition,
)
from .reader import reading_page

routes = APIRouter()
K = (KNOWLEDGE,)
S = (ADMIN,)


def write(req, payload, roles, action, callback, status=200):
    with engine().begin() as db:
        u = principal(db, req, roles)
        out = mutation(db, req, u, action, payload, lambda: callback(db, u))
        return result(
            req, out, code="CREATED" if status == 201 else "OK", status=status
        )


@routes.post("/manage/versions")
async def upload(req: Request):
    with engine().begin() as db:
        principal(db, req, K)
    async with req.form(max_files=1, max_fields=16, max_part_size=MAX_BYTES) as form:
        file = form.get("file")
        require(hasattr(file, "read"), "请选择上传的文件。")
        raw = await file.read(MAX_BYTES + 1)
        filename = file.filename
        data = {k: v for k, v in form.items() if k != "file"}
    with engine().begin() as db:
        u = principal(db, req, K)
        output = mutation(
            db,
            req,
            u,
            "知识导入",
            data | {"digest": digest(raw), "filename": filename},
            lambda: add_version(db, u["user_id"], filename, raw, data),
        )
        return result(req, output, code="ACCEPTED", status=202)


VERSIONS = """SELECT v.version_id,v.document_id,v.version_no,d.title,d.category_id,c.name AS category,d.source_name,d.source_url,
 d.current_version_id,v.state,v.index_state,v.valid_from,v.valid_to,v.file_type,v.sha256,v.size_bytes,v.revision,
 v.review_note,v.created_at,v.reviewed_at,v.published_at,j.job_id,j.stage,j.reason_code AS job_reason
 FROM document_version v JOIN knowledge_document d USING(document_id) JOIN category c USING(category_id)
 JOIN ingest_job j USING(version_id)"""


@routes.get("/manage/versions")
def versions(req: Request):
    with engine().begin() as db:
        principal(db, req, K)
        predicates = ["TRUE"]
        params = {}
        for k, col in [("state", "v.state"), ("category_id", "d.category_id")]:
            if req.query_params.get(k):
                predicates.append(col + "=:" + k)
                params[k] = req.query_params[k]
        if req.query_params.get("q"):
            predicates.append("d.title ILIKE :q")
            params["q"] = "%" + req.query_params["q"] + "%"
        return result(
            req,
            page(
                db,
                VERSIONS + " WHERE " + " AND ".join(predicates),
                params,
                req,
                "v.created_at DESC,v.version_id",
            ),
        )


@routes.get("/manage/jobs/{jid}")
def job(req: Request, jid: str):
    with engine().begin() as db:
        principal(db, req, K)
        return result(
            req,
            found(
                one(
                    db,
                    "SELECT job_id,version_id,state,stage,attempt,reason_code,updated_at FROM ingest_job WHERE job_id=:j",
                    j=uid(jid),
                )
            ),
        )


@routes.post("/manage/jobs/{jid}/retry")
def retry(req: Request, jid: str, data: dict):
    def action(db, u):
        j = found(
            one(db, "SELECT * FROM ingest_job WHERE job_id=:j FOR UPDATE", j=uid(jid))
        )
        require(j["state"] == "FAILED", "仅可重试失败的任务。", 409, "VERSION_CONFLICT")
        run(
            db,
            "UPDATE ingest_job SET state='QUEUED',stage='等待重试',reason_code=NULL,updated_at=now() WHERE job_id=:j",
            j=jid,
        )
        run(
            db,
            "UPDATE document_version SET state='UPLOADED',index_state='PENDING',revision=revision+1 WHERE version_id=:v",
            v=j["version_id"],
        )
        return {"job_id": jid, "version_id": j["version_id"], "state": "QUEUED"}

    return write(req, {"jid": jid}, K, "重试导入", action)


@routes.get("/manage/versions/{vid}/chunks")
def chunks(req: Request, vid: str):
    with engine().begin() as db:
        principal(db, req, K)
        found(one(db, "SELECT 1 FROM document_version WHERE version_id=:v", v=uid(vid)))
        return result(
            req,
            page(
                db,
                "SELECT chunk_id,ordinal,locator,content FROM knowledge_chunk WHERE version_id=:v",
                {"v": vid},
                req,
                "ordinal",
            ),
        )


@routes.post("/manage/versions/{vid}/{action}")
def lifecycle(req: Request, vid: str, action: str, data: dict):
    require(
        action in ("review", "withdraw", "archive", "reopen"),
        "操作不存在。",
        404,
        "NOT_FOUND",
    )
    return write(
        req,
        data | {"vid": vid},
        K,
        "知识版本/" + action,
        lambda db, u: transition(db, u["user_id"], uid(vid), action, data),
    )


def catalogue(req, managed):
    with engine().begin() as db:
        u = principal(db, req, K if managed else None)
        q = """SELECT d.document_id,d.title,d.category_id,c.name AS category,d.source_name,d.source_url,
            v.version_id,v.version_no,v.file_type,v.valid_from,v.valid_to,v.state,
            coalesce((SELECT jsonb_agg(jsonb_build_object('tag_id',t.tag_id,'name',t.name) ORDER BY t.name)
            FROM document_tag dt JOIN tag t USING(tag_id) WHERE dt.document_id=d.document_id),'[]') AS tags
            FROM knowledge_document d JOIN category c USING(category_id) LEFT JOIN document_version v ON v.version_id=d.current_version_id"""
        params = {}
        if not managed:
            q += " WHERE " + VISIBLE
            params.update(role=u["current_role_id"], day=today())
        else:
            q += " WHERE TRUE"
        for key, field_name in [
            ("q", "d.title"),
            ("title", "d.title"),
            ("source", "d.source_name"),
            ("source_name", "d.source_name"),
        ]:
            if req.query_params.get(key):
                q += f" AND {field_name} ILIKE :{key}"
                params[key] = "%" + req.query_params[key] + "%"
        if req.query_params.get("category_id"):
            q += " AND d.category_id=:c"
            params["c"] = uid(req.query_params["category_id"])
        for i, tag in enumerate(
            req.query_params.getlist("tag_id") + req.query_params.getlist("tag_ids")
        ):
            for j, value in enumerate(tag.split(",")):
                name = f"tg{i}_{j}"
                q += f" AND EXISTS(SELECT 1 FROM document_tag WHERE document_id=d.document_id AND tag_id=:{name})"
                params[name] = uid(value)
        when = req.query_params.get("valid_on") or req.query_params.get("date")
        if when:
            q += " AND (v.valid_from IS NULL OR v.valid_from<=:atday) AND (v.valid_to IS NULL OR v.valid_to>=:atday)"
            params["atday"] = date_value(when)
        data = page(db, q, params, req, "d.title,d.document_id")
        return result(req, data)


@routes.get("/knowledge")
def knowledge(req: Request):
    return catalogue(req, False)


@routes.get("/manage/documents")
def documents(req: Request):
    return catalogue(req, True)


@routes.patch("/manage/documents/{did}")
def update_document(req: Request, did: str, data: dict):
    def action(db, u):
        found(
            one(
                db,
                "SELECT document_id FROM knowledge_document WHERE document_id=:d FOR UPDATE",
                d=uid(did),
            )
        )
        if "category_id" in data:
            cat = found(
                one(
                    db,
                    "SELECT category_id FROM category WHERE category_id=:c",
                    c=uid(data["category_id"]),
                )
            )
            run(
                db,
                "UPDATE knowledge_document SET category_id=:c WHERE document_id=:d",
                d=did,
                c=cat["category_id"],
            )
        if "tag_ids" in data:
            require(isinstance(data["tag_ids"], list), "标签格式错误。")
            run(db, "DELETE FROM document_tag WHERE document_id=:d", d=did)
            for tag in dict.fromkeys(data["tag_ids"]):
                found(one(db, "SELECT 1 FROM tag WHERE tag_id=:i", i=uid(tag)))
                run(db, "INSERT INTO document_tag VALUES(:d,:t)", d=did, t=tag)
        return {"document_id": did}

    return write(req, data | {"did": did}, K, "知识目录更新", action)


@routes.get("/knowledge/filters")
def filters(req: Request):
    with engine().begin() as db:
        u = principal(db, req)
        base = " FROM knowledge_document d JOIN document_version v USING(document_id) "
        params = dict(role=u["current_role_id"], day=today())
        cats = many(
            db,
            "SELECT DISTINCT c.*"
            + base
            + "JOIN category c USING(category_id) WHERE "
            + VISIBLE
            + " ORDER BY c.name",
            **params,
        )
        tags = many(
            db,
            "SELECT DISTINCT t.*"
            + base
            + "JOIN document_tag dt USING(document_id) JOIN tag t USING(tag_id) WHERE "
            + VISIBLE
            + " ORDER BY t.name",
            **params,
        )
        return result(req, {"categories": cats, "tags": tags})


@routes.get("/manage/categories")
@routes.get("/manage/tags")
def list_dict(req: Request):
    category = req.url.path.endswith("/categories")
    with engine().begin() as db:
        principal(db, req, K)
        return result(
            req,
            page(
                db,
                "SELECT * FROM " + ("category" if category else "tag"),
                {},
                req,
                "name",
            ),
        )


def dictionary(req, kind, item, data, method):
    is_cat = kind == "categories"
    table, col = ("category", "category_id") if is_cat else ("tag", "tag_id")

    def action(db, u):
        run(db, "SELECT pg_advisory_xact_lock(673102)")
        if item:
            found(
                one(db, f"SELECT * FROM {table} WHERE {col}=:i FOR UPDATE", i=uid(item))
            )
        if method == "DELETE":
            if is_cat:
                occupied = one(
                    db,
                    "SELECT 1 FROM category WHERE parent_id=:i UNION ALL SELECT 1 FROM knowledge_document WHERE category_id=:i UNION ALL SELECT 1 FROM faq WHERE category_id=:i",
                    i=item,
                )
                require(
                    not occupied,
                    "分类仍有子分类或被资料引用。",
                    409,
                    "VERSION_CONFLICT",
                )
            run(db, f"DELETE FROM {table} WHERE {col}=:i", i=item)
            return {col: item}
        name = field(data, "name", 64 if is_cat else 32)
        if not is_cat:
            return one(
                db,
                "INSERT INTO tag(name) VALUES(:n) RETURNING *"
                if not item
                else "UPDATE tag SET name=:n WHERE tag_id=:i RETURNING *",
                n=name,
                i=item,
            )
        parent = data.get("parent_id") or None
        if parent:
            found(one(db, "SELECT 1 FROM category WHERE category_id=:p", p=uid(parent)))
            cycle = one(
                db,
                """WITH RECURSIVE parents AS (SELECT * FROM category WHERE category_id=:p UNION ALL
                SELECT c.* FROM category c JOIN parents x ON c.category_id=x.parent_id)
                SELECT 1 FROM parents WHERE category_id=CAST(:i AS uuid)""",
                p=parent,
                i=item,
            )
            require(not cycle, "分类不能移到自身或后代节点下。")
        return one(
            db,
            "INSERT INTO category(name,parent_id) VALUES(:n,:p) RETURNING *"
            if not item
            else "UPDATE category SET name=:n,parent_id=:p WHERE category_id=:i RETURNING *",
            n=name,
            p=parent,
            i=item,
        )

    return write(req, {"id": item, **data}, K, method + "/" + kind, action)


@routes.post("/manage/categories")
@routes.post("/manage/tags")
def new_dict(req: Request, data: dict):
    return dictionary(req, req.url.path.split("/")[-1], None, data, "POST")


@routes.patch("/manage/categories/{item}")
@routes.patch("/manage/tags/{item}")
def edit_dict(req: Request, item: str, data: dict):
    return dictionary(req, req.url.path.split("/")[-2], item, data, "PATCH")


@routes.delete("/manage/categories/{item}")
@routes.delete("/manage/tags/{item}")
def del_dict(req: Request, item: str):
    return dictionary(req, req.url.path.split("/")[-2], item, {}, "DELETE")


def citation_record(db, u, cid):
    return found(
        one(
            db,
            """SELECT c.citation_id,c.snippet_snapshot AS snippet,c.locator_snapshot AS locator,
        c.source_snapshot AS source,v.state AS version_state,k.version_id FROM citation c
        JOIN message m USING(message_id) JOIN conversation s USING(session_id) JOIN knowledge_chunk k USING(chunk_id)
        JOIN document_version v USING(version_id) JOIN version_role vr ON vr.version_id=v.version_id
        WHERE c.citation_id=:id AND s.owner_id=:u AND s.status='ACTIVE' AND vr.role_id=:r""",
            id=uid(cid),
            u=u["user_id"],
            r=u["current_role_id"],
        )
    )


@routes.get("/citations/{cid}")
def cite(req: Request, cid: str):
    with engine().begin() as db:
        u = principal(db, req)
        return result(req, citation_record(db, u, cid))


def original_access(req, vid, citation_id):
    with engine().begin() as db:
        u = principal(db, req)
        v = found(
            one(
                db,
                "SELECT v.*,d.title,d.current_version_id FROM document_version v JOIN knowledge_document d USING(document_id) WHERE version_id=:v",
                v=uid(vid),
            )
        )
        c = None
        if citation_id:
            c = citation_record(db, u, citation_id)
            require(
                str(c["version_id"]) == vid,
                "引用与文件版本不一致。",
                403,
                "ACCESS_DENIED",
            )
        elif u["role_code"] != KNOWLEDGE:
            found(
                one(
                    db,
                    "SELECT 1 FROM document_version v JOIN knowledge_document d USING(document_id) WHERE v.version_id=:v AND "
                    + VISIBLE,
                    v=vid,
                    day=today(),
                    role=u["current_role_id"],
                )
            )
        file = file_for(v["file_path"])
        found(True if file.is_file() else None)
        return v, file, c


@routes.get("/versions/{vid}/file")
def original(req: Request, vid: str, citation_id: str | None = None):
    v, file, _ = original_access(req, vid, citation_id)
    media = {
        "PDF": "application/pdf",
        "DOCX": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "TXT": "text/plain; charset=utf-8",
    }
    return FileResponse(
        file,
        media_type=media[v["file_type"]],
        filename=v["title"] + "." + v["file_type"].lower(),
        content_disposition_type="inline",
    )


@routes.get("/versions/{vid}/preview", response_class=HTMLResponse)
def preview(
    req: Request, vid: str, citation_id: str | None = None, page: int | None = None
):
    status = 200
    try:
        v, file, citation = original_access(req, vid, citation_id)
        locator = citation["locator"] if citation else ""
        try:
            content = reading_page(
                file,
                v["file_type"],
                page,
                locator,
                citation["snippet"] if citation else "",
            )
        except Problem:
            raise
        except Exception:
            raise Problem(
                "原文件暂时无法解析，请联系知识管理员核对该版本。", 422
            ) from None
        context = dict(version=v, locator=locator, citation_id=citation_id, **content)
    except Problem as err:
        status, context = err.status, {"error": err.message}
    return req.app.state.templates.TemplateResponse(
        request=req, name="reader.html", context=context, status_code=status
    )
