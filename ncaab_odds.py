#!/usr/bin/env python3
"""
NCAAB opening + closing line scraper (OddsShark -> Covers.com).

OddsShark's NCAAB odds page now redirects to Covers.com, so this scrapes:
    https://www.covers.com/sport/basketball/ncaab/odds

Lines
-----
OPENING  - Covers' "Open" column (posted by a high-limit international book).
CLOSING  - the average of the sharp books below, frozen at the last snapshot
           taken before tip-off. Covers switches to live odds once a game starts,
           so the script snapshots repeatedly and locks each game when it goes LIVE.

Sharp-book average: the line is the number most of those books are dealing
(median if they split); the price is the average price at that number.
If none of the sharp books has a market posted, it falls back to every book on
the board and marks the source as "all books".

Usage
-----
    python ncaab_odds.py watch               # snapshot every 10 min until today's games have all tipped
    python ncaab_odds.py watch --interval 5
    python ncaab_odds.py run                 # one snapshot (if you'd rather schedule it yourself)
    python ncaab_odds.py dashboard           # open the dashboard in your browser
    python ncaab_odds.py show                # today's opening vs closing lines
    python ncaab_odds.py show --date 2026-11-02
    python ncaab_odds.py export --date 2026-11-02          # CSV
    python ncaab_odds.py run --books "FanDuel,DraftKings"  # override the sharp-book list

Data is stored in ncaab_odds.db (SQLite) next to this file.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import re
import sqlite3
import statistics
import sys
import time
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path


def _ensure_packages() -> None:
    """Install missing libraries automatically on first run."""
    import importlib.util
    import subprocess
    wanted = [("requests", "requests"), ("bs4", "beautifulsoup4")]
    if sys.platform == "win32":  # Windows has no built-in time-zone database
        wanted.append(("tzdata", "tzdata"))
    need = [pkg for mod, pkg in wanted if importlib.util.find_spec(mod) is None]
    if need:
        print(f"First-time setup: installing {', '.join(need)} ...")
        subprocess.check_call([sys.executable, "-m", "pip", "install", "--quiet", *need])
        print("Done.\n")


try:
    _ensure_packages()
except Exception as _e:
    print(f"[error] couldn't install required libraries: {_e}\n"
          "Open Command Prompt and run:  py -m pip install requests beautifulsoup4 tzdata")
    input("\nPress Enter to close...")
    sys.exit(1)

from zoneinfo import ZoneInfo  # noqa: E402

import requests  # noqa: E402
from bs4 import BeautifulSoup  # noqa: E402

URL = "https://www.covers.com/sport/basketball/ncaab/odds"
ET = ZoneInfo("America/New_York")
DB_PATH = Path(__file__).with_name("ncaab_odds.db")
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

# Books averaged for the closing line, whichever of them appear on the board.
# Edit this list to taste. Market makers first; sharp offshore books are used
# automatically if Covers ever lists them for your region.
SHARP_BOOKS = ["Pinnacle", "Circa", "Bookmaker", "BetOnline",
               "FanDuel", "DraftKings", "bet365"]

# Team notes Google Sheet (must be shared as "Anyone with the link - Viewer").
# Columns: team | covers_abbr | aliases | notes | last_updated
NOTES_SHEET_ID = "1bO_LcZ0M0WbRwQkV9N2wf15krG7Xpl5cRX-872a-A4M"
NOTES_URL = f"https://docs.google.com/spreadsheets/d/{NOTES_SHEET_ID}/export?format=csv"
NOTES_CACHE = Path(__file__).with_name("notes_cache.csv")
HABITS_URL = f"https://docs.google.com/spreadsheets/d/{NOTES_SHEET_ID}/export?format=csv&gid=2002"
HABITS_CACHE = Path(__file__).with_name("habits_cache.csv")
LOG_PATH = Path(__file__).with_name("tracker_log.txt")


def log(msg: str) -> None:
    """Print a line and append it to tracker_log.txt."""
    print(msg)
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"{datetime.now(ET):%Y-%m-%d %H:%M:%S} {msg.strip()}\n")
    except OSError:
        pass

NUM = r"[+-]?\d+(?:\.\d+)?"
MONTHS = {m: i for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], 1)}


# --------------------------------------------------------------------------- fetch
def fetch_html(use_browser: bool = False) -> str:
    if not use_browser:
        problem = ""
        for attempt in range(3):
            try:
                r = requests.get(URL, headers=HEADERS, timeout=90)
                if r.status_code == 200 and "oddsGameRow" in r.text:
                    return r.text
                problem = f"Covers returned status {r.status_code}"
            except requests.RequestException as e:
                problem = f"download failed ({e.__class__.__name__})"
            log(f"[warn] {problem}; retrying ({attempt + 1}/3)")
            time.sleep(20)
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise RuntimeError("couldn't download the Covers odds page; will try again next snapshot")
    with sync_playwright() as p:
        b = p.chromium.launch()
        page = b.new_page(user_agent=HEADERS["User-Agent"])
        page.goto(URL, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_selector("tr.oddsGameRow", timeout=30000)
        html = page.content()
        b.close()
    return html


# --------------------------------------------------------------------------- parse
def _text(el) -> str:
    return re.sub(r"\s+", " ", el.get_text(" ", strip=True)) if el else ""


def _first_num(el) -> float | None:
    t = _text(el)
    if re.match(r"(PK|pk)\b", t):
        return 0.0
    m = re.search(NUM, t)
    return float(m.group()) if m else None


def _cell_values(cell, market: str):
    """Return (line, american_price) from a book's away/home cell."""
    t = _text(cell)
    if not t:
        return None, None
    if market == "moneyline":
        m = re.match(rf"({NUM})", t)
        return None, (int(float(m.group(1))) if m else None)
    if market == "total":
        m = re.match(rf"[ou]\s*({NUM})\s+({NUM})", t)
    else:  # spread
        m = re.match(rf"({NUM}|PK|pk)\s+({NUM})", t)
    if not m:
        return None, None
    line = 0.0 if m.group(1).lower() == "pk" else float(m.group(1))
    return line, int(float(m.group(2)))


