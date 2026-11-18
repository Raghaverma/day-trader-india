import hashlib
import json
import os
import re
import unittest

from _load import ROOT

SKILLS = ["nse-scanner", "nse-news", "nse-entry", "nse-exit", "nse-timemachine", "nse-risk",
          "nse-budget", "nse-broker", "nse-alerts", "nse-analyst", "nse-deals"]
ABOUT = ("11 Claude skills for day trading Indian stocks on the NSE: scan the Nifty for breakouts, read "
         "exchange filings, time every entry and exit, and replay every outcome before you risk a rupee.")
# skills on the way to an order carry the safety block
ORDER_PATH = ["nse-scanner", "nse-entry", "nse-exit", "nse-timemachine", "nse-budget", "nse-deals",
              "nse-risk", "nse-broker", "nse-alerts"]
SCRIPTS = {"nse-scanner": "scan.py", "nse-news": "news.py", "nse-entry": "entry.py", "nse-exit": "exit.py",
           "nse-timemachine": "timemachine.py", "nse-risk": "check.py", "nse-budget": "size.py",
           "nse-broker": "ticket.py", "nse-alerts": "alerts.py", "nse-analyst": "results.py",
           "nse-deals": "deals.py"}


def frontmatter(path):
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    return (m.group(1) if m else ""), text


def walk(exts):
    for base, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in (".git", "__pycache__", "fixtures")]
        for f in files:
            if f.endswith(exts):
                yield os.path.join(base, f)


