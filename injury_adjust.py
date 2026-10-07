"""Injury adjustment for trade values - uses ONLY what was knowable around the trade date.

Why: the trade values are a point-in-time rating of how good a player is. They don't know the
player just rolled an ankle, is Out for the upcoming game, or is sitting on injured reserve, so a
hurt star can look like a huge "win" for whoever received him. This module discounts those
players. It never looks at how many games a player actually missed afterwards (hindsight).

What counts as "known at the time" (public NFL data from the free nflverse project):
  * The official NFL injury report for the NEXT game week (Out / Doubtful / Questionable). If the
    trade is made Mon-Wed, the report that comes out a few days later describes the injury the
    player already had when the trade was made, so a report published up to ~3.5 days after the
    trade is accepted. (Seasons 2025+ have no publish timestamp in the data, so the week's report
    is assumed to have come out two days before kickoff.)
  * Weekly roster status going into the current/next game week: reserve/injured (IR), PUP or
    non-football-injury list.

How it discounts (all tunable below). For a player with "games out" M and G games left in the
season, the new value is  value * max(FLOOR_FACTOR, 1 - M/G):
    IR / PUP / NFI  -> M = IR_GAMES   (4 = the IR minimum stay)
    Out             -> M = OUT_GAMES
    Doubtful        -> M = DOUBTFUL_GAMES
    Questionable    -> no change (most play)
The original value is always kept (field "v0") so the page can show "was X".

2021-2025 results are pre-computed once and frozen in injury_adjustments.json (like the frozen
trade history). 2026+ trades are computed live each run; if the nflverse download fails the
2026+ trades simply keep their un-adjusted values for that run.
"""
import csv
import datetime as dt
import io
import json
import re
import time
from collections import defaultdict
from pathlib import Path

import requests

HERE = Path(__file__).parent
CACHE_DIR = HERE / "cache"
STATIC_FILE = HERE / "injury_adjustments.json"

IR_GAMES = 4.0         # IR minimum stay
OUT_GAMES = 2.0
DOUBTFUL_GAMES = 1.0
FLOOR_FACTOR = 0.35
MIN_GAMES_LEFT = 3
LAST_WEEK = 17         # fantasy season ends after week 17 (nobody plays week 18)
LOOKAHEAD_DAYS = 3.5
VALUE_FLOOR = 2.0        # never push a value below the normal "floor" value (or below its own value if lower)
IR_PREFIXES = ("RES", "PUP", "NON")

NFLVERSE = "https://github.com/nflverse/nflverse-data/releases/download"
GAMES_URL = "https://raw.githubusercontent.com/nflverse/nfldata/master/data/games.csv"
CACHE_TTL_SECONDS = 3 * 3600


def _fetch_csv(url, cache_name, ttl=CACHE_TTL_SECONDS):
    """Download a CSV (cached on disk); fall back to a stale cache if the download fails."""
    CACHE_DIR.mkdir(exist_ok=True)
    path = CACHE_DIR / cache_name
    fresh = path.exists() and (time.time() - path.stat().st_mtime) < ttl
    if not fresh:
        try:
            r = requests.get(url, timeout=120, allow_redirects=True)
            r.raise_for_status()
            path.write_bytes(r.content)
        except Exception as e:  # noqa: BLE001
            if not path.exists():
                raise
            print(f"  warning: could not refresh {cache_name} ({e}); using cached copy")
    return list(csv.DictReader(io.StringIO(path.read_text(encoding="utf-8", errors="replace"))))


def _norm(n):
    n = n.lower().replace(".", "").replace("'", "").replace("-", " ")
    n = re.sub(r"\b(jr|sr|ii|iii|iv|v)\b", "", n)
    return re.sub(r"\s+", " ", n).strip()


class SeasonData:
    def __init__(self, season, schedule_rows):
        self.season = season
        self.weeks = {}
        for r in schedule_rows:
            if r["season"] != str(season) or r["game_type"] != "REG":
                continue
            w = int(r["week"])
            d = dt.date.fromisoformat(r["gameday"])
            self.weeks[w] = min(self.weeks.get(w, d), d)
        ttl = CACHE_TTL_SECONDS if season >= dt.date.today().year else 365 * 86400
        self.inj = {}
        for r in _fetch_csv(f"{NFLVERSE}/injuries/injuries_{season}.csv", f"nflverse_injuries_{season}.csv", ttl):
            if r.get("game_type") not in (None, "", "REG"):
                continue
            self.inj[(r["gsis_id"], int(r["week"]))] = r
        self.week_pub = {}
        for (gid, w), r in self.inj.items():
            t = self.report_time(r, w)
            if w not in self.week_pub or t < self.week_pub[w]:
                self.week_pub[w] = t
        self.ros = {}
        self.by_name = defaultdict(set)
        for r in _fetch_csv(f"{NFLVERSE}/weekly_rosters/roster_weekly_{season}.csv", f"nflverse_rosters_{season}.csv", ttl):
            w = int(r["week"]) if r.get("week") else 0
            self.ros[(r["gsis_id"], w)] = (r.get("status") or "") + "/" + (r.get("status_description_abbr") or "")
            self.by_name[_norm(r["full_name"])].add((r["gsis_id"], r["position"]))

    def next_week(self, trade_dt):
        """First game week whose games haven't started when the trade was made (US/Eastern-ish)."""
        local = trade_dt - dt.timedelta(hours=5)
        for w in sorted(self.weeks):
            start = self.weeks[w]
            if start > local.date() or (start == local.date() and local.hour < 12):
                return w
        return None

    def report_time(self, row, week):
        if row.get("date_modified"):
            return dt.datetime.fromisoformat(row["date_modified"].replace("Z", "+00:00"))
        d = self.weeks[week]
        return dt.datetime(d.year, d.month, d.day, tzinfo=dt.timezone.utc) - dt.timedelta(days=2)

    def find_player(self, pos, name):
        cands = list(self.by_name.get(_norm(name), ()))
        if len(cands) > 1:
            same = [c for c in cands if c[1] == pos]
            cands = same or cands
        ids = list(dict.fromkeys(c[0] for c in cands))
        return ids[0] if len(ids) == 1 else None


