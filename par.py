"""Points Above Replacement (PAR) for traded players.

  PAR of a traded player  =  points he scored while STARTED for the acquiring manager
                             -  (replacement PPG for his position that season  x  games started)

"Points" are the same league-scoring points the rest of the page uses (the "fp" field), and
"games started" is the same "st" field, so PAR is a pure re-expression of numbers already
shown - nothing is re-tracked.

Replacement level, per season and position (all inputs are public and re-derivable):
  1. Cutoff rank  R = (average number of that position the league's managers actually STARTED
     per week, measured from the real lineups - this handles flex / superflex automatically)
     + (bench depth x number of teams).
     Bench depth per team: QB 0.5, TE 0.5, K 0.5, DEF 0.5, RB 1.0, WR 1.0  (BUFFER below).
     Example: 10 teams, one QB slot -> ~10 starters + 5 = QB15.
  2. Every NFL player at that position is ranked by points per game played that season
     (regular-season weeks that have been completed; players with fewer than MIN_GP_FRAC of
     those games are not ranked).
  3. Replacement PPG = the PPG of the player ranked R.

Points for ALL players (not only rostered ones) are rebuilt from Sleeper's raw weekly stat lines
x this league's own scoring_settings for that season. That reproduces Sleeper's own weekly
points exactly (verified on every rostered player-week, 2021-2026), so the scoring used for
the replacement baseline is identical to the scoring used for the trade points.

2021-2025 tables are frozen in replacement_static.json (like the trade history); the current
season's table is recomputed every run from completed weeks only.
"""
import json
import math
import time
from collections import Counter, defaultdict
from pathlib import Path

import requests

HERE = Path(__file__).parent
CACHE_DIR = HERE / "cache"
STATIC_FILE = HERE / "replacement_static.json"
STATIC_THROUGH_SEASON = 2025

BUFFER = {"QB": 0.5, "RB": 1.0, "WR": 1.0, "TE": 0.5, "K": 0.5, "DEF": 0.5}   # bench depth per team
POSITIONS = list(BUFFER)
MIN_GP_FRAC = 0.4
SLEEPER = "https://api.sleeper.app/v1"
# league ids of the frozen seasons (update_league.LEAGUE_IDS only lists the live ones)
HISTORY_LEAGUE_IDS = {2021: "731686009804308480", 2022: "859919691194462208", 2023: "995809919691444224",
                      2024: "1126270721782575104", 2025: "1256991725184876544"}


def _get(url):
    last = None
    for i in range(4):
        try:
            r = requests.get(url, timeout=60)
            r.raise_for_status()
            return r.json()
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(1.5 * (i + 1))
    raise last


def _league(league_id):
    p = CACHE_DIR / f"par_league_{league_id}.json"
    if p.exists():
        return json.loads(p.read_text())
    L = _get(f"{SLEEPER}/league/{league_id}")
    slim = {"scoring_settings": L["scoring_settings"], "total_rosters": L["total_rosters"]}
    CACHE_DIR.mkdir(exist_ok=True)
    p.write_text(json.dumps(slim))
    return slim


def _stats(season, week):
    """Raw Sleeper stat lines for one COMPLETED week, cached forever."""
    p = CACHE_DIR / f"par_stats_{season}_wk{week}.json"
    if p.exists():
        return json.loads(p.read_text())
    S = _get(f"{SLEEPER}/stats/nfl/regular/{season}/{week}")
    if S:
        CACHE_DIR.mkdir(exist_ok=True)
        p.write_text(json.dumps(S))
    return S or {}


