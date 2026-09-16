#!/usr/bin/env python3
"""
Fetches new trades directly from Sleeper (no manual entry, no Excel), values every
2026+ player with the Parse.bot redraft trade-value feed, normalizes everything to a 0-100
per-season scale, merges it with the frozen 2021-2025 dataset, and writes data.json
for the static page to fetch.

Run this from GitHub Actions on a schedule (see .github/workflows/update.yml) or by
hand any time with:  PARSE_API_KEY=pmx_xxx python3 update_league.py

--------------------------------------------------------------------------------
ONE-TIME-PER-YEAR MAINTENANCE: when Sleeper creates next season's league (a new
league_id every year), add one line to LEAGUE_IDS below. That is the only manual
step in this whole pipeline going forward.
--------------------------------------------------------------------------------
"""
import json, os, sys, time
from pathlib import Path
import requests

HERE = Path(__file__).parent
FROZEN_HISTORY = HERE / "frozen_history_2021_2025.json"   # never recomputed - see freeze_historical.py
CACHE_DIR = HERE / "cache"
CACHE_DIR.mkdir(exist_ok=True)
OUTPUT = HERE / "data.json"

# ---- one-time-per-year maintenance lives here ----
LEAGUE_IDS = {
    2026: "1389416556617801728",
    # 2027: "<add next year's league_id here once Sleeper creates it>",
}

# Parse.bot scraper config (matches this league's 2026+ format: 12-team, PPR, 2QB, redraft (this league is redraft, not dynasty))
PARSE_SCRAPER_BASE = "https://api.parse.bot/scraper/2436daea-870d-4e95-91e7-3b45c305429f"
PARSE_API_KEY = os.environ.get("PARSE_API_KEY")
IS_DYNASTY = "false"   # this league is redraft, not dynasty
NUM_QBS = "2"

FLOOR_NORMALIZED = 2.0
DEF_NORMALIZED = 3.0
NORMALIZED_SCALE = 100.0

SLEEPER_BASE = "https://api.sleeper.app/v1"
session = requests.Session()


def sleeper_get(path):
    r = session.get(f"{SLEEPER_BASE}{path}", timeout=30)
    r.raise_for_status()
    return r.json()


def load_players_cache():
    """Sleeper's full player list (~5MB). Cached locally; Sleeper docs say refresh at
    most once a day, so we only refetch if the cache is missing or stale."""
    cache_path = CACHE_DIR / "players_nfl.json"
    if cache_path.exists() and (time.time() - cache_path.stat().st_mtime) < 20 * 3600:
        return json.loads(cache_path.read_text())
    print("Fetching fresh players/nfl from Sleeper (this is ~5MB, only done periodically)...")
    players = sleeper_get("/players/nfl")
    cache_path.write_text(json.dumps(players))
    return players


def abbrev_name(full_first, full_last):
    """Matches the "F. Lastname" style already used throughout the historical dataset."""
    if not full_first:
        return full_last
    return f"{full_first[0]}. {full_last}"


def get_league_chain_ids(season):
    return LEAGUE_IDS.get(season)


def fetch_roster_and_user_maps(league_id):
    rosters = sleeper_get(f"/league/{league_id}/rosters")
    users = sleeper_get(f"/league/{league_id}/users")
    uid_to_name = {u["user_id"]: u["display_name"] for u in users}
    roster_to_name = {r["roster_id"]: uid_to_name.get(r["owner_id"], f"roster{r['roster_id']}") for r in rosters}
    return roster_to_name


def fetch_trades_for_season(season, league_id, players_by_id, max_week=18):
    roster_to_name = fetch_roster_and_user_maps(league_id)
    trades = []
    for week in range(1, max_week + 1):
        try:
            txns = sleeper_get(f"/league/{league_id}/transactions/{week}")
        except requests.HTTPError:
            continue
        for t in txns:
            if t.get("type") != "trade" or t.get("status") != "complete":
                continue
            roster_ids = t.get("roster_ids") or []
            if len(roster_ids) != 2:
                continue  # multi-team trades aren't handled by this network's 2-owner edge model
            r_a, r_b = roster_ids
            adds = t.get("adds") or {}
            drops = t.get("drops") or {}
            a_gave_ids = [pid for pid, dropped_from in drops.items() if dropped_from == r_a]
            b_gave_ids = [pid for pid, dropped_from in drops.items() if dropped_from == r_b]

            def resolve_names(pids):
                out = []
                for pid in pids:
                    if pid.isdigit():
                        p = players_by_id.get(pid)
                        if p:
                            name = abbrev_name(p.get("first_name"), p.get("last_name"))
                        else:
                            name = f"player#{pid}"
                        out.append({"id": int(pid), "name": name, "is_def": False})
                    else:
                        out.append({"id": pid, "name": pid, "is_def": True})  # team code, e.g. "SF"
                return out

            a_gave = resolve_names(a_gave_ids)
            b_gave = resolve_names(b_gave_ids)

            # exclude trades where one side gave nothing tracked (almost always a
            # player-for-FAAB trade - FAAB isn't captured as an asset here)
            if not a_gave or not b_gave:
                continue

            trades.append({
                "trade_id": t["transaction_id"],
                "season": season, "week": week,
                "manager_a": roster_to_name.get(r_a, f"roster{r_a}"),
                "manager_b": roster_to_name.get(r_b, f"roster{r_b}"),
                "a_gave_raw": a_gave, "b_gave_raw": b_gave,
            })
        time.sleep(0.15)  # be polite to Sleeper's API
    return trades


