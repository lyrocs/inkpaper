#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "beautifulsoup4>=4.12",
#   "numpy>=2.0",
#   "pillow>=10.0",
#   "playwright>=1.45",
#   "pygments>=2.17",
#   "pymupdf>=1.24",
# ]
# ///
"""Turn the Building InkPaper mdBook into a landscape XTCH book for the XTEINK X4 / X4 Pro.

Pipeline
  1. `mdbook build` renders the book; SUMMARY.md gives the reading order and the parts.
  2. Each chapter's <main> is restyled for an 800x480 landscape page. Chrome (Playwright)
     paginates it through print-to-PDF. Code blocks never break: a block that does not fit
     in the rest of a page starts on the next one, together with the sentence that
     introduces it. A block taller than a whole page is first shrunk, and only split if
     it still does not fit at the smallest code size.
  3. PyMuPDF rasterises every page, a footer (chapter, page / total) is stamped, and the
     image is quantised to the panel's 4 grey levels, then rotated into the 480x800
     portrait framebuffer the XTC reader draws as-is.
  4. Pages are packed into an XTCH container: header, metadata, chapter table, page index,
     XTH pages.

Run from the repository root:  uv run book/xtch/book2xtch.py
"""

from __future__ import annotations

import argparse
import html
import io
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import textwrap
import time
import tomllib
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pymupdf
from bs4 import BeautifulSoup, NavigableString, Tag
from PIL import Image, ImageDraw, ImageFont
from playwright.sync_api import sync_playwright
from pygments import lex
from pygments.lexers import get_lexer_by_name
from pygments.token import Comment, Keyword, Name, String
from pygments.util import ClassNotFound

REPO = Path(__file__).resolve().parents[2]
BOOK_DIR = REPO / "book"
DEFAULT_OUTPUT = REPO / "target" / "book" / "Building InkPaper.xtch"

# Panel geometry: the X4 framebuffer is 480x800 portrait; we read it turned sideways.
PANEL_W, PANEL_H = 480, 800
PAGE_W, PAGE_H = PANEL_H, PANEL_W

