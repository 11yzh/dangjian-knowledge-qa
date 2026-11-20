from uuid import uuid4

from docx import Document


def test_reader_formats_and_citation_access(s):
    for key, marker in [
        ("M01", "data:image/png;base64,"),
        ("M02", "报名说明"),
        ("M03", "第 1 行"),
    ]:
        path = "/versions/" + s.version(key) + "/preview"
        doc = s.req("U1", "GET", path)
        assert doc.status_code == 200 and doc.headers["content-type"].startswith(
            "text/html"
        )
        assert marker in doc.text and "attachment" not in doc.headers.get(
            "content-disposition", ""
        )
    assert (
        s.req("U1", "GET", "/versions/" + s.version("M04") + "/preview").status_code
        == 404
    )
    sid, request_id = s.session(), str(uuid4())
    answer = s.ask(sid=sid, key=request_id).json()["data"]
    ref = answer["citations"][0]
    href = (
        "/versions/"
        + ref["source"]["version_id"]
        + "/preview?citation_id="
        + ref["citation_id"]
    )
    assert ref["locator"] in s.req("U1", "GET", href).text
    assert s.req("U2", "GET", href).status_code == 404
    other = (
        "/versions/" + s.version("M02") + "/preview?citation_id=" + ref["citation_id"]
    )
    assert s.req("U1", "GET", other).status_code == 403
    assert s.req("U1", "GET", href + "&page=999").status_code == 400

    s.ask(sid=sid)
    pair_url = f"/sessions/{sid}/messages?request_id={request_id}"
    pair = s.obj("U1", "GET", pair_url)
    assert (
        pair["request_state"] == "DONE" and pair["total"] == 2 and pair["session_title"]
    )
    assert [row["speaker"] for row in pair["items"]] == ["USER", "ASSISTANT"]
    assert pair["items"][1]["message_id"] == answer["message_id"]
    assert s.req("U2", "GET", pair_url).status_code == 404


def test_text_original_page_and_word_order(tmp_path):
    from app.reader import reading_page

    text = tmp_path / "行.txt"
    text.write_text(
        "\n".join(f"原始行 {n}" for n in range(1, 87)), encoding="utf-8-sig"
    )
    view = reading_page(text, "TXT", locator="第 83 行")
    assert (view["number"], view["total"], view["target_row"]) == (3, 3, 3)
    assert view["blocks"][2].text == "原始行 83"

    doc = Document()
    doc.add_heading("同名小节", 1)
    doc.add_paragraph("第一处内容")
    doc.add_table(1, 2).rows[0].cells[1].text = "单元格"
    for _ in range(40):
        doc.add_paragraph("间隔段落")
    doc.add_heading("同名小节", 1)
    doc.add_paragraph("第二处内容")
    path = tmp_path / "段落和表格.docx"
    doc.save(path)
    first = reading_page(path, "DOCX", 1)
    assert [b.kind for b in first["blocks"][:4]] == [
        "heading",
        "paragraph",
        "row",
        "paragraph",
    ]
    cited = reading_page(
        path, "DOCX", locator="同名小节 · 第 1 段", snippet="第二处内容"
    )
    assert (
        cited["number"] == 2
        and cited["blocks"][cited["target_row"] - 1].text == "第二处内容"
    )
