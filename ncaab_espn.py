"""Final scores and ESPN BPI power rankings for the NCAAB dashboard.

Both come from ESPN's public JSON feeds (the same ones LSB already uses for scores):
  scoreboard  https://site.api.espn.com/apis/site/v2/sports/basketball/mens-college-basketball/scoreboard
  BPI         https://site.web.api.espn.com/apis/fitt/v3/sports/basketball/mens-college-basketball/powerindex

Everything is saved in ncaab_odds.db (which the cloud tracker keeps on the data branch), so old
dates are read once and the page still shows scores and rankings if ESPN is down for a while.
Covers and ESPN name teams differently, so games are matched on team names and mascots; each match
also teaches us the team's ESPN id, which is then used to find that team in the BPI list.
"""
import json
import re
from datetime import datetime, timedelta

import requests

import ncaab_odds
from ncaab_odds import ET, HEADERS, db

SCOREBOARD = ("https://site.api.espn.com/apis/site/v2/sports/basketball/mens-college-basketball/"
              "scoreboard?dates={}&groups=50&limit=500")
BPI_URL = ("https://site.web.api.espn.com/apis/fitt/v3/sports/basketball/mens-college-basketball/"
           "powerindex?region=us&lang=en&limit=100&page={}")
DATES_PER_RUN = 15      # older dates are read once each; this caps a first-time backfill per run
BPI_EVERY_HOURS = 3     # ESPN updates BPI about once a day


def _setup(con):
    con.execute("""CREATE TABLE IF NOT EXISTS game_scores (
        game_id TEXT PRIMARY KEY, espn_id TEXT, state TEXT, detail TEXT,
        away_score INTEGER, home_score INTEGER, updated_at TEXT)""")
    con.execute("CREATE TABLE IF NOT EXISTS score_dates (game_date TEXT PRIMARY KEY, checked_at TEXT)")
    con.execute("CREATE TABLE IF NOT EXISTS espn_teams (team_key TEXT PRIMARY KEY, espn_id TEXT)")
    con.execute("CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT, at TEXT)")


def _get(url):
    r = requests.get(url, headers=HEADERS, timeout=25)
    r.raise_for_status()
    return r.json()


# ---------- name matching ----------

def _norm(s):
    from ncaab_dashboard import norm
    return norm(s)


def _ours(full, school, abbr):
    """Name keys and mascot for one of our teams (Covers names)."""
    keys = {k for k in (_norm(full), _norm(school)) if k}
    mascot = ""
    if full and school and _norm(full).startswith(_norm(school)):
        mascot = _norm(full)[len(_norm(school)):].strip()
    elif full:
        mascot = _norm(full).split()[-1]
    return {"keys": keys, "mascot": mascot, "abbr": (abbr or "").upper()}


def _theirs(t):
    """Name keys and mascot for an ESPN team object."""
    keys = {_norm(t.get(f)) for f in ("displayName", "location", "shortDisplayName", "nickname") if t.get(f)}
    keys.discard("")
    return {"id": str(t.get("id") or ""), "keys": keys, "mascot": _norm(t.get("name") or ""),
            "abbr": (t.get("abbreviation") or "").upper()}


def _side_score(ours, theirs, known_id):
    if known_id and theirs["id"]:
        return 3 if known_id == theirs["id"] else -9   # we already know this team's ESPN id
    if ours["keys"] & theirs["keys"]:
        return 2
    if ours["mascot"] and ours["mascot"] == theirs["mascot"]:
        return 1
    return 0


# ---------- final scores ----------

def _event_teams(ev):
    c = (ev.get("competitions") or [{}])[0]
    cs = c.get("competitors") or []
    home = next((x for x in cs if x.get("homeAway") == "home"), cs[0] if cs else {})
    away = next((x for x in cs if x.get("homeAway") == "away"), cs[1] if len(cs) > 1 else {})
    st = (ev.get("status") or c.get("status") or {}).get("type") or {}
    return away, home, st


