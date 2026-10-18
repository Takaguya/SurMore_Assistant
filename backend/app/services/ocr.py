"""OCR through NVIDIA's API: page images in, Markdown out.

Two kinds of model work as NVIDIA_OCR_MODEL:
- Vision-language models (e.g. nvidia/nemotron-3.5-super-vl-120b-a12b) get the page with instructions to
  transcribe it as Markdown and describe its diagrams. They read slides, infographics and diagrams well.
- Nemotron Parse (nvidia/nemotron-parse) is called with the `markdown_bbox` tool and answers with a tool call
  whose arguments are JSON: [[{"type", "text", "bbox"}, ...]] in reading order. It suits plain text pages,
  but invents words on diagrams and stylised text.
"""

import base64
import io
import json
import logging
import re
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from pathlib import Path

import openai
from PIL import Image

from ..core.config import get_settings
from . import nvidia

logger = logging.getLogger(__name__)

# Try a sharp image first; if the model chokes on it, retry once smaller.
IMAGE_SIDES_PX = (1600, 1100)
MIN_SIDE_PX = 1000

# Errors that will fail every page the same way, so there's no point continuing.
FATAL_ERRORS = (
    nvidia.NvidiaConfigError,
    openai.AuthenticationError,
    openai.PermissionDeniedError,
    openai.NotFoundError,
)

SKIPPED_BLOCKS = {"Page-header", "Page-footer"}


class OcrError(Exception):
    pass


def image_to_markdown(image: Image.Image) -> str:
    """OCR one page image. Returns "" when the page genuinely has no text."""
    nvidia.check_key()
    last_error: Exception = OcrError("OCR failed")
    for side in IMAGE_SIDES_PX:
        try:
            return _request(_data_uri(image, side))
        except FATAL_ERRORS:
            raise
        except Exception as exc:  # timeouts, 5xx, malformed output: retry smaller
            logger.warning("OCR attempt at %dpx failed: %s", side, exc)
            last_error = exc
    raise last_error


def render_pdf_page(path: Path, page_number: int) -> Image.Image:
    import pypdfium2 as pdfium

    pdf = pdfium.PdfDocument(str(path))
    try:
        return _render(pdf, page_number)
    finally:
        pdf.close()


def ocr_pdf_pages(
    path: Path, page_numbers: list[int], on_progress: Callable[[int, int], None]
) -> dict[int, str | Exception]:
    """OCR several PDF pages concurrently. Returns page number -> Markdown, or the error for that page."""
    import pypdfium2 as pdfium

    settings = get_settings()
    results: dict[int, str | Exception] = {}
    pending: dict[Future, int] = {}

    def collect(done: set[Future]) -> None:
        for future in done:
            page = pending.pop(future)
            try:
                results[page] = future.result()
            except FATAL_ERRORS:
                raise
            except Exception as exc:
                results[page] = exc
            on_progress(len(results), len(page_numbers))

    pdf = pdfium.PdfDocument(str(path))
    try:
        with ThreadPoolExecutor(max_workers=settings.ocr_concurrency) as pool:
            try:
                for page in page_numbers:
                    # pdfium isn't thread-safe, so pages are rendered here and only the API calls run in parallel.
                    pending[pool.submit(image_to_markdown, _render(pdf, page))] = page
                    if len(pending) >= settings.ocr_concurrency * 2:
                        done, _ = wait(pending, return_when=FIRST_COMPLETED)
                        collect(done)
                while pending:
                    done, _ = wait(pending, return_when=FIRST_COMPLETED)
                    collect(done)
            except BaseException:
                for future in pending:
                    future.cancel()
                raise
    finally:
        pdf.close()
    return results


def _render(pdf, page_number: int) -> Image.Image:
    page = pdf[page_number - 1]
    width, height = page.get_size()  # in points (1/72 inch)
    scale = IMAGE_SIDES_PX[0] / max(width, height)
    return page.render(scale=scale).to_pil()


def _data_uri(image: Image.Image, max_side: int) -> str:
    img = image.convert("RGB")
    longest = max(img.size)
    if longest > max_side or longest < MIN_SIDE_PX:
        target = max_side if longest > max_side else MIN_SIDE_PX
        ratio = target / longest
        img = img.resize((round(img.width * ratio), round(img.height * ratio)), Image.Resampling.LANCZOS)
    buffer = io.BytesIO()
    img.save(buffer, format="JPEG", quality=90)
    return "data:image/jpeg;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")


VISION_SYSTEM_PROMPT = (
    "You turn images of document pages into Markdown for a search index. "
    "You copy text exactly and never invent words."
)

# The example matters: without it, models tend to skip the "Figure:" descriptions.
VISION_PROMPT = """Convert this page image to Markdown.

1. Transcribe all text exactly as written, in reading order: headings, paragraphs, lists, tables (as Markdown tables) and code (in fenced code blocks). Skip anything you cannot read.
2. After the labels of each diagram, chart or infographic, add one line that starts with "Figure:" and says in 1-3 sentences what the diagram shows. Do this only for real diagrams, charts and infographics, not for titles, logos or decorative pictures.
3. Do not include the page number or the header and footer text that repeats on every page.
4. If the page has no text, output nothing.

Example for a diagram:

## 2) Router Pattern
- User
- Query
- Router LLM
- Path 1
- Path 2
- LLM response

Figure: A router LLM reads the user's query and sends it down one of two paths, each handled by its own LLM, which returns the response.

Output only the Markdown."""

