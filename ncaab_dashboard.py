#!/usr/bin/env python3
"""NCAAB dashboard: builds "NCAAB Dashboard.html" from the lines that ncaab_odds.py
has collected, adds your team notes and fouling habits, and opens it in your browser.
Run it by double-clicking "Open Dashboard.bat" (keep this file next to ncaab_odds.py)."""

import csv
import html as htmllib
import io
import json
import re
import sqlite3
import sys
import webbrowser
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ncaab_odds  # noqa: E402
from ncaab_odds import ET, HEADERS, db, load_habits, load_notes  # noqa: E402

import requests  # noqa: E402

DASHBOARD_PATH = Path(__file__).with_name("NCAAB Dashboard.html")
MATCHUP_URL = "https://www.covers.com/sport/basketball/ncaab/matchup/{}"
TITLE_RE = re.compile(r"<title[^>]*>\s*(.+?)\s+vs\.?\s+(.+?)\s+(?:Game Overview|Odds|Matchup|Preview|-|\|)", re.S)
SCHOOL_RE = re.compile(r"""href=["'][^"']*/ncaab/teams/main/([a-z0-9.-]+)["'][^>]*>\s*([^<>]{2,60}?)\s*</a>""")
TEAM_MAP = Path(__file__).with_name("team_map.csv")  # abbr,name,school saved from Covers
LOOKUPS_PER_RUN = 60
TWO_WORD_MASCOTS = {"blue devils", "golden griffins", "red storm", "thundering herd", "runnin rebels",
                    "rainbow warriors", "green wave", "yellow jackets", "blue demons", "wolf pack",
                    "golden lions", "fighting illini", "blue hens", "golden eagles", "red raiders",
                    "fighting irish", "tar heels", "mean green", "sun devils", "golden bears",
                    "golden gophers", "nittany lions", "crimson tide", "scarlet knights", "horned frogs",
                    "demon deacons", "golden hurricane", "red wolves", "black knights", "big red",
                    "purple eagles", "great danes", "river hawks", "red foxes", "golden flashes",
                    "blue raiders", "fighting hawks", "golden panthers", "black bears", "lady",
                    "mountain hawks", "ragin cajuns", "delta devils", "golden rams", "mountaineers"}


def norm(s: str) -> str:
    """Loose team-name key: 'Saint Mary's (CA)' -> 'st marys ca', 'Montana St.' -> 'montana st'."""
    s = htmllib.unescape(s or "").lower().replace("&", " and ")
    s = re.sub(r"[().'\u2019]", "", s)
    s = re.sub(r"[-/,]", " ", s)
    swap = {"saint": "st", "state": "st", "university": "", "univ": ""}
    return " ".join(w for w in (swap.get(t, t) for t in s.split()) if w)


def fill_team_names(rows: list[dict]) -> tuple[dict[str, str], dict[str, str]]:
    """Full names ("Montana State Bobcats") and school names ("Montana State") keyed by
    Covers abbreviation (MTST). Covers' odds page only has abbreviations, so each game page
    is read once per new team and the names are saved in the database."""
    con = db()
    con.execute("CREATE TABLE IF NOT EXISTS team_names (abbr TEXT PRIMARY KEY, name TEXT)")
    cols = [r[1] for r in con.execute("PRAGMA table_info(team_names)")]
    if "school" not in cols:
        con.execute("ALTER TABLE team_names ADD COLUMN school TEXT")
    names, schools = {}, {}
    try:  # seed from the saved Covers team list
        for t in csv.DictReader(io.StringIO(TEAM_MAP.read_text(encoding="utf-8"))):
            if t.get("abbr") and t.get("name"):
                names[t["abbr"]] = t["name"]
                if t.get("school"):
                    schools[t["abbr"]] = t["school"]
    except OSError:
        pass
    for abbr, name, school in con.execute("SELECT abbr, name, school FROM team_names"):
        names[abbr] = name
        if school:
            schools[abbr] = school

    def known(a):
        return a in names and a in schools

    warned = ncaab_odds.__dict__.setdefault("_name_warned", set())  # survives dashboard reloads
    tried = 0
    for r in rows:
        if tried >= LOOKUPS_PER_RUN:
            break
        if known(r["away"]) and known(r["home"]):
            continue
        tried += 1
        if tried == 1:
            print("Looking up full team names (first time only)...")
        try:
            resp = requests.get(MATCHUP_URL.format(r["game_id"]), headers=HEADERS, timeout=30)
            page = resp.text
        except requests.RequestException as e:
            if r["game_id"] not in warned:
                warned.add(r["game_id"])
                ncaab_odds.log(f"[warn] team name lookup failed for game {r['game_id']}: {e}")
            continue
        m = TITLE_RE.search(page)
        if not m:
            if r["game_id"] not in warned:
                warned.add(r["game_id"])
                ncaab_odds.log(f"[warn] team name lookup: no title on game {r['game_id']} page (HTTP {resp.status_code})")
            continue
        found, seen = [], set()
        for slug, text in SCHOOL_RE.findall(page):
            text = htmllib.unescape(text).strip()
            if slug in seen or re.match(r"^[A-Z0-9&-]{2,6}\s", text):
                continue
            seen.add(slug)
            found.append(text)
            if len(found) == 2:
                break
        for i, (abbr, name) in enumerate(((r["away"], m.group(1)), (r["home"], m.group(2)))):
            name = re.sub(r"\s+", " ", htmllib.unescape(name)).strip()
            school = found[i] if len(found) == 2 else None
            if school and not norm(name).startswith(norm(school)):
                school = None  # page order didn't match; fall back to name matching
            if abbr and name:
                names[abbr] = name
                if school:
                    schools[abbr] = school
                con.execute("INSERT OR REPLACE INTO team_names (abbr, name, school) VALUES (?, ?, ?)",
                            (abbr, name, school or schools.get(abbr)))
        con.commit()
    con.close()
    return names, schools