def _int(v):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def update_scores(rows, names, schools, team_key):
    """Read ESPN scoreboards for dates that still need scores and save the matches.
    Returns {game_id: score row}. Never raises: a failed read just keeps what was saved."""
    con = db()
    _setup(con)
    known = dict(con.execute("SELECT team_key, espn_id FROM espn_teams"))
    saved = {r[0]: r for r in con.execute(
        "SELECT game_id, espn_id, state, detail, away_score, home_score FROM game_scores")}
    checked = dict(con.execute("SELECT game_date, checked_at FROM score_dates"))
    now = datetime.now(ET)
    today = ncaab_odds.slate_date(now.isoformat())
    recent = {today, (datetime.fromisoformat(today) - timedelta(days=1)).date().isoformat()}

    by_date = {}
    for r in rows:
        by_date.setdefault(r["game_date"], []).append(r)
    need = []
    for d, games in sorted(by_date.items(), reverse=True):
        if d > today:
            continue
        missing = [g for g in games if (saved.get(g["game_id"]) or [None, None, ""])[2] != "post"]
        if not missing:
            continue
        # an older date is done once it was read a full day after it was played
        if d not in recent and checked.get(d, "") >= (datetime.fromisoformat(d) + timedelta(days=1)).isoformat():
            continue
        need.append(d)
    unmatched = []
    for d in need[:DATES_PER_RUN]:
        try:
            events = _get(SCOREBOARD.format(d.replace("-", ""))).get("events") or []
        except Exception as e:  # noqa: BLE001
            ncaab_odds.log(f"[warn] ESPN scores for {d}: {e}")
            continue
        parsed = []
        for ev in events:
            a, h, st = _event_teams(ev)
            parsed.append((ev, a, h, st, _theirs(a.get("team") or {}), _theirs(h.get("team") or {})))
        for g in by_date[d]:
            if (saved.get(g["game_id"]) or [None, None, ""])[2] == "post":
                continue
            ka, kh = team_key(g["away"], g.get("away_conf")), team_key(g["home"], g.get("home_conf"))
            oa = _ours(names.get(ka), schools.get(ka), g["away"])
            oh = _ours(names.get(kh), schools.get(kh), g["home"])
            best, best_s, tie = None, 0, False
            for ev, a, h, st, ta, th in parsed:
                for flip in (False, True):  # neutral-site games can list the teams either way
                    x, y = (th, ta) if flip else (ta, th)
                    s1, s2 = _side_score(oa, x, known.get(ka)), _side_score(oh, y, known.get(kh))
                    if min(s1, s2) < 1 or s1 + s2 < 3:
                        continue
                    if s1 + s2 > best_s:
                        best, best_s, tie = (ev, a, h, st, x, y, flip), s1 + s2, False
                    elif s1 + s2 == best_s:
                        tie = True
            if not best or tie:
                unmatched.append(f"{g['away']} @ {g['home']}")
                continue
            ev, a, h, st, x, y, flip = best
            sa, sh = (_int(h.get("score")), _int(a.get("score"))) if flip else (_int(a.get("score")), _int(h.get("score")))
            state = st.get("state") or ""
            con.execute("INSERT OR REPLACE INTO game_scores VALUES (?,?,?,?,?,?,?)",
                        (g["game_id"], str(ev.get("id") or ""), state, st.get("shortDetail") or st.get("detail") or "",
                         sa, sh, now.isoformat(timespec="minutes")))
            for k, t in ((ka, x), (kh, y)):
                if t["id"] and k not in known:
                    known[k] = t["id"]
                    con.execute("INSERT OR REPLACE INTO espn_teams VALUES (?,?)", (k, t["id"]))
        con.execute("INSERT OR REPLACE INTO score_dates VALUES (?,?)", (d, now.isoformat(timespec="minutes")))
        con.commit()
    if unmatched:
        ncaab_odds.log(f"[info] ESPN scores: couldn't match {len(unmatched)} game(s): {', '.join(unmatched[:12])}")
    out = {r[0]: {"espn_id": r[1], "state": r[2], "detail": r[3], "away": r[4], "home": r[5]}
           for r in con.execute("SELECT game_id, espn_id, state, detail, away_score, home_score FROM game_scores")}
    con.close()
    return out, known


