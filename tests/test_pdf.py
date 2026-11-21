"""autopilot/pdf.py: the journal's Markdown as an HTML page (the PDF itself needs macOS WebKit and a logged-in
session, so render() is only checked for how it reports a failure)."""

import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

from _load import ROOT

sys.path.insert(0, os.path.join(ROOT, "autopilot"))
import pdf  # noqa: E402


class Html(unittest.TestCase):
    def test_the_journals_markdown(self):
        md = ("# 2026-11-20: +₹6,201.91\n\nA line\nwrapped.\n\n## The day\n\n| | |\n|---|---|\n| Net | **+₹6,201.91** |\n\n"
              "| Stock | Net |\n|---|---|\n| ADANIPORTS | -₹428.04 |\n\n1. **First:** one\n2. Second\n\n- a\n- b, "
              "gross +₹4,849, then\n\n[2026-11-20](days/2026-11-20.md) entry_ttl_minutes <b>")
        out = pdf.md_to_html(md, "T", link=lambda h: h.replace("days/", "").replace(".md", ".pdf"))
        self.assertIn('<h1>2026-11-20: <span class="pos">+₹6,201.91</span></h1>', out)
        self.assertIn("<p>A line wrapped.</p>", out)
        self.assertIn("<table><tbody><tr><td>Net</td>", out)                  # "| | |": no header row
        self.assertIn("<thead><tr><th>Stock</th><th>Net</th></tr></thead>", out)
        self.assertIn('<td><span class="neg">-₹428.04</span></td>', out)
        self.assertIn("<ol><li><strong>First:</strong> one</li><li>Second</li></ol>", out)
        self.assertIn('gross <span class="pos">+₹4,849</span>, then', out)   # the comma is not money
        self.assertIn('<a href="2026-11-20.pdf">2026-11-20</a>', out)
        self.assertIn("entry_ttl_minutes &lt;b&gt;", out)                    # escaped; underscores are not italics
        self.assertTrue(out.startswith("<!doctype html>"))


class Render(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.d)
        self.html = os.path.join(self.d, "p.html")
        with open(self.html, "w") as fh:
            fh.write("<p>x</p>")
        self.exe = os.path.join(self.d, "tools", "html2pdf")
        os.makedirs(os.path.dirname(self.exe))
        with open(self.exe, "w") as fh:
            fh.write("")
        os.utime(self.exe, (os.path.getmtime(pdf.SWIFT) + 10,) * 2)        # newer than the source: no build

    def run_helper(self, code, write=None):
        def fake(cmd, **kw):
            if write is not None:
                with open(cmd[2], "wb") as fh:
                    fh.write(write)
            return mock.Mock(returncode=code, stdout="", stderr="boom")
        return mock.patch("pdf.subprocess.run", side_effect=fake)

    def test_a_good_pdf_replaces_the_old_one(self):
        out = os.path.join(self.d, "a.pdf")
        with self.run_helper(0, b"%PDF-1.7 ok"):
            pdf.render(self.d, self.html, out)
        with open(out, "rb") as fh:
            self.assertEqual(fh.read(), b"%PDF-1.7 ok")

    def test_failures_leave_the_old_pdf_and_say_why(self):
        out = os.path.join(self.d, "a.pdf")
        with open(out, "wb") as fh:
            fh.write(b"%PDF-old")
        for code, data, words in ((1, b"", "exit 1"), (0, b"<html>", "not a PDF"), (0, None, "0 bytes")):
            with self.run_helper(code, data):
                with self.assertRaises(pdf.PdfError) as e:
                    pdf.render(self.d, self.html, out)
            self.assertIn(words, str(e.exception))
            with open(out, "rb") as fh:
                self.assertEqual(fh.read(), b"%PDF-old")
            self.assertFalse(os.path.exists(out + ".printing.pdf"))

    def test_a_hung_helper_times_out(self):
        import subprocess
        with mock.patch("pdf.subprocess.run", side_effect=subprocess.TimeoutExpired("x", 90)):
            with self.assertRaises(pdf.PdfError) as e:
                pdf.render(self.d, self.html, os.path.join(self.d, "a.pdf"))
        self.assertIn("did not finish", str(e.exception))

    def test_no_swiftc_says_how_to_get_it(self):
        os.remove(self.exe)
        with mock.patch("pdf.shutil.which", return_value=None), mock.patch("pdf.os.path.exists", return_value=False):
            with self.assertRaises(pdf.PdfError) as e:
                pdf.helper(self.d)
        self.assertIn("xcode-select --install", str(e.exception))


if __name__ == "__main__":
    unittest.main()