def build_table(U, season):
    """Replacement table for one season from completed regular-season weeks, or None."""
    league_id = U.LEAGUE_IDS.get(season) or HISTORY_LEAGUE_IDS[season]
    L = _league(league_id)
    sc = L["scoring_settings"]
    teams = L["total_rosters"]
    players = U.load_players_cache()
    pos_of = lambda pid: (players.get(pid) or {}).get("position")

    weeks, starters = [], Counter()
    pts = defaultdict(dict)
    for w in range(1, U.reg_season_end_week(season) + 1):
        m = U.fetch_matchup_week(season, league_id, w)     # None until the week is complete
        if not m or not any(e.get("points") for e in m):
            continue
        S = _stats(season, w)
        if not S:
            continue
        weeks.append(w)
        for e in m:
            for s in e.get("starters") or []:
                if s and s != "0":
                    starters[pos_of(s)] += 1
        for pid, s in S.items():
            if pid.startswith("TEAM_") or pid not in players:
                continue
            if (s.get("gp") or 0) <= 0 and not (s.get("gms_active") or 0):
                continue
            pts[pid][w] = sum(v * sc[k] for k, v in s.items() if k in sc)
    nw = len(weeks)
    if not nw:
        return None
    min_gp = max(1, math.ceil(MIN_GP_FRAC * nw))
    table = {}
    for pos in POSITIONS:
        n_start = starters[pos] / nw
        rank = max(1, int(round(n_start + BUFFER[pos] * teams)))
        cand = []
        for pid, wk in pts.items():
            if pos_of(pid) != pos or len(wk) < min_gp:
                continue
            tot = sum(wk.values())
            cand.append((tot / len(wk), len(wk), players[pid].get("full_name") or pid))
        cand.sort(reverse=True)
        if not cand:
            continue
        ppg, gp, who = cand[min(rank, len(cand)) - 1]
        table[pos] = {"st": round(n_start, 1), "buf": round(BUFFER[pos] * teams, 1), "rank": rank,
                      "ppg": round(ppg, 2), "who": who, "gp": gp, "n": len(cand)}
    return {"weeks": nw, "regWeeks": U.reg_season_end_week(season), "teams": teams, "minGp": min_gp, "pos": table}


def get_tables(U, seasons):
    static = json.loads(STATIC_FILE.read_text()) if STATIC_FILE.exists() else {}
    out = {}
    for s in sorted(set(seasons)):
        if s <= STATIC_THROUGH_SEASON and str(s) in static:
            out[str(s)] = static[str(s)]
            continue
        try:
            t = build_table(U, s)
        except Exception as e:  # noqa: BLE001
            print(f"  warning: replacement table for {s} skipped ({e})")
            t = None
        if t:
            out[str(s)] = t
    return out


def add_par(trades, U):
    """Mutates compact trades: asset "par" (points above replacement) and "rp" (replacement PPG
    used); trade "pra" / "prb" (PAR received by A / B) and "prNet" (A minus B). Returns the
    replacement tables so the page can show how they were built."""
    tables = get_tables(U, {t["s"] for t in trades})
    n = 0
    for t in trades:
        tab = (tables.get(str(t["s"])) or {}).get("pos")
        if not tab:
            continue
        ok = True
        for side in ("ag", "bg"):
            for a in t[side]:
                pos = a["n"].split(" ")[0]
                row = tab.get(pos)
                if row is None or a.get("fp") is None:
                    ok = False
                    continue
                a["rp"] = row["ppg"]
                a["par"] = round(a["fp"] - row["ppg"] * (a.get("st") or 0), 1)
                n += 1
        if ok:
            t["pra"] = round(sum(a["par"] for a in t["bg"]), 1)   # A received what B gave
            t["prb"] = round(sum(a["par"] for a in t["ag"]), 1)
            t["prNet"] = round(t["pra"] - t["prb"], 1)
    print(f"points above replacement: {n} assets scored")
    return tables


if __name__ == "__main__":
    # freeze 2021-2025:  python par.py   (writes replacement_static.json)
    import importlib.util
    spec = importlib.util.spec_from_file_location("update_league", HERE / "update_league.py")
    U = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(U)
    out = {}
    for s in range(2021, STATIC_THROUGH_SEASON + 1):
        out[str(s)] = build_table(U, s)
        print(s, out[str(s)]["weeks"], "weeks")
    STATIC_FILE.write_text(json.dumps(out, separators=(",", ":")) + "\n")