def _parse_tip(label: str, now: datetime) -> str | None:
    """'Today, 19:30' / 'Nov 3, 22:00' (ET) -> ISO datetime string."""
    m = re.match(r"(Today|Tomorrow|([A-Z][a-z]{2}) (\d{1,2})),\s*(\d{1,2}):(\d{2})", label)
    if not m:
        return None
    hh, mm = int(m.group(4)), int(m.group(5))
    if m.group(1) == "Today":
        d = now.date()
    elif m.group(1) == "Tomorrow":
        d = (now + timedelta(days=1)).date()
    else:
        mon, day = MONTHS[m.group(2)], int(m.group(3))
        year = now.year + (1 if mon < now.month - 6 else 0)
        d = datetime(year, mon, day).date()
    return datetime(d.year, d.month, d.day, hh, mm, tzinfo=ET).isoformat()


REJECTED: list[dict] = []  # quotes thrown out by the last parse_board() call

# Prediction markets / exchanges list contract prices in cents and odd alternate lines;
# they are not sportsbook lines and are never used.
BLOCKED_BOOKS = ("kalshi", "polymarket", "novig", "prophetx", "prophet exchange", "sporttrade",
                 "betfair exchange", "crypto.com")


def _blocked_book(name: str) -> bool:
    n = (name or "").lower()
    return any(b in n for b in BLOCKED_BOOKS)


def _real_market(a: int, b: int) -> bool:
    """A two-sided price a book could actually offer: the implied probabilities add up to
    roughly 100% plus a normal margin. -110/-110 = 104.8%; -190/-190 = 131% is not real."""
    try:
        tot = 1 / _am_to_dec(a) + 1 / _am_to_dec(b)
    except ZeroDivisionError:
        return False
    return 0.97 <= tot <= 1.12


def parse_board(html: str, now: datetime | None = None) -> dict[str, dict]:
    REJECTED.clear()
    now = now or datetime.now(ET)
    soup = BeautifulSoup(html, "html.parser")
    games: dict[str, dict] = {}

    for market, table_id in (("moneyline", "moneyline-table"),
                             ("spread", "spread-table"),
                             ("total", "total-table")):
        table = soup.find("table", id=table_id)
        if not table:
            print(f"[warn] #{table_id} not found - page layout may have changed", file=sys.stderr)
            continue
        for row in table.select("tbody tr.oddsGameRow"):
            book_tds = [td for td in row.find_all("td", recursive=False)
                        if td.get("data-book") and not _blocked_book(td.get("data-book"))]
            gid = next((td.get("data-game") for td in book_tds if td.get("data-game")), None)
            if not gid:
                link = row.select_one(".teams-div a[href*='/matchup/']")
                gid = link["href"].rstrip("/").split("/")[-1].split("?")[0] if link else None
            if not gid:
                continue

            g = games.setdefault(gid, {"game_id": gid, "books": {}, "open": {}})
            if "away" not in g:
                teams = row.select(".teams-div strong")
                if len(teams) < 2:
                    continue
                gt = row.select_one(".game-time")
                label = _text(gt)
                started = bool(gt and "live-status" in gt.get("class", [])) or bool(
                    re.match(r"(LIVE|FINAL|End|Final|Half)", label))
                g.update({
                    "away": _text(teams[0]),
                    "home": _text(teams[1]),
                    "away_rank": row.get("data-away-team-rank"),
                    "home_rank": row.get("data-home-team-rank"),
                    "away_conf": row.get("data-away-conference"),
                    "home_conf": row.get("data-home-conference"),
                    "status": ("final" if re.match(r"FINAL", label, re.I) else "live") if started else "pre",
                    "tip_et": None if started else _parse_tip(label, now),
                })

            op = row.select_one(".opening-lines-div")
            if op:
                a, h = _first_num(op.select_one(".away-cell")), _first_num(op.select_one(".home-cell"))
                if market == "moneyline":
                    g["open"]["away_ml"] = int(a) if a is not None else None
                    g["open"]["home_ml"] = int(h) if h is not None else None
                elif market == "spread":
                    g["open"]["away_spread"] = a
                    g["open"]["home_spread"] = -a if a is not None else h
                else:
                    g["open"]["total"] = a if a is not None else h

            for td in book_tds:
                a_line, a_px = _cell_values(td.select_one(".away-cell"), market)
                h_line, h_px = _cell_values(td.select_one(".home-cell"), market)
                if a_px is None or h_px is None:
                    continue
                if not _real_market(a_px, h_px):
                    # e.g. -190/-190: not a real two-sided price (Covers sometimes serves a
                    # page variant whose cells don't hold the book's current line)
                    REJECTED.append({"game": gid, "book": td.get("data-book"), "market": market,
                                     "away": a_px, "home": h_px, "html": str(td)[:4000]})
                    continue
                g["books"].setdefault(td["data-book"], {})[market] = {
                    "line": a_line, "away": a_px, "home": h_px}
    return {k: v for k, v in games.items() if "away" in v}


