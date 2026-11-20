"""Render the authorized original file one reading page at a time."""

from base64 import b64encode
from contextlib import closing
from io import BytesIO
from itertools import islice
import re
from threading import Lock

from docx import Document
import pypdfium2 as pdfium

from .core import require
from .knowledge import SourceBlock, word_blocks


# PDFium calls, including closing handles, must be serialized in each process.
_pdf_lock = Lock()


def pdf_page(path, number, locator):
    cited = re.fullmatch(r"第\s*(\d+)\s*页", locator)
    if number is None:
        number = int(cited[1]) if cited else 1
    with _pdf_lock, pdfium.PdfDocument(str(path)) as doc:
        total = len(doc)
        require(1 <= number <= total, "页码超出原文范围。")
        with closing(doc[number - 1]) as page:
            width, height = page.get_size()
            scale = min(1500 / max(1, width), 2100 / max(1, height))
            with closing(page.render(scale=scale)) as bitmap, bitmap.to_pil() as image:
                png = BytesIO()
                image.save(png, format="PNG")
            with closing(page.get_textpage()) as text_page:
                page_text = (
                    text_page.get_text_bounded().replace("\r\n", "\n").replace("\r", "")
                )
    return dict(
        number=number,
        total=total,
        image=b64encode(png.getvalue()).decode("ascii"),
        text=page_text,
    )


def text_blocks(path, kind):
    if kind == "DOCX":
        yield from word_blocks(Document(path).iter_inner_content())
    else:
        with path.open(encoding="utf-8-sig") as file:
            for number, line in enumerate(file, 1):
                yield SourceBlock("line", f"第 {number} 行", line.rstrip("\n"))


def reading_page(path, kind, number=None, locator="", snippet=""):
    if kind == "PDF":
        return pdf_page(path, number, locator)
    require(number is None or number > 0, "页码从 1 开始。")
    selected = number or 1
    chosen = []
    total = 0
    target_page, target_row, best_match = 0, 0, 0
    excerpt = "".join(snippet.split())
    blocks = text_blocks(path, kind)
    while batch := list(islice(blocks, 40)):
        total += 1
        if locator and best_match < 2:
            for index, block in enumerate(batch, 1):
                if block.locator != locator:
                    continue
                score = 2 if excerpt and excerpt in "".join(block.text.split()) else 1
                if score > best_match:
                    best_match, target_page, target_row = score, total, index
                    if number is None:
                        selected = total
        if total == selected:
            chosen = batch
    require(selected <= max(total, 1), "页码超出原文范围。")
    return dict(
        number=selected,
        total=max(total, 1),
        blocks=chosen,
        target_row=target_row if selected == target_page else 0,
    )
