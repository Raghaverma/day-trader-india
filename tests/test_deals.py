import datetime
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

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
        self.assertEqual(deals.parse_deals("", "block"), [])
        head = "Date,Symbol,Security Name,Client Name,Buy/Sell,Quantity Traded,Trade Price / Wght. Avg. Price\n"
        self.assertEqual(deals.parse_deals(head + "NO RECORDS\n", "block"), [])


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


if __name__ == "__main__":
    unittest.main()