# --------------------------------------------------------------------------- sharp average
def _am_to_dec(a: int) -> float:
    return 1 + (a / 100 if a > 0 else 100 / -a)


def _dec_to_am(d: float) -> int:
    return round((d - 1) * 100) if d >= 2 else round(-100 / (d - 1))


def _avg_price(prices: list[int]) -> int | None:
    return _dec_to_am(statistics.fmean(_am_to_dec(p) for p in prices)) if prices else None


def _consensus_line(lines: list[float]) -> float:
    counts = Counter(lines).most_common()
    if len(counts) == 1 or counts[0][1] > counts[1][1]:
        return counts[0][0]
    return round(statistics.median(lines) * 2) / 2  # split books -> median, nearest half point


def _juice_gap(q: dict) -> float:
    """How far a two-sided price is from even money: 0 for -110/-110 (or +100/+100),
    large for -200/+160. Uses the gap between the two sides' decimal odds."""
    return abs(_am_to_dec(q["away"]) - _am_to_dec(q["home"]))


def _even_money_line(quotes: list[dict]) -> float:
    """Pick the spread/total number priced closest to even money, so a stale number hung
    at -200 doesn't win just because two books still show it. Ties go to the number more
    books are on, then to the middle number."""
    by_line: dict[float, list[dict]] = {}
    for q in quotes:
        by_line.setdefault(q["line"], []).append(q)
    ranked = sorted(by_line.items(),
                    key=lambda kv: (round(min(_juice_gap(q) for q in kv[1]), 2), -len(kv[1])))
    best_gap = round(min(_juice_gap(q) for q in ranked[0][1]), 2)
    tied = [ln for ln, qs in ranked
            if round(min(_juice_gap(q) for q in qs), 2) == best_gap and len(qs) == len(ranked[0][1])]
    if len(tied) == 1:
        return tied[0]
    return round(statistics.median(tied) * 2) / 2


def _market_avg(books: dict, market: str, sharp: list[str]):
    """Average one market over the sharp books; fall back to all books. Returns (dict, source)."""
    sharp_l = {b.lower() for b in sharp}
    for pool, label in ((lambda b: b.lower() in sharp_l, None), (lambda b: True, "all books")):
        quotes = {b: v[market] for b, v in books.items() if pool(b) and market in v}
        if market != "moneyline":
            quotes = {b: q for b, q in quotes.items() if q["line"] is not None}
        if not quotes:
            continue
        src = label or ",".join(sorted(quotes))
        if market == "moneyline":
            return {"away": _avg_price([q["away"] for q in quotes.values()]),
                    "home": _avg_price([q["home"] for q in quotes.values()])}, src
        line = _even_money_line(list(quotes.values()))
        at = [q for q in quotes.values() if q["line"] == line] or list(quotes.values())
        return {"line": line,
                "away": _avg_price([q["away"] for q in at]),
                "home": _avg_price([q["home"] for q in at])}, src
    return None, None


def closing_from_board(game: dict, sharp: list[str]) -> dict:
    out, sources = {}, set()
    sp, s1 = _market_avg(game["books"], "spread", sharp)
    if sp:
        out.update(close_away_spread=sp["line"], close_home_spread=-sp["line"],
                   close_away_spread_price=sp["away"], close_home_spread_price=sp["home"])
        sources.add(s1)
    tot, s2 = _market_avg(game["books"], "total", sharp)
    if tot:
        out.update(close_total=tot["line"], close_over_price=tot["away"],
                   close_under_price=tot["home"])
        sources.add(s2)
    ml, s3 = _market_avg(game["books"], "moneyline", sharp)
    if ml:
        out.update(close_away_ml=ml["away"], close_home_ml=ml["home"])
        sources.add(s3)
    out["close_source"] = " | ".join(sorted(sources)) if sources else None
    return out


# --------------------------------------------------------------------------- storage
OPEN_COLS = ["open_away_spread", "open_home_spread", "open_total", "open_away_ml", "open_home_ml"]
CLOSE_COLS = ["close_away_spread", "close_home_spread", "close_away_spread_price",
              "close_home_spread_price", "close_total", "close_over_price",
              "close_under_price", "close_away_ml", "close_home_ml"]
