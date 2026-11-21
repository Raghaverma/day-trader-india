#!/usr/bin/env python3
"""PDF pages for the P&L journal. Standard library only; macOS only.

    md_to_html(md, title)          the journal's Markdown (headings, paragraphs, lists, tables, bold, links) as one
                                   styled, self-contained HTML page
    render(home, html, pdf)        prints it to an A4 PDF with macOS's own WebKit, through html2pdf.swift

The Swift helper is compiled once (swiftc, from the Xcode command-line tools) into <trader folder>/tools/html2pdf and
again whenever html2pdf.swift is newer. WebKit needs a logged-in session: the launchd agent runs in one, a sandboxed
shell may not. Every failure comes back as a PdfError with the reason; the journal keeps its Markdown either way.
"""

import html
import os
import re
import shutil
import subprocess

SWIFT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "html2pdf.swift")
TIMEOUT_S = 90               # the helper gives up by itself after 60 s
COMPILE_TIMEOUT_S = 300
MAX_BYTES = 50 * 1024 * 1024  # a page is ~100 KB: anything this big is a runaway print job, not a page


class PdfError(Exception):
    pass


# ---------------------------------------------------------------- Markdown -> HTML

CSS = """
* { box-sizing: border-box; }
body { font: 10pt/1.45 -apple-system, "Helvetica Neue", Helvetica, Arial, sans-serif; color: #1d1d1f; margin: 0; }
h1 { font-size: 17pt; line-height: 1.25; margin: 0 0 6pt; color: #0b3d62; }
h2 { font-size: 13pt; margin: 16pt 0 5pt; padding-bottom: 2pt; border-bottom: 1.5pt solid #0b3d62; color: #0b3d62;
     page-break-after: avoid; }
h3 { font-size: 11pt; margin: 12pt 0 4pt; color: #0b3d62; page-break-after: avoid; }
h4 { font-size: 10.5pt; margin: 11pt 0 3pt; padding: 3pt 6pt; background: #eef3f8; border-left: 3pt solid #0b3d62;
     page-break-after: avoid; }
p { margin: 4pt 0; }
ul, ol { margin: 3pt 0 5pt; padding-left: 16pt; }
li { margin: 1.5pt 0; }
table { border-collapse: collapse; width: 100%; margin: 5pt 0 8pt; font-size: 8.6pt; }
th, td { border: 0.5pt solid #c8ced6; padding: 2.5pt 4pt; text-align: left; vertical-align: top; }
th { background: #eef3f8; font-weight: 600; }
tr { page-break-inside: avoid; }
tr:nth-child(even) td { background: #fafbfc; }
code { font: 8.8pt Menlo, monospace; background: #f2f2f2; padding: 0 2pt; }
a { color: #0b5cad; text-decoration: none; }
.pos { color: #1a7f37; white-space: nowrap; }
.neg { color: #c62828; white-space: nowrap; }
td a { white-space: nowrap; }
.note { color: #6e6e73; font-size: 8.5pt; }
"""

_INLINE = [
    (re.compile(r"\*\*(.+?)\*\*"), r"<strong>\1</strong>"),
    (re.compile(r"`([^`]+)`"), r"<code>\1</code>"),
    (re.compile(r"(?<![\w*])_([^_\n]+?)_(?![\w])"), r"<em>\1</em>"),
    (re.compile(r"(?<![\w*])\*([^*\n]+?)\*(?![\w*])"), r"<em>\1</em>"),
]
_MONEY = re.compile(r"(?<![\w])([+-])(₹\d(?:[\d,]*\d)?(?:\.\d+)?)")
_LINK = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")


def inline(text, link=None):
    """One line of Markdown as HTML: escaped, then bold, code, italics, links, and +₹/-₹ amounts in colour."""
    out, last = [], 0
    for m in _LINK.finditer(text):
        out.append(_inline_plain(text[last:m.start()]))
        href = link(m.group(2)) if link else m.group(2)
        out.append('<a href="%s">%s</a>' % (html.escape(href, quote=True), _inline_plain(m.group(1))))
        last = m.end()
    out.append(_inline_plain(text[last:]))
    return "".join(out)


def _inline_plain(text):
    s = html.escape(text, quote=False)
    for rx, rep in _INLINE:
        s = rx.sub(rep, s)
    return _MONEY.sub(lambda m: '<span class="%s">%s%s</span>' % ("pos" if m.group(1) == "+" else "neg",
                                                                  m.group(1), m.group(2)), s)


def _cells(line):
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|"):
        line = line[:-1]
    return [c.strip() for c in line.split("|")]


