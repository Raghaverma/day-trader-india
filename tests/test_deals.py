import datetime
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from _load import FIX, load

deals = load("nse-deals", "deals")
DIR = os.path.join(FIX, "nse-deals")
BULK = os.path.join(DIR, "bulk-2026-11-13.csv")
BLOCK = os.path.join(DIR, "block-2026-11-13.csv")
NOW = "2026-11-14 10:00"  # Saturday: the files are Friday's


def text(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def latest():
    return deals.annotate(deals.parse_deals(text(BULK), "bulk") + deals.parse_deals(text(BLOCK), "block"))


def run(args):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = deals.main(args)
    return code, out.getvalue(), err.getvalue()


class Files(unittest.TestCase):
    def test_parse_bulk_and_block(self):
        bulk = deals.parse_deals(text(BULK), "bulk")
        block = deals.parse_deals(text(BLOCK), "block")
        self.assertEqual((len(bulk), len(block)), (136, 26))
        first = bulk[0]
        self.assertEqual((first["date"], first["symbol"], first["client"], first["side"], first["qty"], first["price"]),
                         ("2026-11-13", "AAATECH", "VAIBHAV RAJENDRA DOSHI", "BUY", 80083, 90.63))
        self.assertEqual(first["value"], round(80083 * 90.63, 2))
        self.assertEqual(first["remarks"], "")  # "-" in the file
        svf = [d for d in block if d["side"] == "SELL"][0]
        self.assertEqual((svf["symbol"], svf["qty"], svf["price"]), ("MEESHO", 76400000, 246.75))
        self.assertEqual(deals.money(svf["value"]), "₹1,885 cr")
        self.assertEqual(len({d["id"] for d in bulk + block}), 162)

    def test_identical_rows_are_two_trades(self):
        head = "Date,Symbol,Security Name,Client Name,Buy/Sell,Quantity Traded,Trade Price / Wght. Avg. Price\n"
        row = "13-NOV-2026,XYZ,Xyz Ltd,SOME FUND,BUY,100,10.00\n"
        got = deals.parse_deals(head + row + row, "block")
        self.assertEqual(len({d["id"] for d in got}), 2)
        again = deals.parse_deals(head + row + row, "block")
        self.assertEqual([d["id"] for d in got], [d["id"] for d in again])  # stable across runs

    def test_bad_files(self):
        with self.assertRaises(deals.DealsError):
            deals.parse_deals("Symbol,Price\nX,1\n", "bulk")
        head = "Date,Symbol,Security Name,Client Name,Buy/Sell,Quantity Traded,Trade Price / Wght. Avg. Price\n"
        self.assertEqual(deals.parse_deals(head + "NO RECORDS\n", "block"), [])
        self.assertEqual(deals.parse_deals(head + "No Records,,,,,,\n", "block"), [])
        self.assertEqual(deals.parse_deals(head, "block"), [])  # a header and nothing else: a quiet day

    def test_an_empty_body_or_a_web_page_is_not_a_day_with_no_deals(self):
        # was: parse_deals("") == [], so a failed download read as "no deals today"
        for body in ("", "   \n", "\ufeff"):
            with self.assertRaises(deals.DealsError) as cm:
                deals.parse_deals(body, "block")
            self.assertIn("came back empty", str(cm.exception))
        for body in ("<!DOCTYPE html><html>Access Denied</html>", "  <html>blocked</html>"):
            with self.assertRaises(deals.DealsError) as cm:
                deals.parse_deals(body, "bulk")
            self.assertIn("web page", str(cm.exception))

    def test_rows_that_cannot_be_read_are_counted_never_dropped_silently(self):
        head = "Date,Symbol,Security Name,Client Name,Buy/Sell,Quantity Traded,Trade Price / Wght. Avg. Price\n"
        good = "13-NOV-2026,XYZ,Xyz Ltd,SOME FUND,BUY,100,10.00\n"
        problems = []
        got = deals.parse_deals(head + good + "13-NOV-2026,ABC,Abc Ltd,CUT OFF\n"
                                "13-NOV-2026,DEF,Def Ltd,A FUND,BUY,lots,10.00\n"
                                "soon,GHI,Ghi Ltd,A FUND,SELL,5,1.00\n", "block", problems)
        self.assertEqual([d["symbol"] for d in got], ["XYZ"])
        self.assertEqual(len(problems), 1)
        self.assertIn("block.csv: 3 of 4 rows could not be read and are left out", problems[0])
        self.assertIn("CUT OFF", problems[0])
        # every row unreadable: the file failed, it is not "no deals"
        with self.assertRaises(deals.DealsError) as cm:
            deals.parse_deals(head + "13-NOV-2026,DEF,Def Ltd,A FUND,BUY,lots,10.00\n", "block")
        self.assertIn("none of the 1 rows in block.csv could be read", str(cm.exception))
        # the real files parse with nothing to report
        problems = []
        deals.parse_deals(text(BULK), "bulk", problems)
        self.assertEqual(problems, [])


class Clients(unittest.TestCase):
    def test_types_from_real_names(self):
        cases = {
            "HDFC MUTUAL FUND": "mutual fund", "ADITYA BIRLA SUN LIFE MUTUAL FUND": "mutual fund",
            "BAJAJ ALLIANZ LIFE INSURANCE COMPANY LIMITED": "insurance",
            "LIFE INSURANCE CORPORATION OF INDIA": "insurance",
            "SOCIETE GENERALE": "foreign fund", "GOVERNMENT OF SINGAPORE": "foreign fund",
            "MONETARY AUTHORITY OF SINGAPORE": "foreign fund", "SVF II MEERKAT (DE) LLC": "foreign fund",
            "MORGAN STANLEY ASIA SINGAPORE PTE": "foreign fund", "GHISALLO MASTER FUND LP": "foreign fund",
            "NORGES BANK ON ACCOUNT OF THE GOVERNMENT PENSION FUND GLOBAL": "foreign fund",
            "GOLDMAN SACHS BANK EUROPE SE": "foreign fund", "TEXAS MUNICIPAL RETIREMENT SYSTEM": "foreign fund",
            "STATE BANK OF INDIA": "bank", "ICICI BANK LIMITED": "bank",
            "QE SECURITIES LLP": "other", "DEVENDRA BANKATLAL RATHI": "other",
            "GRAVITON RESEARCH CAPITAL LLP": "other", "NEO TREASURY PLUS FUND": "other",
        }
        for name, want in cases.items():
            self.assertEqual(deals.classify(name), want, name)

    def test_counts_on_the_day(self):
        got = {}
        for d in latest():
            got[d["client_type"]] = got.get(d["client_type"], 0) + 1
        self.assertEqual(got, {"other": 135, "foreign fund": 22, "mutual fund": 4, "insurance": 1})


class Filters(unittest.TestCase):
    def setUp(self):
        self.d = latest()

    def test_bulk_row_that_repeats_a_block_deal_shows_once(self):
        svf = [d for d in self.d if d["client"] == "SVF II MEERKAT (DE) LLC"]
        self.assertEqual(sorted(d["kind"] for d in svf), ["block", "bulk"])
        self.assertTrue([d for d in svf if d["kind"] == "bulk"][0]["also_block"])
        self.assertTrue([d for d in svf if d["kind"] == "block"][0]["also_bulk"])
        shown = deals.select(self.d, symbols=["MEESHO"], side="SELL")
        self.assertEqual([d["kind"] for d in shown], ["block"])
        self.assertEqual([d["kind"] for d in deals.select(self.d, symbols=["MEESHO"], side="SELL", kinds={"bulk"})],
                         ["bulk"])

    def test_round_trips_hidden_unless_asked(self):
        self.assertEqual(sum(1 for d in self.d if d["round_trip"]), 118)
        both = [d for d in self.d if d["symbol"] == "BLSPOLY" and d["client"] == "ALPHAGREP SECURITIES PRIVATE LIMITED"]
        self.assertTrue(all(d["round_trip"] for d in both))
        self.assertEqual(deals.select(self.d, symbols=["BLSPOLY"]), [])
        self.assertEqual(len(deals.select(self.d, symbols=["BLSPOLY"], round_trips=True)), 36)

    def test_min_value_side_client_and_type(self):
        big = deals.select(self.d, min_value=deals.parse_value("25cr"))
        self.assertTrue(all(d["value"] >= 25e7 for d in big))
        self.assertEqual(big[0]["client"], "SVF II MEERKAT (DE) LLC")  # biggest first
        self.assertEqual(len(big), 21)
        buys = deals.select(self.d, side="buy", min_value=25e7)
        self.assertTrue(all(d["side"] == "BUY" for d in buys))
        mf = deals.select(self.d, clients=["mutual fund"])
        self.assertEqual([d["client"] for d in mf], ["MOTILAL OSWAL MUTUAL FUND", "ADITYA BIRLA SUN LIFE MUTUAL FUND",
                                                     "HDFC MUTUAL FUND", "EDELWEISS MUTUAL FUND"])
        followed = deals.select(self.d, clients=["  societe  generale ", "hdfc mutual"])
        self.assertEqual([d["client"] for d in followed], ["SOCIETE GENERALE", "HDFC MUTUAL FUND"])
        ins = deals.select(self.d, types={"insurance"})
        self.assertEqual([d["client"] for d in ins], ["BAJAJ ALLIANZ LIFE INSURANCE COMPANY LIMITED"])

    def test_value_in_rupees(self):
        self.assertEqual(deals.parse_value("25cr"), 25e7)
        self.assertEqual(deals.parse_value("₹25 crore"), 25e7)
        self.assertEqual(deals.parse_value("50L"), 50e5)
        self.assertEqual(deals.parse_value("1,00,00,000"), 1e7)
        with self.assertRaises(ValueError):
            deals.parse_value("lots")
        self.assertEqual(deals.money(1e7), "₹1.00 cr")
        self.assertEqual(deals.money(18e5), "₹18 lakh")

    def test_your_stocks(self):
        with tempfile.TemporaryDirectory() as tmp:
            pf, wl = os.path.join(tmp, "portfolio.json"), os.path.join(tmp, "watchlist.json")
            with open(pf, "w") as fh:
                json.dump({"cash": 1000, "positions": [{"ticker": "RALLIS", "shares": 10}]}, fh)
            with open(wl, "w") as fh:
                json.dump({"hits": [{"symbol": "MEESHO", "setups": ["orb"]}]}, fh)
            yours = deals.your_symbols(pf, wl)
        self.assertEqual(yours, {"RALLIS": "portfolio", "MEESHO": "watchlist"})
        mine = deals.select(self.d, mine=set(yours), min_value=1e7)
        self.assertEqual({d["symbol"] for d in mine}, {"RALLIS", "MEESHO"})
        rallis = [d for d in mine if d["symbol"] == "RALLIS"][0]
        line = deals.describe(rallis, yours, ["ask investment"])
        self.assertIn("RALLIS is in your portfolio", line)
        self.assertIn("a client you follow", line)
        self.assertIn("SELL    12,79,684 @ ₹189.42 = ₹24.24 cr  ASK INVESTMENT MANAGERS LIMITED [other]", line)


class Cli(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = os.path.join(self.tmp.name, "state", "deals.json")
        none = os.path.join(self.tmp.name, "none.json")
        self.base = ["--offline", DIR, "--state", self.state, "--now", NOW, "--config", none,
                     "--portfolio", none, "--watchlist", none]

    def tearDown(self):
        self.tmp.cleanup()

    def test_show_the_day(self):
        code, out, err = run(["show"] + self.base)
        self.assertEqual(code, 0, err)
        self.assertTrue(out.startswith("NSE bulk and block deals for 13 Nov 2026 (26 block, 136 bulk): 37 shown."))
        self.assertIn("BLOCK 2026-11-13  MEESHO       SELL  7,64,00,000 @ ₹246.75 = ₹1,885 cr  "
                      "SVF II MEERKAT (DE) LLC [foreign fund]", out)
        self.assertIn("also a bulk deal: over 0.5% of the company's shares", out)
        self.assertIn("Not inside information", out)
        self.assertIn("Not financial advice", out)
        self.assertNotIn("$", out)
        self.assertNotIn(chr(0x2014), out)
        with open(self.state, encoding="utf-8") as fh:
            saved = json.load(fh)
        self.assertEqual((len(saved["deals"]), saved["days"]), (162, ["2026-11-13"]))
        self.assertEqual(saved.get("alerted", {}), {})  # show does not alert

    def test_new_alerts_once_and_history_dedupes(self):
        code, out, err = run(["new", "--client", "mutual fund", "--json"] + self.base)
        self.assertEqual(code, 0, err)
        first = json.loads(out)
        self.assertEqual(len(first["deals"]), 4)
        self.assertEqual(first["summary"], {"mutual fund buy": round(5250000 * 246.75 + 4200000 * 246.75 +
                                                                     1410000 * 246.75 + 1015000 * 246.75, 2)})
        code, out, _ = run(["new", "--client", "mutual fund"] + self.base)
        self.assertEqual(code, 1)
        self.assertIn("Nothing new.", out)
        # the same files again (NSE has not published a new day): nothing doubles
        code, out, _ = run(["new", "--client", "societe generale"] + self.base)
        self.assertEqual(code, 0)
        self.assertIn("SOCIETE GENERALE [foreign fund]", out)
        with open(self.state, encoding="utf-8") as fh:
            saved = json.load(fh)
        self.assertEqual(len(saved["deals"]), 162)
        self.assertEqual(len(saved["alerted"]), 5)
        code, out, err = run(["history", "--days", "7", "--min-value", "0", "--type", "insurance",
                              "--state", self.state, "--now", NOW, "--config", self.base[5]])
        self.assertEqual(code, 0, err)
        self.assertIn("1 of 162 shown", out)
        self.assertIn("BAJAJ ALLIANZ LIFE INSURANCE COMPANY LIMITED [insurance]", out)

    def test_follow_clients_from_config(self):
        cfg = os.path.join(self.tmp.name, "config.json")
        with open(cfg, "w") as fh:
            json.dump({"follow_clients": ["Norges"], "deals_min_value_inr": 0}, fh)
        args = list(self.base)
        args[args.index("--config") + 1] = cfg
        code, out, err = run(["show", "--dry-run"] + args)
        self.assertEqual(code, 0, err)
        self.assertIn("1 shown. Filters: clients: Norges;", out)
        self.assertIn("<- a client you follow", out)
        code, out, _ = run(["show", "--dry-run", "--any-client", "--symbol", "IIFLCAPS"] + args)
        self.assertIn("3 shown", out)
        self.assertFalse(os.path.exists(self.state))

    def test_dry_run_and_stale_files(self):
        late = list(self.base)
        late[late.index("--now") + 1] = "2026-11-17 19:30"
        code, out, _ = run(["show", "--dry-run"] + late)
        self.assertEqual(code, 0)
        self.assertIn("note: NSE's files are for 2026-11-13; the last session was 2026-11-17.", out)
        self.assertFalse(os.path.exists(self.state))
        late[late.index("--now") + 1] = "2026-11-16 10:00"
        code, out, _ = run(["show", "--dry-run"] + late)
        self.assertNotIn("note:", out)  # Monday morning: Friday's files are the latest

    def test_errors_are_plain(self):
        with tempfile.TemporaryDirectory() as empty:
            code, out, err = run(["show", "--offline", empty, "--state", self.state, "--now", NOW,
                                  "--config", self.base[5]])
        self.assertEqual(code, 2)
        self.assertIn("deals: bulk deals: offline: no bulk*.csv", err)
        self.assertNotIn("Traceback", out + err)
        code, _, err = run(["show", "--min-value", "lots"] + self.base)
        self.assertEqual(code, 2)
        self.assertIn("--min-value wants rupees", err)
        code, _, _ = run([])
        self.assertEqual(code, 2)

    def test_explicit_files(self):
        code, out, err = run(["show", "--dry-run", "--bulk", BULK, "--block", BLOCK, "--kind", "bulk",
                              "--min-value", "50cr", "--state", self.state, "--now", NOW,
                              "--config", self.base[5]])
        self.assertEqual(code, 0, err)
        self.assertIn("(0 block, 136 bulk)", out)
        self.assertIn("BULK  2026-11-13  MEESHO       SELL", out)  # with block deals off, the bulk row shows
        self.assertIn("IIFLCAPS", out)

    def test_prev_session(self):
        self.assertEqual(deals.prev_session(datetime.date(2026, 11, 16)), datetime.date(2026, 11, 13))
        self.assertEqual(deals.prev_session(datetime.date(2026, 11, 25)), datetime.date(2026, 11, 23))


# ---------------------------------------------------------------- a file that failed is not "no deals"

HEAD = "Date,Symbol,Security Name,Client Name,Buy/Sell,Quantity Traded,Trade Price / Wght. Avg. Price\n"


class IsolatedHome(unittest.TestCase):
    """Own trader folder per test; the real trader/ is never read or written."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = os.path.join(self.tmp.name, "trader")
        os.makedirs(self.home)
        patcher = mock.patch.dict(os.environ, {"NSE_TRADER_HOME": self.home})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.state = os.path.join(self.tmp.name, "state", "deals.json")
        self.none = os.path.join(self.tmp.name, "none.json")

    def write(self, name, text_):
        path = os.path.join(self.tmp.name, name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text_)
        return path

    def saved(self):
        with open(self.state, encoding="utf-8") as fh:
            return json.load(fh)


class OneFileFails(IsolatedHome):
    def args(self, cmd, bulk=BULK, block=None, extra=()):
        return [cmd, "--bulk", bulk, "--block", block or BLOCK, "--state", self.state, "--now", NOW,
                "--config", self.none, "--portfolio", self.none, "--watchlist", self.none] + list(extra)

    def test_a_web_page_for_block_csv_says_so_and_exits_3_when_nothing_shows(self):
        page = self.write("block-blocked.csv", "<!DOCTYPE html><html>Access Denied</html>")
        code, out, err = run(self.args("show", block=page, extra=["--symbol", "NOSUCH"]))
        self.assertEqual(code, 3, err)  # nothing matched, but half the data is missing: not "no deals"
        self.assertIn("(block.csv NOT READ, 136 bulk)", out.splitlines()[0])
        self.assertIn("INCOMPLETE: block.csv could not be read, so this covers bulk deals only.", out)
        self.assertIn("note: block deals: NSE sent a web page, not block.csv", out)
        self.assertNotIn("0 block", out)
        code, out, _ = run(self.args("show", block=page))
        self.assertEqual(code, 0)  # bulk deals shown, with the warning on top
        self.assertIn("INCOMPLETE", out)

    def test_new_with_a_failed_file_does_not_say_nothing_new_or_move_since_forward(self):
        page = self.write("block-empty.csv", "")
        code, out, _ = run(self.args("new", block=page, extra=["--symbol", "NOSUCH"]))
        self.assertEqual(code, 3)
        self.assertIn("Nothing new in what could be read.", out)
        self.assertNotIn("\nNothing new.", out)
        self.assertNotIn("last_check", self.saved())  # "since" must not claim block.csv was checked
        # the file comes back: the first check really is the first
        code, out, _ = run(self.args("new", extra=["--symbol", "NOSUCH"]))
        self.assertEqual(code, 1)
        self.assertIn("Nothing new.", out)
        self.assertIn("since the first check", out)
        self.assertEqual(self.saved()["last_check"], "2026-11-14T04:30:00Z")

    def test_json_says_which_file_failed(self):
        page = self.write("bulk-cut.csv", "<html>blocked</html>")
        code, out, err = run(self.args("show", bulk=page, extra=["--json"]))
        doc = json.loads(out)
        self.assertEqual((doc["complete"], doc["failed"]), (False, ["bulk"]))
        self.assertTrue(any("bulk deals: NSE sent a web page" in n for n in doc["notes"]))

    def test_both_files_failing_is_exit_2_with_the_reasons(self):
        page = self.write("page.csv", "<html>blocked</html>")
        code, out, err = run(self.args("show", bulk=page, block=page))
        self.assertEqual(code, 2)
        self.assertIn("deals: bulk deals: NSE sent a web page", err)
        self.assertIn("deals: block deals: NSE sent a web page", err)
        self.assertFalse(os.path.exists(self.state))  # nothing remembered

    def test_a_truncated_row_is_a_note_in_the_report(self):
        cut = self.write("bulk-cut.csv", HEAD + "13-NOV-2026,XYZ,Xyz Ltd,SOME FUND,BUY,5000000,100.00\n"
                                         "13-NOV-2026,ABC,Abc Ltd,CUT OFF\n")
        code, out, err = run(self.args("show", bulk=cut, extra=["--min-value", "0"]))
        self.assertEqual(code, 0, err)
        self.assertIn("note: bulk.csv: 1 of 2 rows could not be read and are left out", out)

    def test_an_empty_day_with_both_files_read_is_still_exit_1(self):
        empty = self.write("empty.csv", HEAD)
        code, out, err = run(self.args("new", bulk=empty, block=empty))
        self.assertEqual(code, 1, err)
        self.assertIn("Nothing new.", out)
        self.assertNotIn("INCOMPLETE", out)


class Since(IsolatedHome):
    def test_since_is_in_ist_not_utc(self):
        args = ["new", "--offline", DIR, "--state", self.state, "--config", self.none, "--portfolio", self.none,
                "--watchlist", self.none]
        run(args + ["--now", "2026-11-14 10:00"])
        code, out, _ = run(args + ["--now", "2026-11-14 11:00"])
        self.assertEqual(code, 1)
        self.assertIn("New NSE bulk and block deals since 2026-11-14 10:00 IST: 0", out)  # was "2026-11-14T04:30:00Z"


class ConfigAndPaths(IsolatedHome):
    def base(self, cmd="show"):
        return [cmd, "--offline", DIR, "--now", NOW]

    def test_state_and_config_default_to_the_trader_home(self):
        elsewhere = tempfile.TemporaryDirectory()
        self.addCleanup(elsewhere.cleanup)
        with open(os.path.join(self.home, "config.json"), "w") as fh:
            json.dump({"follow_clients": ["Norges"], "deals_min_value_inr": 0}, fh)
        cwd = os.getcwd()
        os.chdir(elsewhere.name)
        try:
            code, out, err = run(self.base())
        finally:
            os.chdir(cwd)
        self.assertEqual(code, 0, err)
        self.assertIn("clients: Norges", out)  # the config in $NSE_TRADER_HOME was found
        self.assertIn("deals: config " + os.path.join(self.home, "config.json"), err)
        self.assertTrue(os.path.isfile(os.path.join(self.home, "state", "deals.json")))
        self.assertFalse(os.path.exists(os.path.join(elsewhere.name, "trader")))

    def test_a_missing_config_says_the_defaults_are_in_use(self):
        code, out, err = run(self.base() + ["--dry-run"])
        self.assertEqual(code, 0, err)
        cfg = os.path.join(self.home, "config.json")
        self.assertIn("deals: no config at %s: built-in defaults (no follow list, ₹1.00 cr minimum)" % cfg, err)
        self.assertNotIn("config", out)  # stdout stays the report
        code, out, err = run(self.base() + ["--dry-run", "--json"])
        self.assertEqual(err, "")
        self.assertIn("built-in defaults", json.loads(out)["config"])

    def test_a_broken_config_names_the_file(self):
        cfg = self.write("config.json", "{ nope")
        code, _, err = run(self.base() + ["--config", cfg])
        self.assertEqual(code, 2)
        self.assertIn(cfg, err)
        self.assertNotIn("Traceback", err)
        cfg = self.write("config2.json", json.dumps({"deals_min_value_inr": "a lot"}))
        code, _, err = run(self.base() + ["--config", cfg])
        self.assertEqual(code, 2)
        self.assertIn('"deals_min_value_inr"', err)

    def test_follow_clients_as_one_string_is_one_name_not_letters(self):
        cfg = self.write("config.json", json.dumps({"follow_clients": "Norges", "deals_min_value_inr": 0}))
        code, out, err = run(self.base() + ["--config", cfg, "--dry-run"])
        self.assertEqual(code, 0, err)
        self.assertIn("1 shown. Filters: clients: Norges;", out)
        cfg = self.write("config3.json", json.dumps({"follow_clients": {"a": 1}}))
        code, _, err = run(self.base() + ["--config", cfg, "--dry-run"])
        self.assertEqual(code, 2)
        self.assertIn("follow_clients", err)

    def test_a_broken_state_file_stops_the_run_it_is_not_reset(self):
        for body in ("{ half", "[1, 2]"):
            with open(self.write("state.json", body), encoding="utf-8"):
                pass
            code, _, err = run(self.base() + ["--state", os.path.join(self.tmp.name, "state.json"),
                                              "--config", self.none])
            self.assertEqual(code, 2)
            self.assertIn("state.json is not", err)
            self.assertNotIn("Traceback", err)
        with open(os.path.join(self.tmp.name, "state.json"), encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "[1, 2]")  # left alone for the user to look at

    def test_an_unreadable_portfolio_is_a_note_not_an_empty_your_stocks(self):
        pf = self.write("portfolio.json", "{ broken")
        problems = []
        self.assertEqual(deals.your_symbols(pf, self.none, problems), {})
        self.assertIn("could not read %s, so your portfolio is not flagged" % pf, problems[0])
        code, out, _ = run(self.base() + ["--portfolio", pf, "--mine", "--dry-run"])
        self.assertIn("note: could not read %s" % pf, out)


class HolidaysAreRefreshed(IsolatedHome):
    """The "are NSE's files the latest session's?" check is a trading-day decision: it uses
    NSE's own holiday list (refreshed when the year is unknown), not only the built-in 2026 one."""

    def master(self):
        return {"CM": [{"tradingDate": "26-Jan-2027", "description": "Republic Day"}]}

    def run_online(self, fetch):
        csv_day = HEAD + "25-JAN-2027,XYZ,Xyz Ltd,SOME FUND,BUY,5000000,100.00\n"
        with mock.patch.object(deals.Source, "text", lambda self, kind: csv_day), \
                mock.patch.object(deals, "HOLIDAY_FETCH", fetch):
            return run(["show", "--state", self.state, "--now", "2027-01-27 10:00", "--config", self.none,
                        "--portfolio", self.none, "--watchlist", self.none, "--min-value", "0", "--dry-run"])

    def test_republic_day_is_not_mistaken_for_a_session_with_missing_files(self):
        # Wed 27 Jan 2027, 10:00 IST: yesterday (Tue 26 Jan) was a holiday, so Monday 25 Jan's files are
        # the latest. With only the built-in 2026 list the tool called them "not updated yet".
        code, out, err = self.run_online(lambda: self.master())
        self.assertEqual(code, 0, err)
        self.assertNotIn("not updated yet", out)
        self.assertNotIn("holiday list", out)
        cache = os.path.join(self.tmp.name, "state", "nse-holidays.json")
        with open(cache, encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["holidays"], {"2027-01-26": "Republic Day"})

    def test_when_the_holiday_list_cannot_be_refreshed_it_says_so(self):
        def down():
            raise deals.nc.HolidayError("could not reach NSE: timed out")

        code, out, err = self.run_online(down)
        self.assertEqual(code, 0, err)
        self.assertIn("note: could not refresh NSE's holiday list", out)
        self.assertIn("note: NSE's 2027 holiday list is not loaded", out)

    def test_offline_runs_never_fetch_holidays(self):
        def boom():
            raise AssertionError("an offline run must not touch the network")

        with mock.patch.object(deals, "HOLIDAY_FETCH", boom):
            code, out, err = run(["show", "--offline", DIR, "--state", self.state, "--now", NOW, "--config",
                                  self.none, "--portfolio", self.none, "--watchlist", self.none, "--dry-run"])
        self.assertEqual(code, 0, err)


if __name__ == "__main__":
    unittest.main()