REAL_COLS = {"open_away_spread", "open_home_spread", "open_total",
             "close_away_spread", "close_home_spread", "close_total",
             "spread_move", "total_move"}


def db() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH)
    cols = OPEN_COLS + CLOSE_COLS + ["spread_move", "total_move"]
    con.execute(f"""CREATE TABLE IF NOT EXISTS games (
        game_id TEXT PRIMARY KEY, game_date TEXT, tip_et TEXT,
        away TEXT, home TEXT, away_rank TEXT, home_rank TEXT, away_conf TEXT, home_conf TEXT,
        {", ".join(c + (" REAL" if c in REAL_COLS else " INTEGER") for c in cols)},
        close_source TEXT, status TEXT, last_pregame_at TEXT, closed_at TEXT)""")
    con.execute("""CREATE TABLE IF NOT EXISTS line_history (
        game_id TEXT, taken_at TEXT, away_spread REAL, total REAL,
        away_ml INTEGER, home_ml INTEGER)""")
    return con


def _diff(a, b):
    return round(a - b, 1) if a is not None and b is not None else None


def first_line(con, game_id):
    """Opening line = the first line the tracker saved for this game (Covers' own
    'opening' column is unreliable). Each part uses its first non-empty value."""
    out = {}
    for col, key in (("away_spread", "open_away_spread"), ("total", "open_total"),
                     ("away_ml", "open_away_ml"), ("home_ml", "open_home_ml")):
        r = con.execute(f"SELECT {col} FROM line_history WHERE game_id=? AND {col} IS NOT NULL "
                        "ORDER BY taken_at LIMIT 1", (game_id,)).fetchone()
        out[key] = r[0] if r else None
    if all(v is None for v in out.values()):
        return None
    out["open_home_spread"] = -out["open_away_spread"] if out["open_away_spread"] is not None else None
    return out


def clean_bad_history() -> str | None:
    """One-time repair: drop snapshots taken from Covers' broken page variant (impossible
    two-sided prices) and re-derive each affected game's closing line from its last good
    snapshot. Runs once; a marker row records that it has been done."""
    con = db()
    con.execute("CREATE TABLE IF NOT EXISTS migrations (name TEXT PRIMARY KEY, done_at TEXT)")
    if con.execute("SELECT 1 FROM migrations WHERE name='bad_quotes_v1'").fetchone():
        con.close()
        return None

    def bad_ml(a, h):
        return a is not None and h is not None and not _real_market(int(a), int(h))

    rows = con.execute("SELECT rowid, game_id, taken_at, away_ml, home_ml FROM line_history").fetchall()
    per_time: dict[str, list[bool]] = {}
    for _, _, t, a, h in rows:
        if a is not None and h is not None:
            per_time.setdefault(t[:16], []).append(bad_ml(a, h))
    bad_times = {t for t, flags in per_time.items() if sum(flags) > len(flags) / 3}
    del_ids = [rid for rid, _, t, a, h in rows if t[:16] in bad_times or bad_ml(a, h)]
    con.executemany("DELETE FROM line_history WHERE rowid=?", [(i,) for i in del_ids])

    fixed = 0
    for gid, last_pre, src, sa, sh, oa, oh, ma, mh in con.execute(
            "SELECT game_id, last_pregame_at, close_source, close_away_spread_price, "
            "close_home_spread_price, close_over_price, close_under_price, close_away_ml, "
            "close_home_ml FROM games").fetchall():
        suspect = (src == "all books" or (last_pre and last_pre[:16] in bad_times)
                   or (sa is not None and sh is not None and not _real_market(sa, sh))
                   or (oa is not None and oh is not None and not _real_market(oa, oh))
                   or bad_ml(ma, mh))
        if not suspect:
            continue
        r = con.execute("SELECT away_spread, total, away_ml, home_ml FROM line_history "
                        "WHERE game_id=? AND taken_at<=? ORDER BY taken_at DESC LIMIT 1",
                        (gid, last_pre or "9999")).fetchone()
        sp, tot, aml, hml = r if r else (None, None, None, None)
        con.execute("""UPDATE games SET close_away_spread=?, close_home_spread=?,
                       close_away_spread_price=NULL, close_home_spread_price=NULL,
                       close_total=?, close_over_price=NULL, close_under_price=NULL,
                       close_away_ml=?, close_home_ml=?, close_source=?
                       WHERE game_id=?""",
                    (sp, -sp if sp is not None else None, tot, aml, hml,
                     "repaired from history" if r else None, gid))
        fixed += 1
    con.execute("INSERT INTO migrations VALUES ('bad_quotes_v1', ?)",
                (datetime.now(ET).isoformat(timespec="seconds"),))
    con.commit()
    con.close()
    rebuild_opens()
    return (f"Removed {len(del_ids)} bad snapshot rows ({len(bad_times)} bad page loads); "
            f"repaired the closing line of {fixed} games.")