def md_to_html(md, title, link=None):
    """The journal's Markdown as a whole HTML page. link(href) rewrites each link (days/x.md -> x.pdf)."""
    lines = md.splitlines()
    body, para, i = [], [], 0

    def flush():
        if para:
            body.append("<p>%s</p>" % inline(" ".join(para), link))
            del para[:]

    while i < len(lines):
        line = lines[i]
        s = line.strip()
        h = re.match(r"^(#{1,6})\s+(.*)$", s)
        if not s:
            flush()
            i += 1
        elif h:
            flush()
            n = len(h.group(1))
            body.append("<h%d>%s</h%d>" % (n, inline(h.group(2), link), n))
            i += 1
        elif s.startswith("|"):
            flush()
            rows = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                rows.append(_cells(lines[i]))
                i += 1
            head = None
            if len(rows) >= 2 and all(re.match(r"^:?-{2,}:?$", c or "--") for c in rows[1]):
                head, rows = rows[0], rows[2:]
            t = ["<table>"]
            if head is not None and any(head):              # "| | |": a key-value table, no header row
                t.append("<thead><tr>%s</tr></thead>" % "".join("<th>%s</th>" % inline(c, link) for c in head))
            t.append("<tbody>")
            for r in rows:
                t.append("<tr>%s</tr>" % "".join("<td>%s</td>" % inline(c, link) for c in r))
            t.append("</tbody></table>")
            body.append("".join(t))
        elif re.match(r"^([-*]|\d+\.)\s+", s):
            flush()
            ordered = bool(re.match(r"^\d+\.", s))
            items = []
            while i < len(lines):
                t = lines[i].strip()
                m = re.match(r"^([-*]|\d+\.)\s+(.*)$", t)
                if m and bool(re.match(r"^\d+\.", t)) == ordered:
                    items.append(m.group(2))
                elif t and items and not re.match(r"^(#|\|)", t) and lines[i].startswith((" ", "\t")):
                    items[-1] += " " + t                      # a wrapped or nested line: kept with its item
                else:
                    break
                i += 1
            tag = "ol" if ordered else "ul"
            body.append("<%s>%s</%s>" % (tag, "".join("<li>%s</li>" % inline(x, link) for x in items), tag))
        else:
            para.append(s)
            i += 1
    flush()
    return ("<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\"><title>%s</title><style>%s</style>"
            "</head><body>%s</body></html>\n" % (html.escape(title), CSS, "\n".join(body)))


# ---------------------------------------------------------------- HTML -> PDF

def helper(home):
    """The compiled helper's path, compiling it first when it is missing or older than html2pdf.swift."""
    out = os.path.join(home, "tools", "html2pdf")
    if os.path.isfile(out) and os.path.getmtime(out) >= os.path.getmtime(SWIFT):
        return out
    swiftc = shutil.which("swiftc") or ("/usr/bin/swiftc" if os.path.exists("/usr/bin/swiftc") else None)
    if not swiftc:
        raise PdfError("no swiftc to build the PDF helper (install the Xcode command-line tools: xcode-select "
                       "--install)")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    tmp = out + ".building"
    try:
        p = subprocess.run([swiftc, "-O", "-o", tmp, SWIFT], capture_output=True, text=True,
                           timeout=COMPILE_TIMEOUT_S)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise PdfError("building the PDF helper failed: %s" % e)
    if p.returncode != 0 or not os.path.isfile(tmp):
        raise PdfError("building the PDF helper failed: %s" % (p.stderr or p.stdout).strip()[-300:])
    os.replace(tmp, out)
    return out


def render(home, html_path, pdf_path):
    """Print html_path to pdf_path (replaced only when the new one is good)."""
    exe = helper(home)
    tmp = pdf_path + ".printing.pdf"
    try:
        if os.path.exists(tmp):
            os.remove(tmp)
        p = subprocess.run([exe, os.path.abspath(html_path), os.path.abspath(tmp)], capture_output=True, text=True,
                           timeout=TIMEOUT_S)
    except subprocess.TimeoutExpired:
        _drop(tmp)
        raise PdfError("the PDF helper did not finish in %d s (is anyone logged in?)" % TIMEOUT_S)
    except OSError as e:
        _drop(tmp)
        raise PdfError("could not run the PDF helper: %s" % e)
    size = os.path.getsize(tmp) if os.path.isfile(tmp) else 0
    if p.returncode != 0 or not size or size > MAX_BYTES:
        _drop(tmp)
        raise PdfError("the PDF helper failed (exit %s, %d bytes): %s" % (
            p.returncode, size, (p.stderr or p.stdout or "").strip()[-200:]))
    with open(tmp, "rb") as fh:
        if fh.read(5) != b"%PDF-":
            _drop(tmp)
            raise PdfError("the PDF helper wrote something that is not a PDF")
    os.replace(tmp, pdf_path)


def _drop(path):
    try:
        os.remove(path)
    except OSError:
        pass