THINK_BLOCK = re.compile(r"<think>.*?</think>", re.DOTALL)
WRAPPING_FENCE = re.compile(r"^```(?:markdown|md)?\s*\n(.*)\n```$", re.DOTALL)


def _request(data_uri: str) -> str:
    if "nemotron-parse" in get_settings().nvidia_ocr_model:
        return _request_parse(data_uri)
    return _request_vision(data_uri)


def _request_vision(data_uri: str) -> str:
    model = get_settings().nvidia_ocr_model
    response = nvidia.sync_client().chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": VISION_SYSTEM_PROMPT},
            {"role": "user", "content": [
                {"type": "text", "text": VISION_PROMPT},
                {"type": "image_url", "image_url": {"url": data_uri}},
            ]},
        ],
        temperature=0,
        max_tokens=8192,
        # Nemotron models reason first by default: ~20x slower for OCR, with no gain in accuracy.
        extra_body={"chat_template_kwargs": {"enable_thinking": False}} if "nemotron" in model else None,
    )
    choice = response.choices[0]
    if choice.finish_reason == "length":
        raise OcrError("the page has more text than the OCR model can return in one go")
    text = THINK_BLOCK.sub("", choice.message.content or "").strip()
    fenced = WRAPPING_FENCE.match(text)
    return fenced.group(1).strip() if fenced else text


def _request_parse(data_uri: str) -> str:
    settings = get_settings()
    response = nvidia.sync_client().chat.completions.create(
        model=settings.nvidia_ocr_model,
        messages=[{"role": "user", "content": [{"type": "image_url", "image_url": {"url": data_uri}}]}],
        tools=[{"type": "function", "function": {"name": "markdown_bbox"}}],
        max_tokens=4096,
    )
    choice = response.choices[0]
    if choice.finish_reason == "length":
        raise OcrError("the page has more text than the OCR model can return in one go")

    message = choice.message
    if message.tool_calls:
        blocks = json.loads(message.tool_calls[0].function.arguments)
        if blocks and isinstance(blocks[0], list):  # one list of blocks per page
            blocks = [block for page in blocks for block in page]
        return blocks_to_markdown(blocks)
    if message.content and message.content.strip():
        # Prompt-style models answer in content, with layout tags around the text.
        return re.sub(r"<[^>\s]+>", "", message.content).strip()
    raise OcrError("the OCR model returned an empty response")


def blocks_to_markdown(blocks: list[dict]) -> str:
    parts = []
    for block in blocks:
        kind = block.get("type") or "Text"
        text = (block.get("text") or "").strip()
        if kind in SKIPPED_BLOCKS or not text:
            continue
        if kind == "Title" and not text.startswith("#"):
            text = f"# {text}"
        elif kind == "Section-header" and not text.startswith("#"):
            text = f"## {text}"
        elif kind == "List-item" and not re.match(r"^([-*+]|\d+[.)])\s", text):
            text = f"- {text}"
        elif kind == "Table":
            text = latex_table_to_markdown(text)
        elif kind == "Formula" and not text.startswith("$"):
            text = f"$$\n{text}\n$$"
        parts.append(text)
    return "\n\n".join(parts)


def latex_table_to_markdown(text: str) -> str:
    """Nemotron Parse may return tables as LaTeX tabular; convert them to Markdown tables."""
    if "\\begin{tabular}" not in text:
        return text
    body = re.sub(r"\\begin\{tabular\}\{[^}]*\}|\\end\{tabular\}", "", text)
    body = re.sub(r"\\(hline|toprule|midrule|bottomrule)|\\cline\{[^}]*\}", "", body)

    rows: list[list[str]] = []
    for raw_row in re.split(r"\\\\", body):
        if not raw_row.strip():
            continue
        cells: list[str] = []
        for cell in re.split(r"(?<!\\)&", raw_row):
            span = re.match(r"\s*\\multicolumn\{(\d+)\}\{[^}]*\}\{(.*)\}\s*$", cell, re.DOTALL)
            if span:
                cells.append(_latex_cell(span.group(2)))
                cells.extend([""] * (int(span.group(1)) - 1))
            else:
                cells.append(_latex_cell(cell))
        rows.append(cells)
    if not rows:
        return text

    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    lines = ["| " + " | ".join(rows[0]) + " |", "|" + " --- |" * width]
    lines += ["| " + " | ".join(r) + " |" for r in rows[1:]]
    return "\n".join(lines)


def _latex_cell(cell: str) -> str:
    cell = re.sub(r"\\multirow\{[^}]*\}\{[^}]*\}\{(.*?)\}", r"\1", cell)
    cell = re.sub(r"\\textbf\{(.*?)\}", r"**\1**", cell)
    cell = re.sub(r"\\textit\{(.*?)\}", r"*\1*", cell)
    for escaped, plain in (("\\&", "&"), ("\\%", "%"), ("\\_", "_"), ("\\#", "#"), ("\\$", "$")):
        cell = cell.replace(escaped, plain)
    return " ".join(cell.replace("|", "\\|").split())