def clean_kalshi_history() -> str | None:
    """One-time repair: remove snapshots saved from page loads that carried Kalshi quotes
    (the log marks those runs with "ignored N impossible quotes") and re-derive affected
    closing lines from the last remaining snapshot."""
    con = db()
    con.execute("CREATE TABLE IF NOT EXISTS migrations (name TEXT PRIMARY KEY, done_at TEXT)")
    if con.execute("SELECT 1 FROM migrations WHERE name='kalshi_v2'").fetchone():
        con.close()
        return None
    bad_min = set()
    try:
        for line in LOG_PATH.read_text(encoding="utf-8").splitlines():
            if "[warn] ignored" in line and "impossible quotes" in line:
                t = datetime.strptime(line[:16], "%Y-%m-%d %H:%M")
                for d in (-1, 0, 1):
                    bad_min.add((t + timedelta(minutes=d)).strftime("%Y-%m-%dT%H:%M"))
    except OSError:
        pass
    rows = con.execute("SELECT rowid, taken_at FROM line_history").fetchall()
    del_ids = [rid for rid, t in rows if t[:16] in bad_min]
    con.executemany("DELETE FROM line_history WHERE rowid=?", [(i,) for i in del_ids])
    fixed = 0
    for gid, last_pre, src in con.execute(
            "SELECT game_id, last_pregame_at, close_source FROM games").fetchall():
        if not ((last_pre and last_pre[:16] in bad_min) or "all books" in (src or "")):
            continue
        r = con.execute("SELECT away_spread, total, away_ml, home_ml, taken_at FROM line_history "
                        "WHERE game_id=? AND taken_at<=? ORDER BY taken_at DESC LIMIT 1",
                        (gid, last_pre or "9999")).fetchone()
        sp, tot, aml, hml, t = r if r else (None, None, None, None, None)
        con.execute("""UPDATE games SET close_away_spread=?, close_home_spread=?,
                       close_away_spread_price=NULL, close_home_spread_price=NULL,
                       close_total=?, close_over_price=NULL, close_under_price=NULL,
                       close_away_ml=?, close_home_ml=?, close_source=?,
                       last_pregame_at=COALESCE(?, last_pregame_at) WHERE game_id=?""",
                    (sp, -sp if sp is not None else None, tot, aml, hml,
                     "repaired from history" if r else None, t, gid))
        fixed += 1
    con.execute("INSERT INTO migrations VALUES ('kalshi_v2', ?)",
                (datetime.now(ET).isoformat(timespec="seconds"),))
    con.commit()
    con.close()
    rebuild_opens()
    return (f"Kalshi cleanup: removed {len(del_ids)} snapshot rows from {len(bad_min) // 3} "
            f"Kalshi page loads; repaired {fixed} games.")


def rebuild_opens() -> int:
    """Recalculate opening lines and line moves for every saved game from the tracker's
    own history. Safe to run any number of times."""
    con = db()
    n = 0
    for (gid,) in con.execute("SELECT DISTINCT game_id FROM line_history").fetchall():
        o = first_line(con, gid)
        if not o:
            continue
        con.execute("""UPDATE games SET open_away_spread=?, open_home_spread=?, open_total=?,
                       open_away_ml=?, open_home_ml=?,
                       spread_move=CASE WHEN close_away_spread IS NULL OR ? IS NULL THEN NULL
                                        ELSE ROUND(close_away_spread - ?, 1) END,
                       total_move=CASE WHEN close_total IS NULL OR ? IS NULL THEN NULL
                                       ELSE ROUND(close_total - ?, 1) END
                       WHERE game_id=?""",
                    (o["open_away_spread"], o["open_home_spread"], o["open_total"],
                     o["open_away_ml"], o["open_home_ml"],
                     o["open_away_spread"], o["open_away_spread"], o["open_total"], o["open_total"], gid))
        n += 1
    con.commit()
    con.close()
    return n