def assess(sd, asset_name, trade_dt):
    """Return None (no adjustment) or {"f": factor, "k": "IR"|"Out"|"Dbt", "i": injury text}."""
    m = re.match(r"(\w+) (.*) - (\w+)$", asset_name)
    if not m or m.group(1) == "DEF":
        return None
    gid = sd.find_player(m.group(1), m.group(2))
    nxt = sd.next_week(trade_dt)
    if not gid or not nxt:
        return None
    cur = nxt - 1
    kind, injury = None, ""
    row = sd.inj.get((gid, nxt))
    window = dt.timedelta(days=LOOKAHEAD_DAYS)
    if row and (sd.report_time(row, nxt) - trade_dt) <= window:
        # the next game week's report (published by, or within a few days of, the trade)
        status = (row.get("report_status") or "").strip()
        injury = row.get("report_primary_injury") or row.get("practice_primary_injury") or ""
        kind = {"Out": "Out", "Doubtful": "Dbt"}.get(status)
    elif not row and sd.week_pub.get(nxt) and sd.week_pub[nxt] <= trade_dt:
        pass  # that week's report is already out and doesn't list him: healthy
    else:
        # next week's report isn't out yet: a player listed Out in the last report is likely still hurt
        prev = sd.inj.get((gid, cur))
        if prev and (prev.get("report_status") or "").strip() == "Out":
            age = trade_dt - sd.report_time(prev, cur)
            if dt.timedelta(0) <= age <= dt.timedelta(days=8):
                kind = "Dbt"
                injury = prev.get("report_primary_injury") or prev.get("practice_primary_injury") or ""
    ros = [sd.ros.get((gid, w), "?") for w in (cur, nxt)]
    if any(s.startswith(IR_PREFIXES) for s in ros):
        kind = "IR"
        if not injury:
            for w in (nxt, cur, cur - 1, cur - 2):
                r = sd.inj.get((gid, w))
                if r and (r.get("report_primary_injury") or r.get("practice_primary_injury")):
                    injury = r.get("report_primary_injury") or r.get("practice_primary_injury")
                    break
    if not kind:
        return None
    games_out = {"IR": IR_GAMES, "Out": OUT_GAMES, "Dbt": DOUBTFUL_GAMES}[kind]
    games_left = max(MIN_GAMES_LEFT, LAST_WEEK - nxt + 1)
    f = max(FLOOR_FACTOR, 1.0 - games_out / games_left)
    return {"f": round(f, 3), "k": kind, "i": injury}


def compute_for_trades(trades):
    """trades: compact trades each with "ts" (ms). Returns {trade_id: {asset_name: adj}}."""
    sched = _fetch_csv(GAMES_URL, "nflverse_games.csv", 7 * 86400)
    sds = {}
    out = {}
    for t in trades:
        if not t.get("ts"):
            continue
        s = t["s"]
        if s not in sds:
            try:
                sds[s] = SeasonData(s, sched)
            except Exception as e:  # noqa: BLE001
                print(f"  warning: no injury data for {s} ({e}); skipping adjustment")
                sds[s] = None
        sd = sds[s]
        if sd is None:
            continue
        when = dt.datetime.fromtimestamp(t["ts"] / 1000, dt.timezone.utc)
        for a in t["ag"] + t["bg"]:
            adj = assess(sd, a["n"], when)
            if adj:
                out.setdefault(str(t["id"]), {})[a["n"]] = adj
    return out


def apply_adjustments(trades, adj_by_trade):
    """Mutates compact trades: keeps the original in v0, writes the adjusted v, flags inj/ij, and
    recomputes va/vb/tv from the assets. Trades with no adjustments are left untouched."""
    n = 0
    for t in trades:
        adj = adj_by_trade.get(str(t["id"]))
        if not adj:
            continue
        for a in t["ag"] + t["bg"]:
            x = adj.get(a["n"])
            if not x or "v0" in a:
                continue
            a["v0"] = a["v"]
            a["v"] = round(max(min(a["v"], VALUE_FLOOR), a["v"] * x["f"]), 1)
            a["inj"] = x["k"]
            if x.get("i"):
                a["ij"] = x["i"]
            n += 1
        t["va"] = round(sum(a["v"] for a in t["ag"]), 2)
        t["vb"] = round(sum(a["v"] for a in t["bg"]), 2)
        t["tv"] = round(t["va"] + t["vb"], 2)
    return n


def adjust_all(trades):
    """Entry point for update_league.py: static adjustments for frozen history + live for trades
    carrying a "ts" timestamp that the static file doesn't cover."""
    static = json.loads(STATIC_FILE.read_text()) if STATIC_FILE.exists() else {}
    n = apply_adjustments(trades, static)
    live = [t for t in trades if t.get("ts") and str(t["id"]) not in static]
    try:
        live_adj = compute_for_trades(live) if live else {}
    except Exception as e:  # noqa: BLE001
        print(f"  warning: live injury adjustment skipped ({e})")
        live_adj = {}
    n += apply_adjustments(live, live_adj)
    print(f"injury adjustment: {n} assets adjusted")
    return n