def load_all() -> list[dict]:
    con = db()
    con.row_factory = sqlite3.Row
    rows = con.execute("SELECT * FROM games ORDER BY game_date DESC, tip_et, away").fetchall()
    con.close()
    return [dict(r) for r in rows]


def read_habit_rows() -> list[dict]:
    load_habits()  # refreshes habits_cache.csv from the sheet when it can
    try:
        text = ncaab_odds.HABITS_CACHE.read_text(encoding="utf-8")
    except OSError:
        return []
    return list(csv.DictReader(io.StringIO(text)))


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def build_dashboard(open_browser: bool = True) -> Path:
    """Write NCAAB Dashboard.html (lines + notes + fouling habits) and optionally open it."""
    rows = load_all()
    note_index = load_notes()
    habit_rows = read_habit_rows()
    try:
        full_names, schools = fill_team_names(rows)
    except Exception:
        full_names, schools = {}, {}

    # ---- notes: one entry per sheet row, findable by Covers code, school name or alias
    note_rows, seen = [], set()
    for row in note_index.values():
        if id(row) not in seen:
            seen.add(id(row))
            note_rows.append(row)
    by_abbr, by_name, by_conf = {}, {}, {}
    for row in note_rows:
        aliases = [a.strip() for a in (row.get("aliases") or "").split(";") if a.strip()]
        if "conference" in [a.lower() for a in aliases]:
            by_conf[norm(row.get("team"))] = row
            continue
        if (row.get("covers_abbr") or "").strip():
            by_abbr[row["covers_abbr"].strip().upper()] = row
        for key in [row.get("team", "")] + aliases:
            if key.strip():
                by_name.setdefault(norm(key), row)

    habits_by_name, habits_by_abbr = {}, {}
    for h in habit_rows:
        if (h.get("team") or "").strip():
            habits_by_name[norm(h["team"])] = h
        if (h.get("covers_abbr") or "").strip():
            habits_by_abbr[h["covers_abbr"].strip().upper()] = h

    def find_note(abbr):
        if abbr in by_abbr:
            return by_abbr[abbr]
        school = schools.get(abbr)
        if school and norm(school) in by_name:
            return by_name[norm(school)]
        full = norm(full_names.get(abbr, ""))
        words = full.split()
        if len(words) >= 2 and " ".join(words[:-1]) in by_name:
            return by_name[" ".join(words[:-1])]
        if len(words) >= 3 and " ".join(words[-2:]) in TWO_WORD_MASCOTS and " ".join(words[:-2]) in by_name:
            return by_name[" ".join(words[:-2])]
        return by_name.get(norm(abbr))

    def habit_for(abbr, note):
        h = habits_by_abbr.get(abbr)
        if not h and note:
            h = habits_by_name.get(norm(note.get("team")))
        if not h and schools.get(abbr):
            h = habits_by_name.get(norm(schools[abbr]))
        if not h or (_num(h.get("games_logged")) == 0 and _num(h.get("no_foul_logged")) == 0):
            return None
        keys = ("games_logged", "avg_time_left_sec", "avg_down_by", "earliest_start_sec",
                "sec_left_per_point_down", "no_foul_logged")
        return {k: h.get(k) for k in keys}

    for r in rows:
        reasons = []
        for side in ("away", "home"):
            abbr = r[side]
            note = find_note(abbr)
            r[f"{side}_name"] = full_names.get(abbr) or (note or {}).get("team") or abbr
            r[f"{side}_notes"] = (note or {}).get("notes", "").strip()
            r[f"{side}_tag"] = ((note or {}).get("tag") or "").strip().upper()
            r[f"{side}_note_team"] = (note or {}).get("team") or ""
            r[f"{side}_habit"] = habit_for(abbr, note)
            if "DO NOT BET" in [x.strip() for x in re.split(r"[;,]", r[f"{side}_tag"])]:
                reasons.append(r[f"{side}_note_team"] or abbr)
            conf = by_conf.get(norm(r.get(f"{side}_conf") or ""))
            if conf:
                reasons.append(f"{conf.get('team')} game")
        r["dnb"] = ", ".join(dict.fromkeys(reasons))

    teams = []
    for row in note_rows:
        is_conf = "conference" in (row.get("aliases") or "").lower()
        h = None if is_conf else habit_for("", row)
        teams.append({"abbr": (row.get("covers_abbr") or "").strip().upper(), "team": row.get("team") or "",
                      "notes": (row.get("notes") or "").strip(), "habit": h,
                      "tag": (row.get("tag") or "").strip().upper(), "conf": is_conf})

    data = {
        "generated": datetime.now(ET).isoformat(timespec="minutes"),
        "today": datetime.now(ET).date().isoformat(),
        "games": rows,
        "teams": sorted(teams, key=lambda t: t["team"].lower()),
    }
    payload = json.dumps(data).replace("</", "<\\/")
    DASHBOARD_PATH.write_text(DASHBOARD_HTML.replace("__DATA__", payload), encoding="utf-8")
    try:  # live link (only if "Set Up Live Link" has been run)
        import ncaab_publish
        ncaab_publish.upload(DASHBOARD_PATH)
    except Exception:
        pass
    if open_browser:
        webbrowser.open(DASHBOARD_PATH.resolve().as_uri())
    return DASHBOARD_PATH