XTCH_MAGIC = b"XTCH"
XTH_MAGIC = b"XTH\0"
HEADER_SIZE = 56
METADATA_SIZE = 256
CHAPTER_SIZE = 96
INDEX_ENTRY_SIZE = 16
XTH_HEADER_SIZE = 22
XTH_DATA_SIZE = PANEL_W * ((PANEL_H + 7) // 8) * 2
XTH_PAGE_SIZE = XTH_HEADER_SIZE + XTH_DATA_SIZE

FOOTER_FONTS = [
    ("/System/Library/Fonts/Supplemental/Charter.ttc", 0),
    ("/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf", 0),
]


# ------------------------------------------------------------------- book structure


@dataclass
class Entry:
    """One unit of the reading order: a chapter file, or a part divider page."""

    kind: str  # "chapter" | "part"
    title: str
    source: str = ""  # chapter: path of the .md file relative to src
    contents: tuple[str, ...] = ()  # part: titles of the chapters it holds


@dataclass
class BookInfo:
    title: str
    author: str
    language: str
    entries: list[Entry]


LINK = re.compile(r"\[(?P<title>.+?)\]\((?P<href>[^)]+\.md)\)")
ROMAN = re.compile(r"^Part\s+([IVXLC]+)\b")


def load_book(book_dir: Path) -> BookInfo:
    config = tomllib.loads((book_dir / "book.toml").read_text(encoding="utf-8"))
    meta = config.get("book", {})
    src = book_dir / meta.get("src", "src")

    entries: list[Entry] = []
    number = 0
    for line in (src / "SUMMARY.md").read_text(encoding="utf-8").splitlines():
        heading = re.match(r"^#\s+(.+)$", line)
        if heading:
            if heading.group(1).strip().lower() != "summary":
                entries.append(Entry("part", heading.group(1).strip()))
            continue
        link = LINK.search(line)
        if not link:
            continue
        title = link.group("title").replace("`", "")
        if line.lstrip().startswith("- "):  # numbered chapter, as mdBook numbers it
            number += 1
            title = f"{number}. {title}"
        entries.append(Entry("chapter", title, source=link.group("href")))

    # a part page lists what its chapters cover, so the reader knows what comes next
    for i, entry in enumerate(entries):
        if entry.kind != "part":
            continue
        titles = []
        for later in entries[i + 1 :]:
            if later.kind == "part":
                break
            titles.append(later.title)
        entry.contents = tuple(titles)

    authors = meta.get("authors") or [""]
    return BookInfo(
        title=meta.get("title", "Building InkPaper"),
        author=", ".join(authors),
        language=meta.get("language", "en"),
        entries=entries,
    )


def build_html(book_dir: Path, out_dir: Path) -> None:
    if shutil.which("mdbook") is None:
        raise SystemExit("mdbook not found: install it with `cargo install mdbook`")
    subprocess.run(["mdbook", "build", str(book_dir), "-d", str(out_dir)], check=True,
                   stdout=subprocess.DEVNULL)


# -------------------------------------------------------------------- HTML rewrite


def highlight(code: str, lang: str) -> str:
    """Per-line HTML (<span class=l>) with token classes that survive four grey levels."""
    lexer = None
    if lang in {"rust", "toml"}:
        try:
            lexer = get_lexer_by_name(lang, stripnl=False, ensurenl=False)
        except ClassNotFound:
            lexer = None
    lines: list[list[str]] = [[]]

    def emit(text: str, cls: str | None) -> None:
        for i, part in enumerate(text.split("\n")):
            if i:
                lines.append([])
            if part:
                escaped = html.escape(part, quote=False)
                lines[-1].append(f'<span class="{cls}">{escaped}</span>' if cls else escaped)

    if lexer is None:
        for i, line in enumerate(code.split("\n")):
            emit("\n" if i else "", None)
            emit(line, "cmd" if lang in {"sh", "bash", "console"} and line and not line.startswith("#") else None)
    else:
        for token, value in lex(code, lexer):
            if token in Comment.Preproc or token in Name.Attribute or token in Name.Decorator:
                cls = "a"
            elif token in Comment:
                cls = "c"
            elif token in Keyword:
                cls = "k"
            elif token in String:
                cls = "s"
            else:
                cls = None
            emit(value, cls)

    while lines and not lines[-1]:
        lines.pop()
    return "".join(code_line(parts) for parts in lines)


def code_line(parts: list[str]) -> str:
    """One code line whose leading spaces become padding: a line too long for the page
    wraps under its own indentation instead of back at the left edge."""
    indent = 0
    while parts and not parts[0].startswith("<"):
        stripped = parts[0].lstrip(" ")
        indent += len(parts[0]) - len(stripped)
        if stripped:
            parts = [stripped, *parts[1:]]
            break
        parts = parts[1:]
    return f'<span class="l" style="--i:{indent}">{"".join(parts) or "&#8203;"}</span>'


def rewrite_pre(soup: BeautifulSoup, pre: Tag, snippets: list[str]) -> Tag:
    """A styled block whose body is a placeholder, filled after serialisation so that
    BeautifulSoup never touches the significant leading whitespace."""
    code = pre.find("code") or pre
    for hidden in code.select("span.boring"):  # mdBook's hidden `# ` lines
        hidden.decompose()
    languages = [c.removeprefix("language-") for c in (code.get("class") or []) if c.startswith("language-")]
    lang = languages[0] if languages else "text"
    raw = textwrap.dedent(code.get_text()).strip("\n")

    block = soup.new_tag("pre", attrs={"class": f"code lang-{lang}"})
    block.append(NavigableString(f"\x00CODE{len(snippets)}\x00"))
    snippets.append(highlight(raw, lang))
    return block


def rewrite_chapter(page_html: str, css: str, language: str) -> str:
    soup = BeautifulSoup(page_html, "html.parser")
    main = soup.find("main")
    if main is None:
        raise ValueError("mdBook page without <main>")

    snippets: list[str] = []
    for pre in main.find_all("pre"):
        pre.replace_with(rewrite_pre(soup, pre, snippets))

    # the sentence that introduces a listing travels with it to the next page
    for block in main.find_all("pre", class_="code"):
        lead = block.find_previous_sibling()
        if lead is not None and lead.name == "p" and lead.get_text().rstrip().endswith(":"):
            lead["class"] = (lead.get("class") or []) + ["lead-in"]

    # links cannot be followed on the device: keep their text
    for anchor in main.find_all("a"):
        anchor.unwrap()

    # long identifiers and paths may break after `_`, `/` and `::` rather than leave a
    # justified line full of gaps (every <code> left here is inline)
    for code in main.find_all("code"):
        for text in list(code.find_all(string=True)):
            text.replace_with(re.sub(r"(::|[_/])", "\\1\u200b", str(text)))

    content = re.sub(r"\x00CODE(\d+)\x00", lambda m: snippets[int(m.group(1))], main.decode_contents())
    return document(content, css, language)


def part_page(entry: Entry, css: str, language: str) -> str:
    match = ROMAN.match(entry.title)
    label = f"Part {match.group(1)}" if match else "Part"
    name = entry.title.split("—", 1)[-1].strip() if "—" in entry.title else entry.title
    items = "".join(f"<li>{html.escape(t)}</li>" for t in entry.contents)
    body = (
        f"<section class='part'><div class='part-label'>{html.escape(label)}</div>"
        f"<div class='part-title'>{html.escape(name)}</div><div class='part-rule'></div>"
        f"<ol class='part-contents'>{items}</ol></section>"
    )
    return document(body, css, language)


def document(body: str, css: str, language: str) -> str:
    return (
        f"<!DOCTYPE html><html lang='{html.escape(language)}'><head><meta charset='utf-8'>"
        f"<style>{css}</style></head><body>{body}</body></html>"
    )


# ------------------------------------------------------------------------- styling


@dataclass
class Style:
    body_px: float = 22.0
    code_px: float = 16.0
    no_wrap_px: float = 14.0
    min_code_px: float = 12.5
    line_height: float = 1.34
    margin_x: int = 24
    margin_top: int = 14
    footer_h: int = 26
    font: str = "Charter"
    mono: str = "Menlo"
    justify: bool = True

    @property
    def content_w(self) -> int:
        return PAGE_W - 2 * self.margin_x

    @property
    def content_h(self) -> int:
        return PAGE_H - self.margin_top - self.footer_h

    def css(self) -> str:
        align = "justify" if self.justify else "left"
        return f"""
@page {{ size: {PAGE_W}px {PAGE_H}px; margin: {self.margin_top}px {self.margin_x}px {self.footer_h}px; }}
html {{ -webkit-print-color-adjust: exact; print-color-adjust: exact; }}
body {{ margin: 0; font-family: '{self.font}', Georgia, serif; font-size: {self.body_px}px;
        line-height: {self.line_height}; color: #000; text-align: {align}; hyphens: auto;
        -webkit-hyphens: auto; orphans: 2; widows: 2; font-kerning: normal;
        text-rendering: geometricPrecision; }}
p {{ margin: 0 0 0.5em 0; }}
p.lead-in {{ break-after: avoid; page-break-after: avoid; }}
h1, h2, h3 {{ line-height: 1.15; text-align: left; hyphens: manual; break-after: avoid;
        page-break-after: avoid; break-inside: avoid; }}
h1 {{ font-size: 1.55em; margin: 0.1em 0 0.55em; padding-bottom: 0.25em; border-bottom: 3px solid #000; }}
h2 {{ font-size: 1.22em; margin: 0.75em 0 0.35em; }}
h3 {{ font-size: 1.05em; margin: 0.6em 0 0.3em; font-style: italic; }}
h1 + *, h2 + *, h3 + * {{ break-before: avoid; }}
ul, ol {{ margin: 0 0 0.5em 0; padding-left: 1.3em; }}
li {{ margin: 0 0 0.15em 0; }}
blockquote {{ margin: 0.4em 0 0.6em; padding: 0.1em 0 0.1em 0.8em; border-left: 3px solid #000; }}
code {{ font-family: '{self.mono}', monospace; font-size: {self.code_px / self.body_px:.3f}em;
        hyphens: none; -webkit-hyphens: none; overflow-wrap: anywhere; }}
code, pre.code .l {{ unicode-bidi: bidi-override; direction: ltr; }}
pre.code {{ font-family: '{self.mono}', monospace; font-size: {self.code_px}px; line-height: 1.24;
        margin: 0.3em 0 0.65em; padding: 6px 9px; border: 2px solid #000; border-radius: 4px;
        text-align: left; white-space: pre-wrap; overflow-wrap: anywhere; hyphens: none;
        -webkit-hyphens: none; break-inside: avoid; page-break-inside: avoid; }}
pre.code .l {{ display: block; padding-left: calc((var(--i, 0) + 2) * 1ch); text-indent: -2ch;
        min-height: 1.24em; break-inside: avoid; }}
pre.code.oversized {{ break-before: page; page-break-before: always; break-inside: auto; page-break-inside: auto; }}
pre.code .k {{ font-weight: bold; }}
pre.code .c {{ font-style: italic; color: #555; }}
pre.code .a {{ color: #555; }}
pre.code .cmd {{ font-weight: bold; }}
table {{ border-collapse: collapse; font-size: 0.8em; line-height: 1.25; margin: 0.4em 0 0.7em;
        text-align: left; hyphens: manual; }}
th, td {{ border: 1px solid #000; padding: 3px 6px; vertical-align: top; }}
tr {{ break-inside: avoid; }}
hr {{ border: 0; border-top: 1px solid #000; }}
section.part {{ height: {self.content_h - 4}px; display: flex; flex-direction: column;
        justify-content: center; text-align: left; hyphens: manual; }}
.part-label {{ font-size: 0.85em; letter-spacing: 0.18em; text-transform: uppercase; }}
.part-title {{ font-size: 1.75em; font-weight: bold; line-height: 1.15; margin-top: 0.2em; }}
.part-rule {{ border-top: 3px solid #000; width: 5em; margin: 0.6em 0 0.7em; }}
.part-contents {{ list-style: none; padding: 0; margin: 0; font-size: 0.92em; }}
.part-contents li {{ margin: 0 0 0.25em; }}
"""


# Each code block is fitted in two steps. Width: a block with a line too long for the page
# takes the largest size, down to `noWrapPx`, at which no line wraps; past that it keeps
# its size and wraps under its indentation. Height: a block taller than a page shrinks
# down to `minPx`; only when even that is too tall does it start on a fresh page and split
# between lines.
#
# Then the "keep with" links are checked. A block travels to the next page with its
# introducing sentence (and the heading glued to that sentence), but when the whole chain
# is taller than a page Chrome would honour the link by breaking inside the code, so the
# link is released and the block moves alone.
FIT_JS = """([height, basePx, noWrapPx, minPx]) => {
  const wraps = el => [...el.querySelectorAll('.l')].some(line =>
    line.getBoundingClientRect().height > parseFloat(getComputedStyle(line).fontSize) * 1.24 * 1.5);
  const tall = el => el.getBoundingClientRect().height > height;
  const outer = el => {
    const cs = getComputedStyle(el);
    return el.getBoundingClientRect().height + parseFloat(cs.marginTop) + parseFloat(cs.marginBottom);
  };
  const heading = el => el && /^H[1-6]$/.test(el.tagName);
  let narrowed = 0, shrunk = 0, split = 0, released = 0;
  for (const el of document.querySelectorAll('pre.code')) {
    let px = basePx;
    if (wraps(el)) {
      let size = basePx;
      while (size > noWrapPx && wraps(el)) {
        size -= 0.5;
        el.style.fontSize = size + 'px';
      }
      if (wraps(el)) el.style.fontSize = '';
      else { px = size; narrowed++; }
    }
    if (!tall(el)) continue;
    let size = px;
    while (size > minPx && tall(el)) {
      size -= 0.5;
      el.style.fontSize = size + 'px';
    }
    if (tall(el)) {
      el.style.fontSize = px === basePx ? '' : px + 'px';
      el.classList.add('oversized');
      split++;
    } else shrunk++;
  }
  for (const el of document.querySelectorAll('pre.code:not(.oversized)')) {
    const prev = el.previousElementSibling;
    if (prev && prev.classList.contains('lead-in')) {
      const head = prev.previousElementSibling;
      const chain = outer(prev) + outer(el) + (heading(head) ? outer(head) : 0);
      if (chain > height) { prev.classList.remove('lead-in'); released++; }
    } else if (heading(prev) && outer(prev) + outer(el) > height) {
      prev.style.breakAfter = 'auto';
      el.style.breakBefore = 'auto';
      released++;
    }
  }
  return [narrowed, shrunk, split, released];
}"""


@dataclass
class Section:
    title: str
    pdf: Path
    pages: int


def launch(playwright):
    try:
        return playwright.chromium.launch()
    except Exception:
        try:
            return playwright.chromium.launch(channel="chrome")
        except Exception as error:
            raise SystemExit(
                "no browser for Playwright: run `uv run --with playwright playwright install chromium`"
            ) from error


def render_sections(info: BookInfo, entries: list[Entry], html_dir: Path, style: Style, work: Path, log) -> list[Section]:
    css = style.css()
    sections: list[Section] = []
    with sync_playwright() as playwright:
        browser = launch(playwright)
        # lay out at the printed content width so measurements match the PDF
        page = browser.new_page(viewport={"width": style.content_w, "height": style.content_h})
        page.emulate_media(media="print")
        for i, entry in enumerate(entries):
            if entry.kind == "part":
                doc = part_page(entry, css, info.language)
            else:
                source = html_dir / Path(entry.source).with_suffix(".html")
                doc = rewrite_chapter(source.read_text(encoding="utf-8"), css, info.language)
            # beside the mdBook output, so relative image paths still resolve
            path = html_dir / f"__xtch_{i:03d}.html"
            path.write_text(doc, encoding="utf-8")
            page.goto(path.as_uri(), wait_until="load")
            page.evaluate("document.fonts.ready")
            narrowed, shrunk, split, released = page.evaluate(
                FIT_JS, [style.content_h - 2, style.code_px, style.no_wrap_px, style.min_code_px])
            pdf = work / f"section{i:03d}.pdf"
            page.pdf(path=str(pdf), prefer_css_page_size=True, print_background=True)
            with pymupdf.open(pdf) as rendered:
                count = rendered.page_count
            sections.append(Section(entry.title, pdf, count))
            notes = "".join(note for note in (
                f", {narrowed} code block(s) narrowed to avoid wrapping" if narrowed else "",
                f", {shrunk} shrunk to fit a page" if shrunk else "",
                f", {split} split across pages" if split else "",
                f", {released} moved without its lead-in" if released else "",
            ))
            log(f"  [{i + 1}/{len(entries)}] {entry.title}: {count} page(s){notes}")
        browser.close()
    return sections


def render_cover(info: BookInfo, style: Style, work: Path) -> Image.Image:
    """Portrait cover: the reader uses the first page for its library thumbnail."""
    title, _, subtitle = info.title.partition(":")
    doc = f"""<!DOCTYPE html><html><head><meta charset='utf-8'><style>
html, body {{ margin: 0; width: {PANEL_W}px; height: {PANEL_H}px; background: #fff; }}
body {{ font-family: '{style.font}', serif; display: flex; flex-direction: column; justify-content: center;
        padding: 0 40px; box-sizing: border-box; }}
.bar {{ border-top: 7px solid #000; width: 100%; margin: 26px 0; }}
h1 {{ font-size: 56px; line-height: 1.04; margin: 0; }}
.sub {{ font-size: 25px; line-height: 1.3; }}
.sub .word {{ white-space: nowrap; }}
.code {{ font-family: '{style.mono}', monospace; font-size: 17px; line-height: 1.45; color: #444;
         margin-top: 46px; border-left: 4px solid #000; padding-left: 14px; }}
.by {{ font-size: 18px; margin-top: 40px; }}
</style></head><body>
<div class='bar'></div><h1>{html.escape(title.strip())}</h1><div class='bar'></div>
<div class='sub'>{' '.join(f"<span class='word'>{html.escape(word)}</span>" for word in subtitle.split())}</div>
<div class='code'>self.battery.update(cx, |state, cx| {{<br>&nbsp;&nbsp;if state.set(reading) {{<br>&nbsp;&nbsp;&nbsp;&nbsp;cx.notify();<br>&nbsp;&nbsp;}}<br>}});</div>
<div class='by'>{html.escape(info.author)}</div>
</body></html>"""
    path = work / "cover.html"
    path.write_text(doc, encoding="utf-8")
    with sync_playwright() as playwright:
        browser = launch(playwright)
        page = browser.new_page(viewport={"width": PANEL_W, "height": PANEL_H})
        page.goto(path.as_uri())
        png = page.screenshot()
        browser.close()
    return Image.open(io.BytesIO(png)).convert("L")


# -------------------------------------------------------------- raster + encoding

# Grey levels as previews show them, and the XTH pixel codes (the LUT swaps 1 and 2).
LEVEL_RGB = np.array([0, 85, 170, 255], dtype=np.uint8)
LEVEL_CODE = np.array([3, 1, 2, 0], dtype=np.uint8)


@dataclass
class Quantizer:
    # exclusive upper bounds of 8-bit grey for black, dark grey and light grey; the rest is white
    thresholds: tuple[int, int, int] = (80, 150, 215)

    def levels(self, image: Image.Image) -> np.ndarray:
        grey = np.asarray(image.convert("L"), dtype=np.uint8)
        black, dark, light = self.thresholds
        return np.select([grey < black, grey < dark, grey < light], [0, 1, 2], 3).astype(np.uint8)


def rasterize(pdf_page: pymupdf.Page, supersample: int) -> Image.Image:
    zoom = PAGE_W / pdf_page.rect.width * max(supersample, 1)
    pixmap = pdf_page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), colorspace=pymupdf.csGRAY, alpha=False)
    image = Image.frombytes("L", (pixmap.width, pixmap.height), pixmap.samples)
    if image.size != (PAGE_W, PAGE_H):
        image = image.resize((PAGE_W, PAGE_H), Image.Resampling.BOX if supersample > 1 else Image.Resampling.LANCZOS)
    return image