def fetch_parse_snapshot(season, week):
    """One week's full redraft trade-value rankings, cached to disk so re-runs don't re-hit the API
    for weeks that are already in the past (only the current/most-recent week can still change)."""
    cache_path = CACHE_DIR / f"parsebot_{season}_wk{week}.json"
    if cache_path.exists():
        return json.loads(cache_path.read_text())
    if not PARSE_API_KEY:
        raise RuntimeError("PARSE_API_KEY is not set - add it as a repo secret (see README)")
    r = session.get(
        f"{PARSE_SCRAPER_BASE}/get_historical_rankings",
        headers={"X-API-Key": PARSE_API_KEY},
        params={"week": week, "year": season, "num_qbs": NUM_QBS, "is_dynasty": IS_DYNASTY},
        timeout=30,
    )
    r.raise_for_status()
    data = r.json()
    items = (data.get("data") or {}).get("items") or []
    if items:  # only cache non-empty snapshots - an empty one might just mean "not played yet"
        cache_path.write_text(json.dumps(data))
    return data


def value_2026_plus(season, week, asset, season_max_tracker):
    """Returns (normalized_value, note). asset is {'id':..,'name':..,'is_def':bool}."""
    if asset["is_def"]:
        return DEF_NORMALIZED, "DEF-nominal"
    sleeper_id = str(asset["id"])
    # walk backwards from the requested week to find the nearest earlier snapshot that has this player
    for wk in range(week, 0, -1):
        snap = fetch_parse_snapshot(season, wk)
        items = (snap.get("data") or {}).get("items") or []
        if not items:
            continue
        by_sleeper = {str(it["player"].get("sleeperId")): it for it in items if it.get("player")}
        # track the season's running max regardless of whether THIS player is in it
        local_max = max((it["value"] for it in items), default=0)
        season_max_tracker[season] = max(season_max_tracker.get(season, 0), local_max)
        hit = by_sleeper.get(sleeper_id)
        if hit:
            raw = hit["value"]
            smax = season_max_tracker.get(season) or raw
            return round(raw / smax * NORMALIZED_SCALE, 1), f"ok-wk{wk}(parsebot)"
    return FLOOR_NORMALIZED, "never-ranked-floor"


def build_2026_plus_trades():
    players_by_id = load_players_cache()
    season_max_tracker = {}
    all_trades = []
    for season, league_id in sorted(LEAGUE_IDS.items()):
        raw_trades = fetch_trades_for_season(season, league_id, players_by_id)
        for t in raw_trades:
            a_gave = []
            for asset in t["a_gave_raw"]:
                v, note = value_2026_plus(t["season"], t["week"], asset, season_max_tracker)
                a_gave.append({"name": asset["name"], "value": v, "note": note})
            b_gave = []
            for asset in t["b_gave_raw"]:
                v, note = value_2026_plus(t["season"], t["week"], asset, season_max_tracker)
                b_gave.append({"name": asset["name"], "value": v, "note": note})
            value_a = round(sum(x["value"] for x in a_gave), 2)
            value_b = round(sum(x["value"] for x in b_gave), 2)
            all_trades.append({
                "trade_id": t["trade_id"], "season": t["season"], "week": t["week"],
                "manager_a": t["manager_a"], "manager_b": t["manager_b"],
                "a_gave": a_gave, "b_gave": b_gave,
                "value_a_gave": value_a, "value_b_gave": value_b,
                "total_value": round(value_a + value_b, 2),
            })
    return all_trades


def to_compact(trades):
    managers = set()
    out = []
    for t in trades:
        managers.add(t["manager_a"]); managers.add(t["manager_b"])
        out.append({
            "s": t["season"], "w": t["week"], "a": t["manager_a"], "b": t["manager_b"],
            "ag": [{"n": x["name"], "v": x["value"]} for x in t["a_gave"]],
            "bg": [{"n": x["name"], "v": x["value"]} for x in t["b_gave"]],
            "va": t["value_a_gave"], "vb": t["value_b_gave"], "tv": t["total_value"],
            "id": str(t["trade_id"]),
        })
    return managers, out


def main():
    frozen = json.loads(FROZEN_HISTORY.read_text())  # {"managers": [...], "trades": [...]} compact schema, 2021-2025
    new_trades = build_2026_plus_trades()
    new_managers, new_compact = to_compact(new_trades)

    all_managers = sorted(set(frozen["managers"]) | new_managers)
    all_trades = frozen["trades"] + new_compact
    # de-dupe by trade_id in case a run overlaps a previous one
    seen = set()
    deduped = []
    for t in all_trades:
        if t["id"] in seen:
            continue
        seen.add(t["id"])
        deduped.append(t)

    result = {"managers": all_managers, "trades": deduped}
    OUTPUT.write_text(json.dumps(result, separators=(",", ":")))
    print(f"wrote {OUTPUT}: {len(deduped)} trades total ({len(new_compact)} from 2026+), {len(all_managers)} managers")


if __name__ == "__main__":
    main()