class Repo(unittest.TestCase):
    def test_eleven_skill_folders_at_the_root(self):
        found = sorted(d for d in os.listdir(ROOT) if os.path.isfile(os.path.join(ROOT, d, "SKILL.md")))
        self.assertEqual(found, sorted(SKILLS))

    def test_root_layout(self):
        # trader/ and __pycache__/ are .gitignored: a local trader folder from using the skills is not the layout
        root = sorted(d for d in os.listdir(ROOT)
                      if not d.startswith((".git", ".DS_Store")) and d not in ("trader", "__pycache__"))
        self.assertEqual(root, sorted([".claude-plugin", "CHANGELOG.md", "LICENSE", "README.md", "PORTING.md", "autopilot", "hooks",
                                      "tests"] + SKILLS))

    def test_every_skill_has_its_script(self):
        for s, script in SCRIPTS.items():
            self.assertTrue(os.path.isfile(os.path.join(ROOT, s, script)), "%s/%s" % (s, script))

    def test_nse_common_copies_are_identical(self):
        digests = {}
        for s in SKILLS:
            with open(os.path.join(ROOT, s, "nse_common.py"), "rb") as fh:
                digests[s] = hashlib.sha256(fh.read()).hexdigest()
        self.assertEqual(len(set(digests.values())), 1,
                         "nse_common.py differs: %s" % sorted(digests.items(), key=lambda kv: kv[1]))

    def test_frontmatter_name_and_description(self):
        for s in SKILLS:
            fm, text = frontmatter(os.path.join(ROOT, s, "SKILL.md"))
            self.assertIn("name: %s\n" % s, fm + "\n", s)
            self.assertRegex(fm, r"description: ", s)
            self.assertGreater(len(fm), 200, "%s description is too thin" % s)
            self.assertGreater(len(text.splitlines()), 60, s)

    def test_descriptions_stay_short_enough_for_the_skill_list(self):
        # a long description can push a skill out of the session's skill list (nse-timemachine once
        # showed up with none), so keep each one well under the 1,024-character limit
        for s in SKILLS:
            fm, _ = frontmatter(os.path.join(ROOT, s, "SKILL.md"))
            m = re.search(r"^description: >-?\n((?:  .*\n?)+)", fm + "\n", re.M)
            self.assertIsNotNone(m, "%s has no folded description" % s)
            text = " ".join(m.group(1).split())
            self.assertLessEqual(len(text), 700, "%s description is %d characters" % (s, len(text)))
            self.assertGreater(len(text), 150, s)

    def test_hooks_are_wired_and_compile(self):
        with open(os.path.join(ROOT, "hooks", "hooks.json"), encoding="utf-8") as fh:
            doc = json.load(fh)
        events = doc.get("hooks") or doc
        for event in ("PreToolUse", "UserPromptSubmit", "SessionStart"):
            self.assertIn(event, events, event)
        blob = json.dumps(events)
        self.assertIn("place_order", blob)
        self.assertIn("${CLAUDE_PLUGIN_ROOT}", blob)
        for f in os.listdir(os.path.join(ROOT, "hooks")):
            if f.endswith(".py"):
                with open(os.path.join(ROOT, "hooks", f), encoding="utf-8") as fh:
                    compile(fh.read(), f, "exec")

    def test_order_path_skills_carry_the_safety_rule(self):
        for s in ORDER_PATH:
            _, text = frontmatter(os.path.join(ROOT, s, "SKILL.md"))
            low = " ".join(text.lower().split())
            self.assertIn("not financial advice", low, s)
            self.assertIn("yes", low, s)

    def test_no_us_leftovers_in_skills(self):
        banned = re.compile(r"\$\d|\bdollars?\b|_usd\b|\bAlpaca\b|\bSEC EDGAR\b|America/New_York|\bNYSE\b|\bET\b")
        for s in SKILLS:
            for f in os.listdir(os.path.join(ROOT, s)):
                if not f.endswith((".py", ".md", ".json")) or f == "nse_common.py":
                    continue
                with open(os.path.join(ROOT, s, f), encoding="utf-8") as fh:
                    for i, line in enumerate(fh, 1):
                        m = banned.search(line)
                        if m and "original" not in line.lower() and not re.search(r"\bUS\b", line):
                            self.fail("%s/%s:%d still says %r: %s" % (s, f, i, m.group(0), line.strip()[:120]))

    def test_requests_carry_no_personal_details(self):
        pat = re.compile(r"[\w.+-]+@[\w-]+\.[a-z]{2,}\b", re.I)
        for path in walk((".py",)):
            if os.path.basename(path) == "test_repo.py":
                continue
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
            hit = pat.search(text)
            self.assertIsNone(hit, "%s contains %r" % (os.path.relpath(path, ROOT), hit and hit.group(0)))

    def test_plugin_manifest_points_at_the_root_skills(self):
        with open(os.path.join(ROOT, ".claude-plugin", "plugin.json")) as fh:
            plugin = json.load(fh)
        self.assertEqual(plugin["name"], "day-trader-india")
        self.assertEqual(plugin["skills"], "./")
        self.assertEqual(plugin["description"], ABOUT)
        with open(os.path.join(ROOT, ".claude-plugin", "marketplace.json")) as fh:
            market = json.load(fh)
        self.assertEqual(market["name"], "day-trader-india")
        self.assertEqual(market["plugins"][0]["name"], "day-trader-india")
        self.assertEqual(market["plugins"][0]["source"], "./")

    def test_example_json_files_parse(self):
        for path in walk((".json",)):
            with open(path, encoding="utf-8") as fh:
                json.load(fh)

    def test_readme(self):
        with open(os.path.join(ROOT, "README.md"), encoding="utf-8") as fh:
            readme = " ".join(fh.read().split())
        self.assertTrue(readme.startswith("# The Day Trader skill, India edition"))
        for line in (ABOUT, "## Fine print", "Not financial advice", "Jakeschincariol/day-trader-skill"):
            self.assertIn(line, readme)
        for s in SKILLS:
            self.assertIn("/day-trader-india:%s" % s, readme, s)

    def test_license_keeps_the_original_copyright(self):
        with open(os.path.join(ROOT, "LICENSE"), encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn("MIT License", text)
        self.assertIn("Jake Schincariol", text)


if __name__ == "__main__":
    unittest.main()