def footer_font(size: int) -> ImageFont.ImageFont:
    for path, index in FOOTER_FONTS:
        if Path(path).exists():
            return ImageFont.truetype(path, size, index=index)
    return ImageFont.load_default(size)


def draw_footer(image: Image.Image, style: Style, title: str, page_no: int, total: int, font) -> None:
    draw = ImageDraw.Draw(image)
    rule_y = PAGE_H - style.footer_h + 4
    draw.line([(style.margin_x, rule_y), (PAGE_W - style.margin_x, rule_y)], fill=0, width=1)
    right = f"{page_no} / {total}"
    right_w = draw.textlength(right, font=font)
    room = PAGE_W - 2 * style.margin_x - right_w - 24
    text = title
    while text and draw.textlength(text, font=font) > room:
        text = text[:-2] + "…"
    y = PAGE_H - style.footer_h + 6
    draw.text((style.margin_x, y), text, fill=0, font=font)
    draw.text((PAGE_W - style.margin_x - right_w, y), right, fill=0, font=font)


def to_panel(levels: np.ndarray, buttons: str) -> np.ndarray:
    """Rotate the landscape page (480 rows x 800 cols) into the portrait framebuffer.

    buttons="right": the device is turned counter-clockwise, front buttons on the right.
    buttons="left": the opposite turn.
    """
    if buttons == "right":
        return np.rot90(levels, k=1)
    if buttons == "left":
        return np.rot90(levels, k=-1)
    raise ValueError(buttons)