DASHBOARD_HTML = r'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>NCAAB Lines</title>
<style>
:root{--bg:#f6f7f9;--card:#fff;--text:#16181d;--muted:#6b7280;--line:#e5e7eb;--accent:#2563eb;--up:#16a34a;--down:#dc2626;--warn:#d97706;--soft:#eef2ff;color-scheme:light}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#101216;--card:#1a1d23;--text:#eceef2;--muted:#9aa1ad;--line:#2a2e36;--accent:#60a5fa;--up:#4ade80;--down:#f87171;--warn:#fbbf24;--soft:#1e2433;color-scheme:dark}}
:root[data-theme="dark"]{--bg:#101216;--card:#1a1d23;--text:#eceef2;--muted:#9aa1ad;--line:#2a2e36;--accent:#60a5fa;--up:#4ade80;--down:#f87171;--warn:#fbbf24;--soft:#1e2433;color-scheme:dark}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:15px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
.wrap{max-width:1360px;margin:0 auto;padding:20px 16px 48px}
h1{font-size:22px;margin:0 0 4px}
.sub{color:var(--muted);margin:0 0 18px;font-size:13px}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px;margin-bottom:16px}
.stat{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px 14px}
.stat .l{font-size:12px;color:var(--muted)}
.stat .v{font-size:22px;font-weight:650;margin-top:2px}
.stat .v.sm{font-size:15px}
.bar{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:12px}
.chip{border:1px solid var(--line);background:var(--card);color:var(--text);border-radius:99px;padding:5px 12px;font-size:13px;cursor:pointer}
.chip.on{background:var(--accent);border-color:var(--accent);color:#fff}
select.chip{padding:5px 8px}
input.search{border:1px solid var(--line);background:var(--card);color:var(--text);border-radius:99px;padding:6px 12px;font-size:13px;min-width:180px}
.spacer{flex:1}
.tablebox{overflow-x:auto;background:var(--card);border:1px solid var(--line);border-radius:12px}
table{width:100%;border-collapse:collapse;min-width:940px}
th,td{text-align:left;padding:10px 10px;border-bottom:1px solid var(--line);font-size:14px;vertical-align:middle}
th{font-size:12px;color:var(--muted);font-weight:600;white-space:nowrap}
th[data-k]{cursor:pointer;user-select:none}
th[data-k]:hover{color:var(--text)}
tbody tr.g{cursor:pointer}
tbody tr.g:hover td{background:var(--bg)}
tbody tr.g.open td{background:var(--bg)}
.two div{white-space:nowrap}
.two div+div{margin-top:4px}
.team{font-weight:600}
.rk{color:var(--muted);font-size:12px;font-weight:500;margin-right:3px}
.conf{color:var(--muted);font-size:12px;margin-left:4px}
.px{color:var(--muted);font-size:12px;margin-left:4px}
.dim{color:var(--muted)}
.ico{display:inline-block;margin-left:6px;font-size:11px;padding:1px 6px;border-radius:99px;background:var(--soft);color:var(--accent);font-weight:600;vertical-align:1px}
.ico.f{background:transparent;border:1px solid var(--warn);color:var(--warn)}
.tag{display:inline-block;padding:2px 9px;border-radius:99px;font-size:12px;font-weight:600;border:1px solid currentColor}
.t-closed{color:var(--muted)}.t-upcoming{color:var(--up)}.t-missed{color:var(--warn)}
.mv{font-size:12px;font-weight:600;white-space:nowrap}
.mv.up{color:var(--up)}.mv.down{color:var(--down)}
.stale{color:var(--warn);font-size:11px;margin-top:4px;white-space:nowrap}
.tg{display:inline-block;margin-left:6px;font-size:10px;padding:1px 6px;border-radius:4px;font-weight:700;letter-spacing:.3px;vertical-align:1px;border:1px solid currentColor}
.tg-hack{color:var(--warn)}.tg-nohack{color:var(--accent)}.tg-mixed{color:var(--muted)}.tg-dnb{color:#fff;background:var(--down);border-color:var(--down)}.tg-rev{color:#a855f7}
tbody tr.g.dnb td:first-child{box-shadow:inset 4px 0 0 var(--down)}
.dnbnote{color:var(--down);font-size:11px;font-weight:600;margin-top:4px;white-space:nowrap}
.legend{color:var(--muted);font-size:12px;margin:-4px 0 12px}
.stalebar{background:var(--warn);color:#111;border-radius:10px;padding:10px 14px;font-size:14px;font-weight:600;margin:0 0 14px}
.ranges{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:12px}
.rg{flex:1 1 300px;background:var(--card);border:1px solid var(--line);border-radius:12px;padding:10px 14px}
.rg.on{border-color:var(--accent)}
.rg .top{display:flex;align-items:center;gap:6px;font-size:13px;margin-bottom:6px}
.rg .top .l{font-weight:600;margin-right:auto}
.rg .top .h{color:var(--muted);font-size:11px;font-weight:400;margin-left:6px}
.rg input[type=number]{width:62px;border:1px solid var(--line);background:var(--bg);color:var(--text);border-radius:6px;padding:3px 6px;font-size:13px}
.rg .reset{border:0;background:none;color:var(--accent);font-size:12px;cursor:pointer;padding:0 0 0 6px}
.dual{position:relative;height:26px}
.dual .track{position:absolute;left:0;right:0;top:11px;height:4px;border-radius:2px;background:var(--line)}
.dual .fill{position:absolute;top:11px;height:4px;border-radius:2px;background:var(--accent)}
.dual input[type=range]{position:absolute;left:0;top:0;width:100%;height:26px;margin:0;background:none;pointer-events:none;-webkit-appearance:none;appearance:none}
.dual input[type=range]::-webkit-slider-runnable-track{background:none;height:26px}
.dual input[type=range]::-moz-range-track{background:none}
.dual input[type=range]::-webkit-slider-thumb{-webkit-appearance:none;pointer-events:auto;width:18px;height:18px;margin-top:4px;border-radius:50%;background:var(--card);border:2px solid var(--accent);cursor:pointer}
.dual input[type=range]::-moz-range-thumb{pointer-events:auto;width:16px;height:16px;border-radius:50%;background:var(--card);border:2px solid var(--accent);cursor:pointer}
.detail td{background:var(--bg);padding:4px 12px 16px}
.dgrid{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:12px}
.tcard{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px 14px}
.tcard h3{margin:0 0 8px;font-size:15px}
.hab{display:grid;grid-template-columns:repeat(4,1fr);gap:6px;margin-bottom:10px}
.hab div{background:var(--bg);border-radius:8px;padding:6px 8px}
.hab .l{font-size:11px;color:var(--muted)}
.hab .v{font-weight:650}
.notes{margin:0;padding:0;list-style:none}
.notes li{padding:6px 0;border-top:1px solid var(--line);font-size:14px}
.notes li:first-child{border-top:0}
.nd{color:var(--muted);font-size:12px;margin-right:6px}
.ndate{display:inline-block;min-width:92px;font-size:12px;font-weight:600;margin-right:6px}
.ndate.nodate{color:var(--muted);font-weight:400;font-style:italic}
.none{color:var(--muted);font-size:13px}
.sec{font-size:15px;margin:26px 0 8px}
.small{color:var(--muted);font-size:12px}
.empty{padding:36px 16px;text-align:center;color:var(--muted)}
.tgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:12px}
.foot{color:var(--muted);font-size:12px;margin-top:18px}
button.rf{background:var(--accent);color:#fff;border:0;border-radius:8px;padding:7px 14px;font-size:13px;cursor:pointer}
@media (max-width:600px){.hab{grid-template-columns:repeat(2,1fr)}}
</style>
</head>
<body>
<div class="wrap">
  <h1>NCAAB Lines</h1>
  <p class="sub" id="sub"></p>

  <div class="stalebar" id="stalebar" style="display:none"></div>
  <div class="stats" id="stats"></div>

  <div class="bar">
    <button class="chip on" data-st="all">All games</button>
    <button class="chip" data-st="upcoming">Upcoming</button>
    <button class="chip" data-st="closed">Closed</button>
    <button class="chip" data-x="notes">Has notes</button>
    <button class="chip" data-x="foul">Has foul data</button>
    <button class="chip" data-x="moved">Line moved</button>
    <button class="chip" data-x="hack">Hackers playing</button>
    <button class="chip" data-x="rev">Reverse hackers</button>
    <button class="chip" data-x="dnb">Do not bet</button>
    <select class="chip" id="dateSel"></select>
    <select class="chip" id="confSel"><option value="">All conferences</option></select>
    <input class="search" id="q" placeholder="Search team">
    <button class="chip" id="clear" style="display:none">Clear filters</button>
    <span class="spacer"></span>
    
  </div>

  <div class="ranges">
    <div class="rg" id="rg-sp"><div class="top"><span class="l">Spread<span class="h">favorite's number, e.g. -7.5 = 7.5</span></span><input type="number" step="0.5" id="sp-min"> to <input type="number" step="0.5" id="sp-max"><button class="reset" id="sp-reset">Reset</button></div>
      <div class="dual"><div class="track"></div><div class="fill" id="sp-fill"></div><input type="range" step="0.5" id="sp-lo"><input type="range" step="0.5" id="sp-hi"></div></div>
    <div class="rg" id="rg-tt"><div class="top"><span class="l">Total</span><input type="number" step="0.5" id="tt-min"> to <input type="number" step="0.5" id="tt-max"><button class="reset" id="tt-reset">Reset</button></div>
      <div class="dual"><div class="track"></div><div class="fill" id="tt-fill"></div><input type="range" step="0.5" id="tt-lo"><input type="range" step="0.5" id="tt-hi"></div></div>
  </div>
  <p class="legend">Team badges from your notes: <span class="tg tg-hack">HACK</span> fouls when trailing late &middot; <span class="tg tg-nohack">NO HACK</span> doesn't &middot; <span class="tg tg-mixed">MIXED</span> both seen &middot; <span class="tg tg-rev">REVERSE HACK</span> fouls when leading &middot; <span class="tg tg-dnb">DO NOT BET</span></p>
  <div class="tablebox">
    <table>
      <thead id="head"></thead>
      <tbody id="rows"></tbody>
    </table>
    <div class="empty" id="empty" style="display:none"></div>
  </div>
  <p class="small" style="margin-top:8px">Click a game to see both teams' fouling habits and notes. Click a Spread or Total column header to sort highest to lowest (click again for lowest first). Spread and moneyline are listed away team first. Open = first line the tracker saw for that game. Close = book average at the last snapshot before tip-off.</p>

  <h2 class="sec">Team notes</h2>
  <div class="bar">
    <button class="chip on" data-tn="today">Playing on selected date</button>
    <button class="chip" data-tn="all">All teams</button>
    <input class="search" id="tq" placeholder="Search notes">
  </div>
  <div class="tgrid" id="teams"></div>

  <p class="foot" id="foot"></p>
</div>

<script>
var DATA = __DATA__;
(function(){
  var $=function(id){return document.getElementById(id)};
  function esc(s){var d=document.createElement("div");d.textContent=s==null?"":String(s);return d.innerHTML}
  function sg(v){if(v==null||v==="")return "\u2013";var n=+v;if(isNaN(n))return esc(v);n=Math.round(n*10)/10;return (n>0?"+":"")+n}
  function pl(v){if(v==null||v==="")return "\u2013";var n=Math.round(+v*10)/10;return String(n)}
  function px(v){return v==null||v===""?"":"<span class='px'>("+sg(v)+")</span>"}
  function clock(s){if(s==null||s==="")return "\u2013";s=Math.round(+s);return Math.floor(s/60)+":"+(s%60<10?"0":"")+(s%60)}
  var MON=["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"],DOW=["Sun","Mon","Tue","Wed","Thu","Fri","Sat"];
  function dayLabel(s){var m=String(s).match(/^(\d{4})-(\d{2})-(\d{2})$/);if(!m)return s;var d=new Date(Date.UTC(+m[1],+m[2]-1,+m[3]));return DOW[d.getUTCDay()]+", "+MON[+m[2]-1]+" "+(+m[3])+(s===DATA.today?" (today)":"")}
  function tipLabel(t){if(!t)return "\u2013";var m=t.match(/T(\d{2}):(\d{2})/);if(!m)return t;var h=+m[1],ap=h>=12?"PM":"AM";h=h%12||12;return h+":"+m[2]+" "+ap}
  function teamLabel(g,s){var n=g[s+"_name"],a=g[s],c=g[s+"_conf"]||"";return "<span class='team'>"+esc(n||a)+"</span><span class='conf'>"+(n&&n!==a?esc(a)+(c?" \u00b7 ":""):"")+esc(c)+"</span>"}
  function tags(t){return String(t||"").split(/\s*[;,]\s*/).filter(Boolean)}
  function tagBadge(t){return tags(t).map(function(x){var c={"HACK":"tg-hack","NO HACK":"tg-nohack","MIXED":"tg-mixed","DO NOT BET":"tg-dnb","REVERSE HACK":"tg-rev"}[x]||"tg-mixed";return "<span class='tg "+c+"'>"+esc(x)+"</span>"}).join("")}
  function rank(r){return r&&+r>0&&+r<=25?"<span class='rk'>#"+(+r)+"</span>":""}

  var games=DATA.games||[];
  var store={};try{store=JSON.parse(localStorage.getItem("ncaab_dash")||"{}")||{}}catch(e){}
  var st=store.st||"all", ex=store.ex||{}, conf=store.conf||"", q="", tn="today", tq="";
  var dates=Object.keys(games.reduce(function(a,g){a[g.game_date]=1;return a},{})).sort().reverse();
  var date=dates.indexOf(DATA.today)>=0?DATA.today:(dates[0]||DATA.today);
  var sort={k:"tip",d:1}, open={};
  function save(){try{localStorage.setItem("ncaab_dash",JSON.stringify({st:st,ex:ex,conf:conf}))}catch(e){}}

  function tipMs(g){var t=Date.parse(g.tip_et||"");return isNaN(t)?null:t}
  function status(g){if(g.closed_at)return "closed";var t=tipMs(g);return t!==null&&t<=Date.now()?"closed":"upcoming"}
  function staleMin(g){
    var t=tipMs(g),l=Date.parse(g.last_pregame_at||"");
    if(t===null||isNaN(l)||status(g)!=="closed")return 0;
    var m=Math.round((t-l)/60000);return m>20?m:0;
  }
  function ago(m){return m>=60?Math.floor(m/60)+"h "+(m%60)+"m":m+" min"}
  function hasNotes(g){return !!(g.away_notes||g.home_notes)}
  function hasFoul(g){return !!(g.away_habit||g.home_habit)}
  function moved(g){return (g.spread_move&&+g.spread_move!==0)||(g.total_move&&+g.total_move!==0)}
  function isHacker(t){var a=tags(t);return a.indexOf("HACK")>=0||a.indexOf("MIXED")>=0}
  function isRev(t){return tags(t).indexOf("REVERSE HACK")>=0}
  function confs(g){return [g.away_conf,g.home_conf].filter(Boolean)}

  $("dateSel").innerHTML=dates.length?dates.map(function(d){return "<option value='"+d+"'>"+esc(dayLabel(d))+"</option>"}).join(""):"<option>"+esc(dayLabel(DATA.today))+"</option>";
  $("dateSel").value=date;

  function spreadMove(g){
    var m=+g.spread_move;if(!g.spread_move||!m)return "";
    var toward=m>0?g.home:g.away;
    return "<div class='mv up'>Spread "+Math.abs(m)+" \u2192 "+esc(toward)+"</div>";
  }
  function totalMove(g){
    var m=+g.total_move;if(!g.total_move||!m)return "";
    return "<div class='mv "+(m>0?"up":"down")+"'>Total "+(m>0?"\u2191 ":"\u2193 ")+Math.abs(m)+"</div>";
  }
  function num(v,abs){if(v==null||v==="")return null;var n=+v;if(isNaN(n))return null;return abs?Math.abs(n):n}
  function spVal(g){var v=num(g.close_home_spread,true);return v!=null?v:num(g.open_home_spread,true)}
  function ttVal(g){var v=num(g.close_total);return v!=null?v:num(g.open_total)}
  var rng={sp:null,tt:null}, bnd={sp:null,tt:null};
  function bounds(day,f,lo){var vs=day.map(f).filter(function(v){return v!=null});if(!vs.length)return {min:lo,max:lo+1};
    return {min:Math.floor(Math.min.apply(null,vs)),max:Math.ceil(Math.max.apply(null,vs))}}
  function syncRange(k){
    var b=bnd[k],r=rng[k]||{lo:b.min,hi:b.max};
    ["lo","hi"].forEach(function(e){var el=$(k+"-"+e);el.min=b.min;el.max=b.max});
    $(k+"-lo").value=r.lo;$(k+"-hi").value=r.hi;
    if(document.activeElement!==$(k+"-min"))$(k+"-min").value=r.lo;
    if(document.activeElement!==$(k+"-max"))$(k+"-max").value=r.hi;
    var w=b.max-b.min||1;$(k+"-fill").style.left=((r.lo-b.min)/w*100)+"%";$(k+"-fill").style.width=((r.hi-r.lo)/w*100)+"%";
    $("rg-"+k).classList.toggle("on",!!rng[k]);
    $(k+"-reset").style.visibility=rng[k]?"visible":"hidden";
  }
  function setRange(k,lo,hi){
    var b=bnd[k];lo=+lo;hi=+hi;if(isNaN(lo))lo=b.min;if(isNaN(hi))hi=b.max;
    if(lo>hi){var t=lo;lo=hi;hi=t}
    rng[k]=(lo<=b.min&&hi>=b.max)?null:{lo:lo,hi:hi};render();
  }
  var cols=[["tip","Tip (ET)"],[null,"Matchup"],["spo","Spread open"],["spc","Spread close"],["tto","Total open"],["ttc","Total close"],[null,"ML open"],[null,"ML close"],["move","Line move"],["status","Status"]];
  function arrow(k){return sort.k===k?(sort.d>0?" \u25b2":" \u25bc"):""}
  function val(g,k){
    if(k==="tip")return g.tip_et||"";
    if(k==="move")return Math.abs(+g.spread_move||0)+Math.abs(+g.total_move||0)/2;
    if(k==="status")return status(g);
    if(k==="spo")return num(g.open_home_spread,true);
    if(k==="spc")return num(g.close_home_spread,true);
    if(k==="tto")return num(g.open_total);
    if(k==="ttc")return num(g.close_total);
    return "";
  }

  function render(){
    var day=games.filter(function(g){return g.game_date===date});
    var allC={};day.forEach(function(g){confs(g).forEach(function(c){allC[c]=1})});
    var cl=Object.keys(allC).sort();
    $("confSel").innerHTML="<option value=''>All conferences</option>"+cl.map(function(c){return "<option>"+esc(c)+"</option>"}).join("");
    if(conf&&!allC[conf])conf="";
    $("confSel").value=conf;

    var bs=bounds(games,spVal,0),bt=bounds(games,ttVal,140);
    bnd.sp={min:0,max:Math.max(50,bs.max)};
    bnd.tt={min:Math.min(110,bt.min),max:Math.max(190,bt.max)};
    ["sp","tt"].forEach(function(k){if(rng[k]){bnd[k].min=Math.min(bnd[k].min,Math.floor(rng[k].lo));bnd[k].max=Math.max(bnd[k].max,Math.ceil(rng[k].hi))}});
    var list=day.filter(function(g){
      if(st!=="all"&&status(g)!==st)return false;
      if(rng.sp){var v=spVal(g);if(v==null||v<rng.sp.lo||v>rng.sp.hi)return false}
      if(rng.tt){var w=ttVal(g);if(w==null||w<rng.tt.lo||w>rng.tt.hi)return false}
      if(ex.notes&&!hasNotes(g))return false;
      if(ex.foul&&!hasFoul(g))return false;
      if(ex.moved&&!moved(g))return false;
      if(ex.hack&&!(isHacker(g.away_tag)||isHacker(g.home_tag)))return false;
      if(ex.dnb&&!g.dnb)return false;
      if(ex.rev&&!(isRev(g.away_tag)||isRev(g.home_tag)))return false;
      if(conf&&confs(g).indexOf(conf)<0)return false;
      if(q){var s=(g.away+" "+g.home+" "+(g.away_name||"")+" "+(g.home_name||"")).toLowerCase();if(s.indexOf(q)<0)return false}
      return true;
    });
    syncRange("sp");syncRange("tt");
    list.sort(function(a,b){var x=val(a,sort.k),y=val(b,sort.k);if(x==null||y==null)return (x==null)-(y==null)||a.away.localeCompare(b.away);if(typeof x==="string")return sort.d*x.localeCompare(y)||a.away.localeCompare(b.away);return sort.d*(x-y)});

    var closed=day.filter(function(g){return status(g)==="closed"}).length;
    var noted=day.filter(hasNotes).length;
    var big=day.slice().sort(function(a,b){return Math.abs(+b.spread_move||0)-Math.abs(+a.spread_move||0)})[0];
    $("stats").innerHTML=
      "<div class='stat'><div class='l'>Games</div><div class='v'>"+day.length+"</div></div>"+
      "<div class='stat'><div class='l'>Closing lines locked</div><div class='v'>"+closed+"</div></div>"+
      "<div class='stat'><div class='l'>Upcoming</div><div class='v'>"+(day.length-closed)+"</div></div>"+
      "<div class='stat'><div class='l'>Games with a hacker</div><div class='v'>"+day.filter(function(g){return isHacker(g.away_tag)||isHacker(g.home_tag)}).length+"</div></div>"+
      "<div class='stat'><div class='l'>Do-not-bet games</div><div class='v'>"+day.filter(function(g){return g.dnb}).length+"</div></div>"+
      "<div class='stat'><div class='l'>Biggest spread move</div><div class='v sm'>"+(big&&+big.spread_move?esc((big.away_name||big.away)+" @ "+(big.home_name||big.home))+" ("+Math.abs(+big.spread_move)+" pts)":"\u2013")+"</div></div>";

    var anyF=st!=="all"||ex.notes||ex.foul||ex.moved||ex.hack||ex.dnb||ex.rev||conf||q||rng.sp||rng.tt;
    $("clear").style.display=anyF?"":"none";
    document.querySelectorAll("[data-st]").forEach(function(c){c.classList.toggle("on",c.dataset.st===st)});
    document.querySelectorAll("[data-x]").forEach(function(c){c.classList.toggle("on",!!ex[c.dataset.x])});

    $("head").innerHTML="<tr>"+cols.map(function(c){return c[0]?"<th data-k='"+c[0]+"'>"+c[1]+arrow(c[0])+"</th>":"<th>"+c[1]+"</th>"}).join("")+"</tr>";
    $("empty").style.display=list.length?"none":"block";
    $("empty").textContent=day.length?"No games match these filters.":"No games stored for this date yet. Run Track Today's Lines before the first game.";

    $("rows").innerHTML=list.map(function(g){
      var id=g.game_id, isOpen=!!open[id];
      var icons=function(side){return (g[side+"_notes"]?"<span class='ico' title='Has notes'>notes</span>":"")+(g[side+"_habit"]?"<span class='ico f' title='Has fouling data'>foul</span>":"")};
      var row="<tr class='g"+(isOpen?" open":"")+(g.dnb?" dnb":"")+"' data-id='"+esc(id)+"'>"+
        "<td>"+tipLabel(g.tip_et)+"</td>"+
        "<td class='two'><div>"+rank(g.away_rank)+teamLabel(g,"away")+tagBadge(g.away_tag)+icons("away")+"</div><div>"+rank(g.home_rank)+teamLabel(g,"home")+tagBadge(g.home_tag)+icons("home")+"</div></td>"+
        "<td class='two dim'><div>"+sg(g.open_away_spread)+"</div><div>"+sg(g.open_home_spread)+"</div></td>"+
        "<td class='two'><div><strong>"+sg(g.close_away_spread)+"</strong>"+px(g.close_away_spread_price)+"</div><div><strong>"+sg(g.close_home_spread)+"</strong>"+px(g.close_home_spread_price)+"</div></td>"+
        "<td class='dim'>"+pl(g.open_total)+"</td>"+
        "<td class='two'><div>o <strong>"+pl(g.close_total)+"</strong>"+px(g.close_over_price)+"</div><div>u <strong>"+pl(g.close_total)+"</strong>"+px(g.close_under_price)+"</div></td>"+
        "<td class='two dim'><div>"+sg(g.open_away_ml)+"</div><div>"+sg(g.open_home_ml)+"</div></td>"+
        "<td class='two'><div><strong>"+sg(g.close_away_ml)+"</strong></div><div><strong>"+sg(g.close_home_ml)+"</strong></div></td>"+
        "<td>"+(spreadMove(g)+totalMove(g)||"<span class='dim'>\u2013</span>")+"</td>"+
        "<td>"+(g.close_source==="missed"?"<span class='tag t-missed' title='Covers added this game after tip-off, or tracking started late'>Missed close</span>":"<span class='tag t-"+status(g)+"'>"+(status(g)==="closed"?"Closed":"Upcoming")+"</span>")+(staleMin(g)?"<div class='stale' title='The tracker was not running at tip-off, so this is the last line it saw.'>Last line "+ago(staleMin(g))+" before tip</div>":"")+(g.dnb?"<div class='dnbnote'>DO NOT BET: "+esc(g.dnb)+"</div>":"")+"</td></tr>";
      if(isOpen){
        row+="<tr class='detail'><td colspan='10'><div class='dgrid'>"+teamCard(g.away,g.away_name,g.away_rank,g.away_conf,g.away_habit,g.away_notes,g.away_tag)+teamCard(g.home,g.home_name,g.home_rank,g.home_conf,g.home_habit,g.home_notes,g.home_tag)+"</div>"+
          "<div class='small' style='margin-top:8px'>"+(g.close_source==="missed"?"No closing line: this game first showed up on Covers after tip-off (or tracking started late), so only the opening line was saved.</div></td></tr>":"Closing line from: "+esc(g.close_source||"\u2013")+" \u00b7 last pre-game line saved "+esc(String(g.last_pregame_at||"").slice(11,16))+" ET"+(staleMin(g)?" ("+ago(staleMin(g))+" before tip \u2013 the tracker wasn't running at tip-off)":"")+"</div></td></tr>");
      }
      return row;
    }).join("");
    renderTeams(day);
    save();
  }

  function noteList(text){
    if(!text)return "<p class='none'>No notes yet.</p>";
    var MON=["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];
    var items=String(text).split(/\n+/).filter(function(s){return s.trim()}).map(function(l,i){
      var m=l.match(/^\s*\[([^\]]+)\]\s*(?:\(([^)]*)\))?\s*(.*)$/);
      var d=m?m[1].trim():"", kind=m&&m[2]?m[2].trim():"", body=m?m[3]:l, key=-1, label;
      var dm=d.match(/^(\d{4})-(\d{1,2})-(\d{1,2})$/)||d.match(/^()(\d{1,2})\/(\d{1,2})(?:\/(\d{2,4}))?$/);
      if(dm){
        var mo=+dm[2], dy=+dm[3], yr=dm[1]||dm[4]||"";
        if(yr&&yr.length===2)yr="20"+yr;
        if(!yr)yr=(mo>=7?String(new Date().getMonth()>=6?new Date().getFullYear():new Date().getFullYear()-1):String(new Date().getMonth()>=6?new Date().getFullYear()+1:new Date().getFullYear()));
        label=MON[mo-1]+" "+dy+", "+yr; key=(+yr)*10000+mo*100+dy;
      } else label=(d.toLowerCase()==="list"?"No date (list)":(d||"No date"));
      return {key:key,i:i,label:label,dated:key>0,kind:kind,body:body};
    });
    items.sort(function(a,b){return (b.key-a.key)||(a.i-b.i)});
    return "<ul class='notes'>"+items.map(function(it){
      var k=it.kind.toLowerCase(), kc=k==="hack"||k==="fouling"?"tg-hack":(k==="no hack"?"tg-nohack":(k==="do not bet"?"tg-dnb":(k==="reverse hack"?"tg-rev":"")));
      return "<li><span class='ndate"+(it.dated?"":" nodate")+"'>"+esc(it.label)+"</span>"+
        (it.kind?"<span class='tg "+kc+"' style='margin-right:6px'>"+esc(it.kind.toUpperCase())+"</span>":"")+esc(it.body)+"</li>";
    }).join("")+"</ul>";
  }
  function habBlock(h){
    if(!h)return "";
    var n=+h.games_logged||0, nf=+h.no_foul_logged||0, out="";
    if(n){
      out+="<div class='hab'>"+
      "<div><div class='l'>Hacks logged</div><div class='v'>"+n+"</div></div>"+
      "<div><div class='l'>Avg start</div><div class='v'>"+clock(h.avg_time_left_sec)+"</div></div>"+
      "<div><div class='l'>Avg down by</div><div class='v'>"+pl(h.avg_down_by)+"</div></div>"+
      "<div><div class='l'>Sec / pt down</div><div class='v'>"+pl(h.sec_left_per_point_down)+"</div></div>"+
      "</div>"+((h.earliest_start_sec||nf)?"<div class='small' style='margin:-4px 0 8px'>"+(h.earliest_start_sec?"Earliest start: "+clock(h.earliest_start_sec)+" left":"")+(h.earliest_start_sec&&nf?" \u00b7 ":"")+(nf?nf+" time"+(nf>1?"s":"")+" didn't hack when trailing":"")+"</div>":"");
    } else if(nf){
      out+="<div class='small' style='margin:0 0 8px'>No hacks logged \u00b7 "+nf+" time"+(nf>1?"s":"")+" didn't hack when trailing</div>";
    }
    return out;
  }
  function teamCard(abbr,name,rk,cf,h,notes,tag){
    var title=name&&abbr&&name!==abbr?name+" ("+abbr+")":(name||abbr);
    return "<div class='tcard'><h3>"+rank(rk)+esc(title)+"<span class='conf'>"+esc(cf||"")+"</span>"+tagBadge(tag)+"</h3>"+
      (h?habBlock(h):"<p class='none' style='margin:0 0 8px'>No fouling data logged.</p>")+noteList(notes)+"</div>";
  }

  function renderTeams(day){
    var playing={},confs2={};day.forEach(function(g){[g.away_note_team,g.home_note_team].forEach(function(t){if(t)playing[t]=1});[g.away_conf,g.home_conf].forEach(function(c){if(c)confs2[c]=1})});
    var list=(DATA.teams||[]).filter(function(t){
      if(tn==="today"&&!(playing[t.team]||(t.conf&&confs2[t.team])))return false;
      if(tq){var s=(t.abbr+" "+t.team+" "+(t.tag||"")+" "+(t.notes||"")).toLowerCase();if(s.indexOf(tq)<0)return false}
      return true;
    });
    document.querySelectorAll("[data-tn]").forEach(function(c){c.classList.toggle("on",c.dataset.tn===tn)});
    $("teams").innerHTML=list.length?list.map(function(t){return teamCard(t.abbr,t.team,null,"",t.habit,t.notes,t.tag)}).join(""):
      "<div class='tcard none'>"+((DATA.teams||[]).length?"No teams with notes match.":"No team notes yet. Send them to Claude in chat.")+"</div>";
  }

  document.addEventListener("click",function(e){
    var t=e.target;
    var th=t.closest&&t.closest("th[data-k]");
    if(th){var k=th.dataset.k;if(sort.k===k)sort.d=-sort.d;else{sort.k=k;sort.d=(k==="move"||k==="spo"||k==="spc"||k==="tto"||k==="ttc")?-1:1}render();return}
    var tr=t.closest&&t.closest("tr.g");
    if(tr){var id=tr.dataset.id;open[id]=!open[id];render();return}
    if(t.dataset&&t.dataset.st){st=t.dataset.st;render();return}
    if(t.dataset&&t.dataset.x){ex[t.dataset.x]=!ex[t.dataset.x];render();return}
    if(t.dataset&&t.dataset.tn){tn=t.dataset.tn;render();return}
  });
  $("clear").onclick=function(){st="all";ex={};conf="";q="";rng={sp:null,tt:null};$("q").value="";render()};
  ["sp","tt"].forEach(function(k){
    $(k+"-lo").oninput=function(){var hi=+$(k+"-hi").value;setRange(k,Math.min(+this.value,hi),hi)};
    $(k+"-hi").oninput=function(){var lo=+$(k+"-lo").value;setRange(k,lo,Math.max(+this.value,lo))};
    $(k+"-min").onchange=function(){setRange(k,this.value,(rng[k]||bnd[k]).hi)};
    $(k+"-max").onchange=function(){setRange(k,(rng[k]||bnd[k]).lo,this.value)};
    $(k+"-reset").onclick=function(){rng[k]=null;render()};
  });
  $("dateSel").onchange=function(){date=this.value;open={};render()};
  $("confSel").onchange=function(){conf=this.value;render()};
  $("q").oninput=function(){q=this.value.trim().toLowerCase();render()};
  $("tq").oninput=function(){tq=this.value.trim().toLowerCase();render()};

  var gen=DATA.generated?String(DATA.generated).replace("T"," ").slice(0,16):"";
  $("sub").textContent="Opening vs closing lines with your team notes and fouling habits. Updated "+gen+" ET.";
  $("foot").textContent="To load the newest lines, close this tab and double-click Open Dashboard again.";
  var genMs=Date.parse(DATA.generated||"");
  if(!isNaN(genMs)&&Date.now()-genMs>75*60000){
    var mins=Math.round((Date.now()-genMs)/60000);
    $("stalebar").textContent="Not updated for "+(mins>=120?Math.round(mins/60)+" hours":mins+" minutes")+" (last update "+gen+" ET). The tracker hasn't run recently, so these lines may be out of date.";
    $("stalebar").style.display="block";
  }
  render();
})();
</script>
</body>
</html>
'''


if __name__ == "__main__":
    try:
        print(f"Opening {build_dashboard()}")
    except Exception as e:
        print(f"[error] {e}\nCopy this message to Claude.")
        input("\nPress Enter to close...")