def record(games: dict[str, dict], sharp: list[str]) -> tuple[int, int]:
    con = db()
    now = datetime.now(ET).isoformat(timespec="seconds")
    updated = closed = missed = 0
    now_dt = datetime.now(ET)
    for g in games.values():
        prev = con.execute("SELECT status, tip_et, game_date FROM games WHERE game_id=?",
                           (g["game_id"],)).fetchone()
        if prev and prev[1]:
            # Keep the date/tip time saved when the game was first seen: after midnight
            # Covers still labels last night's games "Today", which would read as tonight.
            g["tip_et"] = prev[1]
        # Covers sometimes keeps showing a game as pre-game after it has tipped.
        # Past tip-off, never overwrite the saved line: lock it instead.
        if g["status"] == "pre" and g["tip_et"] and datetime.fromisoformat(g["tip_et"]) <= now_dt:
            if prev and prev[0] == "pre":
                con.execute("UPDATE games SET status='started', closed_at=? WHERE game_id=?",
                            (now, g["game_id"]))
                closed += 1
            continue
        if g["status"] != "pre":
            if prev and prev[0] == "pre":
                con.execute("UPDATE games SET status=?, closed_at=? WHERE game_id=?",
                            (g["status"], now, g["game_id"]))
                closed += 1
            elif not prev and g["status"] == "live":
                # First seen already live (tracker started late, or Covers added it late):
                # keep it with its opening line so it still shows up, marked as a missed close.
                o = {f"open_{k}": v for k, v in g["open"].items()}
                row = {"game_id": g["game_id"], "game_date": now[:10], "tip_et": None,
                       "away": g["away"], "home": g["home"],
                       "away_rank": g["away_rank"], "home_rank": g["home_rank"],
                       "away_conf": g["away_conf"], "home_conf": g["home_conf"],
                       **{k: o.get(k) for k in OPEN_COLS},
                       "close_source": "missed", "status": "live",
                       "last_pregame_at": None, "closed_at": now}
                keys = list(row)
                con.execute(f"INSERT OR IGNORE INTO games ({','.join(keys)}) "
                            f"VALUES ({','.join('?' * len(keys))})", [row[k] for k in keys])
                missed += 1
            continue
        c = closing_from_board(g, sharp)
        if not c.get("close_source"):
            continue  # no odds posted yet
        old_src = con.execute("SELECT close_source FROM games WHERE game_id=?",
                              (g["game_id"],)).fetchone()
        sharp_l = {b.lower() for b in sharp}
        def _main(src):
            return any(x.strip().lower() in sharp_l for x in re.split(r"[,|]", src or ""))
        if old_src and _main(old_src[0]) and not _main(c["close_source"]):
            continue  # this page load had none of the main books: keep their last line
        old = con.execute(f"SELECT {','.join(CLOSE_COLS)} FROM games WHERE game_id=?",
                          (g["game_id"],)).fetchone()
        if old:  # a market missing from this snapshot keeps its last good value
            for k, v in zip(CLOSE_COLS, old):
                if c.get(k) is None and v is not None:
                    c[k] = v
        con.execute("INSERT INTO line_history VALUES (?,?,?,?,?,?)",
                    (g["game_id"], now, c.get("close_away_spread"), c.get("close_total"),
                     c.get("close_away_ml"), c.get("close_home_ml")))
        o = first_line(con, g["game_id"]) or {f"open_{k}": v for k, v in g["open"].items()}
        row = {
            "game_id": g["game_id"], "game_date": (prev[2] if prev else (g["tip_et"] or now)[:10]),
            "tip_et": g["tip_et"],
            "away": g["away"], "home": g["home"],
            "away_rank": g["away_rank"], "home_rank": g["home_rank"],
            "away_conf": g["away_conf"], "home_conf": g["home_conf"],
            **{k: o.get(k) for k in OPEN_COLS},
            **{k: c.get(k) for k in CLOSE_COLS},
            "spread_move": _diff(c.get("close_away_spread"), o.get("open_away_spread")),
            "total_move": _diff(c.get("close_total"), o.get("open_total")),
            "close_source": c["close_source"], "status": "pre",
            "last_pregame_at": now, "closed_at": None,
        }
        keys = list(row)
        con.execute(
            f"INSERT INTO games ({','.join(keys)}) VALUES ({','.join('?' * len(keys))}) "
            "ON CONFLICT(game_id) DO UPDATE SET "
            + ",".join(f"{k}=excluded.{k}" for k in keys if k != "game_id"),
            [row[k] for k in keys])
        updated += 1
    con.commit()
    con.close()
    record.missed = missed
    return updated, closed


def load_day(date: str) -> list[dict]:
    con = db()
    con.row_factory = sqlite3.Row
    rows = con.execute("SELECT * FROM games WHERE game_date=? ORDER BY tip_et, away",
                       (date,)).fetchall()
    con.close()
    return [dict(r) for r in rows]


# --------------------------------------------------------------------------- notes
def load_notes() -> dict[str, dict]:
    """Return {KEY: row} for every abbreviation/name/alias in the notes sheet.
    Falls back to the last downloaded copy if the sheet can't be reached."""
    text = None
    try:
        r = requests.get(NOTES_URL, headers=HEADERS, timeout=20)
        if r.status_code == 200 and not r.text.lstrip().startswith("<"):
            text = r.content.decode("utf-8-sig")
            NOTES_CACHE.write_text(text, encoding="utf-8")
        else:
            print("[warn] couldn't read the notes sheet - is it shared as "
                  "'Anyone with the link'? Using last saved copy.", file=sys.stderr)
    except requests.RequestException as e:
        print(f"[warn] notes sheet unreachable ({e}); using last saved copy.", file=sys.stderr)
    if text is None and NOTES_CACHE.exists():
        text = NOTES_CACHE.read_text(encoding="utf-8")
    if not text:
        return {}
    index: dict[str, dict] = {}
    for row in csv.DictReader(io.StringIO(text)):
        if not (row.get("notes") or "").strip():
            continue
        keys = [row.get("covers_abbr", ""), row.get("team", "")]
        keys += (row.get("aliases") or "").split(";")
        for k in keys:
            k = k.strip().upper()
            if k:
                index[k] = row
    return index