def encode_xth(levels: np.ndarray) -> bytes:
    """`levels`: 800 rows x 480 cols of grey indexes, 0 = black .. 3 = white."""
    rows, cols = levels.shape
    assert (cols, rows) == (PANEL_W, PANEL_H), levels.shape
    code = LEVEL_CODE[levels]
    # column-major, columns right to left, 8 vertical pixels per byte, MSB at the top
    columns = code[:, ::-1].T
    high = np.packbits((columns >> 1) & 1, axis=1, bitorder="big")
    low = np.packbits(columns & 1, axis=1, bitorder="big")
    data = high.tobytes() + low.tobytes()
    assert len(data) == XTH_DATA_SIZE
    return XTH_MAGIC + struct.pack("<HHBBIQ", cols, rows, 0, 0, len(data), 0) + data


def fit_utf8(text: str, size: int) -> bytes:
    """NUL-padded UTF-8 that never cuts a character in half."""
    encoded = text.encode("utf-8")[: size - 1]
    while encoded:
        try:
            encoded.decode("utf-8")
            break
        except UnicodeDecodeError:
            encoded = encoded[:-1]
    return encoded.ljust(size, b"\0")


@dataclass
class Chapter:
    name: str
    start: int  # 0-based, inclusive
    end: int


def write_xtch(output: Path, info: BookInfo, chapters: list[Chapter], pages, page_count: int) -> None:
    metadata_off = HEADER_SIZE
    chapter_off = metadata_off + METADATA_SIZE
    index_off = chapter_off + CHAPTER_SIZE * len(chapters)
    data_off = index_off + INDEX_ENTRY_SIZE * page_count

    header = struct.pack(
        "<4sBBHBBBBIQQQQII",
        XTCH_MAGIC, 1, 0, page_count, 0, 1, 0, 1 if chapters else 0, 0,
        metadata_off, index_off, data_off, 0, chapter_off if chapters else 0, 0,
    )
    assert len(header) == HEADER_SIZE
    metadata = (
        fit_utf8(info.title, 128) + fit_utf8(info.author, 64) + fit_utf8("InkPaper", 32)
        + fit_utf8(info.language, 16) + struct.pack("<IHHQ", int(time.time()), 0, len(chapters), 0)
    )
    assert len(metadata) == METADATA_SIZE
    table = b"".join(fit_utf8(c.name, 80) + struct.pack("<HHIII", c.start + 1, c.end + 1, 0, 0, 0) for c in chapters)
    index = b"".join(
        struct.pack("<QIHH", data_off + i * XTH_PAGE_SIZE, XTH_PAGE_SIZE, PANEL_W, PANEL_H) for i in range(page_count)
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    with open(output, "wb") as file:
        file.write(header + metadata + table + index)
        written = 0
        for page in pages:
            assert len(page) == XTH_PAGE_SIZE
            file.write(page)
            written += 1
    assert written == page_count, (written, page_count)


# ---------------------------------------------------------------------------- main


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("-o", "--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--book", type=Path, default=BOOK_DIR, help="mdBook directory (holds book.toml)")
    parser.add_argument("--only", nargs="*", help="substrings of chapter files to include, for trial runs")
    parser.add_argument("--buttons", choices=["right", "left"], default="right",
                        help="side of the front buttons while reading in landscape")
    parser.add_argument("--body-px", type=float, default=Style.body_px)
    parser.add_argument("--code-px", type=float, default=Style.code_px)
    parser.add_argument("--no-wrap-px", type=float, default=Style.no_wrap_px,
                        help="smallest size a code block may shrink to so its long lines do not wrap")
    parser.add_argument("--min-code-px", type=float, default=Style.min_code_px,
                        help="smallest size a code block taller than a page may shrink to")
    parser.add_argument("--line-height", type=float, default=Style.line_height)
    parser.add_argument("--font", default=Style.font)
    parser.add_argument("--mono", default=Style.mono)
    parser.add_argument("--ragged", action="store_true", help="left-aligned text instead of justified")
    parser.add_argument("--supersample", type=int, default=2)
    parser.add_argument("--thresholds", default="80,150,215", help="grey bounds for black, dark and light")
    parser.add_argument("--no-cover", action="store_true")
    parser.add_argument("--preview", type=Path, help="directory for PNG previews of the e-ink pages")
    parser.add_argument("--preview-pages", default="", help="comma list of 1-based pages to preview (default: all)")
    args = parser.parse_args()

    log = lambda message: print(message, file=sys.stderr, flush=True)  # noqa: E731
    style = Style(body_px=args.body_px, code_px=args.code_px, no_wrap_px=args.no_wrap_px, min_code_px=args.min_code_px,
                  line_height=args.line_height, font=args.font, mono=args.mono, justify=not args.ragged)
    quantizer = Quantizer(tuple(int(x) for x in args.thresholds.split(",")))  # type: ignore[arg-type]

    info = load_book(args.book)
    entries = info.entries
    if args.only:
        entries = [e for e in entries if e.kind == "chapter" and any(s in e.source for s in args.only)]
        if not entries:
            log("no chapter matches --only")
            return 1

    with tempfile.TemporaryDirectory(prefix="book2xtch-") as tmp:
        work = Path(tmp)
        started = time.time()
        html_dir = work / "html"
        log("Building the book with mdBook…")
        build_html(args.book, html_dir)
        log(f"Laying out {len(entries)} sections…")
        sections = render_sections(info, entries, html_dir, style, work, log)
        cover = None if args.no_cover else render_cover(info, style, work)

        chapters: list[Chapter] = []
        next_page = 0
        if cover is not None:
            chapters.append(Chapter("Cover", 0, 0))
            next_page = 1
        for section in sections:
            chapters.append(Chapter(section.title, next_page, next_page + section.pages - 1))
            next_page += section.pages
        total = next_page
        log(f"Layout done in {time.time() - started:.1f}s: {total} pages")

        font = footer_font(15)
        wanted = {int(x) for x in args.preview_pages.split(",") if x}
        if args.preview:
            args.preview.mkdir(parents=True, exist_ok=True)

        def preview(page_no: int, levels: np.ndarray) -> None:
            if args.preview and (not wanted or page_no in wanted):
                Image.fromarray(LEVEL_RGB[levels]).save(args.preview / f"p{page_no:04d}.png")

        def pages():
            page_no = 0
            if cover is not None:
                levels = quantizer.levels(cover)
                page_no += 1
                preview(page_no, levels)
                yield encode_xth(levels)
            for section in sections:
                with pymupdf.open(section.pdf) as rendered:
                    for pdf_page in rendered:
                        image = rasterize(pdf_page, args.supersample)
                        page_no += 1
                        draw_footer(image, style, section.title, page_no, total, font)
                        levels = quantizer.levels(image)
                        preview(page_no, levels)
                        yield encode_xth(to_panel(levels, args.buttons))

        write_xtch(args.output, info, chapters, pages(), total)
        size = args.output.stat().st_size / 1e6
        log(f"Wrote {args.output} ({size:.1f} MB, {total} pages) in {time.time() - started:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