# ---------- BPI ----------

def _bpi_fetch():
    teams, page, pages, names = [], 1, 1, None
    while page <= pages and page <= 10:
        data = _get(BPI_URL.format(page))
        pages = int((data.get("pagination") or {}).get("pages") or 1)
        if names is None:
            cat = next((c for c in data.get("categories") or [] if c.get("name") == "bpi"), {})
            names = cat.get("names") or []
        for t in data.get("teams") or []:
            vals = next((c.get("values") or [] for c in t.get("categories") or [] if c.get("name") == "bpi"), [])
            row = dict(zip(names, vals))
            tm = t.get("team") or {}
            if not row.get("bpirank"):
                continue
            teams.append({
                "id": str(tm.get("id") or ""), "name": tm.get("displayName") or "",
                "short": tm.get("shortDisplayName") or tm.get("nickname") or "",
                "nickname": tm.get("nickname") or "", "mascot": tm.get("name") or "",
                "abbr": tm.get("abbreviation") or "",
                "conf": (tm.get("group") or {}).get("shortName") or (tm.get("group") or {}).get("abbreviation") or "",
                "rank": int(row["bpirank"]), "bpi": row.get("bpi"), "off": row.get("bpioffense"),
                "def": row.get("bpidefense"), "chg": row.get("bpisevendaychangerank"),
                "w": _int(row.get("wins")), "l": _int(row.get("losses")),
            })
        page += 1
    if len(teams) < 100:
        raise ValueError(f"only {len(teams)} teams came back")
    return sorted(teams, key=lambda t: t["rank"])


def load_bpi():
    """BPI list (fresh every few hours, otherwise the saved copy) and when ESPN last gave it to us."""
    con = db()
    _setup(con)
    hit = con.execute("SELECT v, at FROM kv WHERE k='bpi'").fetchone()
    now = datetime.now(ET)
    if not hit or hit[1] < (now - timedelta(hours=BPI_EVERY_HOURS)).isoformat():
        try:
            teams = _bpi_fetch()
            hit = (json.dumps(teams), now.isoformat(timespec="minutes"))
            con.execute("INSERT OR REPLACE INTO kv VALUES ('bpi', ?, ?)", hit)
            con.commit()
        except Exception as e:  # noqa: BLE001
            ncaab_odds.log(f"[warn] ESPN BPI: {e}")
    con.close()
    return (json.loads(hit[0]), hit[1]) if hit else ([], None)


def bpi_for(teams, known_id, full, school):
    """The BPI entry for one of our teams: by ESPN id when a game match taught us it, else by name."""
    if known_id:
        for t in teams:
            if t["id"] == known_id:
                return t
    ours = _ours(full, school, "")
    hits = [t for t in teams if ours["keys"] & {_norm(t["name"]), _norm(t["short"]), _norm(t["nickname"])}]
    return hits[0] if len(hits) == 1 else None


# ---------- results against the closing line ----------

def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def grade(g, sc):
    """Final score plus cover and over/under against the closing line (opening line if no close)."""
    if not sc or sc.get("state") != "post" or sc.get("away") is None or sc.get("home") is None:
        return None
    a, h = sc["away"], sc["home"]
    out = {"away": a, "home": h, "detail": sc.get("detail") or "Final"}
    sp, src = _f(g.get("close_home_spread")), "close"
    if sp is None:
        sp, src = _f(g.get("open_home_spread")), "open"
    if sp is not None:
        m = h + sp - a                      # > 0: home covered
        fav = "home" if sp < 0 else ("away" if sp > 0 else None)
        out["spread"] = {"line": abs(sp), "src": src, "fav": fav,
                         "result": "push" if m == 0 else ("home" if m > 0 else "away")}
    tot, tsrc = _f(g.get("close_total")), "close"
    if tot is None:
        tot, tsrc = _f(g.get("open_total")), "open"
    if tot is not None:
        pts = a + h
        out["total"] = {"line": tot, "src": tsrc, "result": "push" if pts == tot else ("over" if pts > tot else "under")}
    return out