def team_notes(abbr: str, notes: dict[str, dict]) -> str:
    row = notes.get((abbr or "").strip().upper())
    return row["notes"].strip() if row else ""


def load_habits() -> dict[str, dict]:
    """Team Habits tab: per-team fouling averages calculated in the sheet from the Foul Log."""
    text = None
    try:
        r = requests.get(HABITS_URL, headers=HEADERS, timeout=20)
        if r.status_code == 200 and not r.text.lstrip().startswith("<"):
            text = r.content.decode("utf-8-sig")
            HABITS_CACHE.write_text(text, encoding="utf-8")
    except requests.RequestException:
        pass
    if text is None and HABITS_CACHE.exists():
        text = HABITS_CACHE.read_text(encoding="utf-8")
    if not text:
        return {}
    return {(row.get("covers_abbr") or "").strip().upper(): row
            for row in csv.DictReader(io.StringIO(text)) if row.get("covers_abbr")}


def _clock(sec) -> str:
    try:
        s = round(float(sec))
    except (TypeError, ValueError):
        return "?"
    return f"{s // 60}:{s % 60:02d}"


def habit_summary(abbr: str, habits: dict[str, dict]) -> str:
    h = habits.get((abbr or "").strip().upper())
    if not h or not h.get("games_logged"):
        return ""
    g = h["games_logged"]
    return (f"Fouling ({g} game{'s' if str(g) != '1' else ''} logged): starts avg {_clock(h.get('avg_time_left_sec'))} left, "
            f"down avg {h.get('avg_down_by')} | earliest {_clock(h.get('earliest_start_sec'))} | "
            f"{h.get('sec_left_per_point_down')} sec left per point down")


# --------------------------------------------------------------------------- output
def _f(v, signed=True):
    if v is None:
        return "-"
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return f"{v:+}" if signed else str(v)


def show(date: str) -> None:
    rows = load_day(date)
    if not rows:
        print(f"No games stored for {date}.")
        return
    notes = load_notes()
    habits = load_habits()
    print(f"{'Tip':5} {'Matchup':18} | {'Spread open->close':22} | {'Total open->close':22} | "
          f"{'ML open':12} {'ML close':12} | Status")
    for r in rows:
        tip = r["tip_et"][11:16] if r["tip_et"] else "?"
        sp = f"{_f(r['open_away_spread'])} -> {_f(r['close_away_spread'])} ({_f(r['close_away_spread_price'])})"
        tot = f"{_f(r['open_total'], False)} -> {_f(r['close_total'], False)} ({_f(r['close_over_price'])})"
        mlo = f"{_f(r['open_away_ml'])}/{_f(r['open_home_ml'])}"
        mlc = f"{_f(r['close_away_ml'])}/{_f(r['close_home_ml'])}"
        status = "CLOSED" if r["closed_at"] else "pre-game (updating)"
        print(f"{tip:5} {r['away'] + ' @ ' + r['home']:18} | {sp:22} | {tot:22} | {mlo:12} {mlc:12} | {status}")
        for side in ("away", "home"):
            hs = habit_summary(r[side], habits)
            if hs:
                print(f"      {r[side]} {hs}")
            n = team_notes(r[side], notes)
            if n:
                lines = n.splitlines()
                print(f"      {r[side]} notes: {lines[0]}")
                for ln in lines[1:]:
                    print(f"      {' ' * len(r[side])}        {ln}")
    print("\nSpreads/ML are listed away first. Closing = sharp-book average at last pre-game snapshot.")


def export(date: str, out: str) -> None:
    rows = load_day(date)
    notes = load_notes()
    habits = load_habits()
    for r in rows:
        for side in ("away", "home"):
            r[f"{side}_notes"] = team_notes(r[side], notes)
            r[f"{side}_foul_habits"] = habit_summary(r[side], habits)
    fields = ["game_date", "tip_et", "away", "home", "away_rank", "home_rank",
              "away_conf", "home_conf", *OPEN_COLS, *CLOSE_COLS, "spread_move", "total_move",
              "close_source", "away_foul_habits", "home_foul_habits", "away_notes", "home_notes",
              "status", "last_pregame_at", "closed_at", "game_id"]
    with open(out, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    print(f"Wrote {len(rows)} games to {out}")


# --------------------------------------------------------------------------- main
def refresh_dashboard() -> None:
    """Rebuild NCAAB Dashboard.html if ncaab_dashboard.py is next to this file."""
    try:
        import importlib
        import ncaab_dashboard
        importlib.reload(ncaab_dashboard)  # pick up dashboard updates without restarting
        try:
            import ncaab_publish
            importlib.reload(ncaab_publish)
        except ImportError:
            pass
    except ImportError:
        return
    try:
        ncaab_dashboard.build_dashboard(open_browser=False)
    except Exception as e:
        log(f"[warn] couldn't rebuild dashboard: {e}")


def snapshot(sharp: list[str], use_browser: bool) -> None:
    html = fetch_html(use_browser)
    games = parse_board(html)
    if REJECTED:
        log(f"[warn] ignored {len(REJECTED)} impossible quotes (e.g. {REJECTED[0]['book']} "
            f"{REJECTED[0]['market']} {REJECTED[0]['away']}/{REJECTED[0]['home']})")
        try:
            Path(DB_PATH).with_name("last_rejected.json").write_text(
                json.dumps(REJECTED[:40], indent=1), encoding="utf-8")
        except Exception:
            pass
    try:  # keep the latest per-book quotes so odd numbers can be checked later
        Path(DB_PATH).with_name("last_board.json").write_text(
            json.dumps(games, indent=1, default=str), encoding="utf-8")
        i = html.find('id="spread-table"')
        k = html.find("data-book=", i)
        Path(DB_PATH).with_name("last_board_sample.html").write_text(html[k:k + 6000], encoding="utf-8")
    except Exception:
        pass
    up, cl = record(games, sharp)
    ms = getattr(record, "missed", 0)
    ts = datetime.now(ET).strftime("%H:%M:%S ET")
    log(f"[{ts}] {len(games)} games on board | {up} pre-game lines updated | {cl} newly closed"
        + (f" | {ms} added already live (missed close)" if ms else ""))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("run", "watch"):
        p = sub.add_parser(name)
        p.add_argument("--books", help="comma-separated books to average (default: SHARP_BOOKS)")
        p.add_argument("--browser", action="store_true", help="fetch with headless Chromium (Playwright)")
        if name == "watch":
            p.add_argument("--interval", type=int, default=10, help="minutes between snapshots")
    sub.add_parser("dashboard")
    for name in ("show", "export"):
        p = sub.add_parser(name)
        p.add_argument("--date", default=datetime.now(ET).date().isoformat())
        if name == "export":
            p.add_argument("--out")
    a = ap.parse_args()

    if a.cmd in ("run", "watch"):
        sharp = [b.strip() for b in a.books.split(",")] if a.books else SHARP_BOOKS
    if a.cmd == "run":
        snapshot(sharp, a.browser)
    elif a.cmd == "watch":
        watch(sharp, a.browser, a.interval)
    elif a.cmd == "dashboard":
        from ncaab_dashboard import build_dashboard
        print(f"Opened {build_dashboard()}")
    elif a.cmd == "show":
        show(a.date)
    elif a.cmd == "export":
        export(a.date, a.out or str(Path(__file__).with_name(f"ncaab_lines_{a.date}.csv")))


def _single_instance():
    """Hold a local port so a second tracker window can't run at the same time."""
    import socket
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", 47613))
    except OSError:
        return None
    return sock


def watch(sharp: list[str], use_browser: bool, interval: int) -> None:
    lock = _single_instance()
    if lock is None:
        print("The tracker is already running in another window - nothing to do here.")
        return
    try:
        fixed = rebuild_opens()
        if fixed:
            log(f"Opening lines recalculated from saved history for {fixed} games.")
    except Exception as e:
        log(f"[warn] couldn't recalculate opening lines: {e}")
    log(f"Tracking lines every {interval} min. Leave this window open (minimized is fine); "
        "it keeps running day after day. The PC must stay awake for it to work.\n")
    while True:
        try:
            snapshot(sharp, use_browser)
        except Exception as e:  # keep looping through transient errors
            log(f"[error] {e}")
        try:
            refresh_dashboard()
        except Exception as e:
            log(f"[error] dashboard: {e}")
        time.sleep(interval * 60)


def menu() -> None:
    """Shown when the file is double-clicked (no command given)."""
    while True:
        today = datetime.now(ET).date().isoformat()
        print("\n=========== NCAAB Lines Tool ===========")
        print("  1  Track lines (keeps running every day)")
        print("  2  Open dashboard (lines, filters, notes)")
        print("  3  Show today's lines here (text)")
        print("  4  Export today's lines to Excel (CSV)")
        print("  5  Take one snapshot now")
        print("  Q  Quit")
        choice = input("Choose: ").strip().lower()
        try:
            if choice == "1":
                watch(SHARP_BOOKS, False, 10)
            elif choice == "2":
                from ncaab_dashboard import build_dashboard
                print(f"Opening {build_dashboard()} ...")
            elif choice == "3":
                show(today)
            elif choice == "4":
                out = Path(__file__).with_name(f"ncaab_lines_{today}.csv")
                export(today, str(out))
                if sys.platform == "win32":
                    import os
                    os.startfile(out)  # opens in Excel
            elif choice == "5":
                snapshot(SHARP_BOOKS, False)
                refresh_dashboard()
            elif choice in ("q", "quit", "exit"):
                return
        except KeyboardInterrupt:
            print("\nStopped.")
        except Exception as e:
            print(f"\n[error] {e}\nIf this keeps happening, copy this message to Claude.")


if __name__ == "__main__":
    if len(sys.argv) == 1:
        try:
            menu()
        except Exception as e:
            print(f"\n[error] {e}")
            input("\nPress Enter to close...")
    else:
        main()
