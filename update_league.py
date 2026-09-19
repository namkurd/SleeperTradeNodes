#!/usr/bin/env python3
"""
Fetches new trades directly from Sleeper (no manual entry, no Excel), values every
2026+ player with the Parse.bot redraft trade-value feed, normalizes everything to a 0-100
per-season scale, tracks each traded asset's REAL fantasy points in the acquiring manager's
starting lineup (Sleeper matchup data), merges it all with the frozen 2021-2025 dataset, and
writes data.json for the static page to fetch.

Fantasy-points tracking fills in gradually as the season is played: each run only has data
through the most recently completed week, and re-running later (the whole point of the
schedule) adds newly played weeks to every still-open trade automatically.

Tracking rules (mirrors the frozen 2021-2025 pipeline exactly):
  - Counting starts the week AFTER the trade.
  - A benched week scores 0 but keeps the tracking window open; the moment the asset is no
    longer on the acquiring roster at all, tracking stops for good. Sleeper's transaction log
    is used to tell apart "traded away again" from "dropped/waived".
  - If the acquiring manager's team does NOT make the playoffs that season, tracking stops at
    the end of the regular season (week 14 for 2026+) even if they still roster the asset.
    Since the playoff bracket doesn't exist until the regular season ends, this cap is only
    applied once Sleeper's winners_bracket endpoint actually returns it - until then every
    trade is tracked as if still in the regular season, same as the "not played yet" case.
  - Each asset's total is also split into regular-season points vs playoff points.

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

# ---- manager identity: sleeper username -> real first name (confirmed with the owner) ----
USERNAME_TO_FIRST = {
    "GreenBayBlay": "Joe",
    "HaanRolo": "Haan",
    "Hellerch": "Christian",
    "Legendaly": "Aidan",
    "Ozviagin": "Oleg",
    "Stevster77": "Steven",
    "ilovelamp917": "Alex",
    "lalu101": "Ankit",
    "namkurd": "Ben",
    "rrakower": "Ryan",
    "thehebrewhammer24": "Jake",
    "tkitaev": "Tommy",
    "Kohogan18": "Kaitlyn",   # new 2026 owner
    "slondon1": "Stephanie",  # new 2026 owner
}


def canonical_manager(raw_name):
    return USERNAME_TO_FIRST.get(raw_name, raw_name)


def reg_season_end_week(season):
    return 14 if season >= 2026 else 15


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
    uid_to_name = {u["user_id"]: canonical_manager(u["display_name"]) for u in users}
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
                "roster_a": r_a, "roster_b": r_b,
                "a_gave_raw": a_gave, "b_gave_raw": b_gave,
            })
        time.sleep(0.15)  # be polite to Sleeper's API
    return trades


def fetch_league_transactions(league_id, max_week=18):
    """Every transaction across the season (not just trades), used to tell apart "traded away
    again" from "dropped/waived" when an asset leaves a roster. Cached per week; an empty
    response for a given week isn't cached, since more transactions can still happen there
    later in the season (mirrors fetch_matchup_week's caching rule)."""
    all_txns = []
    for week in range(1, max_week + 1):
        cache_path = CACHE_DIR / f"txns_{league_id}_wk{week}.json"
        if cache_path.exists():
            txns = json.loads(cache_path.read_text())
        else:
            try:
                txns = sleeper_get(f"/league/{league_id}/transactions/{week}")
            except requests.HTTPError:
                continue
            if txns:
                cache_path.write_text(json.dumps(txns))
        for t in txns:
            t["_week"] = week
        all_txns.extend(txns)
    return all_txns


def build_drops_index(league_id, max_week=18):
    """{(roster_id, player_id): [(week, txn_type), ...]} for every drop this season."""
    index = {}
    for t in fetch_league_transactions(league_id, max_week):
        if t.get("status") != "complete":
            continue
        for pid, rid in (t.get("drops") or {}).items():
            index.setdefault((rid, str(pid)), []).append((t["_week"], t.get("type")))
    return index


def find_departure_type(drops_index, wk_departed_after, roster_id, pid):
    """The asset was last seen on roster_id's roster in week wk_departed_after and gone by
    wk_departed_after+1. Find the transaction that moved it and say whether it was a trade
    or a drop/waiver. Searches a small window since transaction weeks and roster-snapshot
    weeks can be off by one at the boundary."""
    pid_key = str(pid)
    entries = drops_index.get((roster_id, pid_key), [])
    candidates = [(wk, ttype) for wk, ttype in entries if wk_departed_after <= wk <= wk_departed_after + 1]
    if not candidates:
        candidates = [(wk, ttype) for wk, ttype in entries if wk <= wk_departed_after + 2]
    if not candidates:
        return "unknown"
    candidates.sort(key=lambda c: c[0])
    return "traded" if candidates[-1][1] == "trade" else "dropped"


def fetch_winners_bracket(league_id):
    """The set of roster_ids that made the playoffs, once Sleeper generates the bracket (after
    the regular season ends). Returns None if the bracket doesn't exist yet - deliberately not
    cached in that case, mirroring fetch_matchup_week, so a later scheduled run picks it up
    the moment the playoffs are set."""
    cache_path = CACHE_DIR / f"winners_bracket_{league_id}.json"
    if cache_path.exists():
        data = json.loads(cache_path.read_text())
    else:
        data = sleeper_get(f"/league/{league_id}/winners_bracket")
        if data:
            cache_path.write_text(json.dumps(data))
    if not data:
        return None
    roster_ids = set()
    for game in data:
        for key in ("t1", "t2"):
            rid = game.get(key)
            if isinstance(rid, int):
                roster_ids.add(rid)
    return roster_ids or None


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


def fetch_matchup_week(season, league_id, week):
    """One week's matchup data (starters, points, full rosters) for every team in the league.
    Cached to disk; an empty response usually means that week hasn't been generated by Sleeper
    yet (the season is still in progress), so it's deliberately NOT cached - a later run needs
    to see the real data once that week happens."""
    cache_path = CACHE_DIR / f"matchups_{season}_wk{week}.json"
    if cache_path.exists():
        return json.loads(cache_path.read_text())
    data = sleeper_get(f"/league/{league_id}/matchups/{week}")
    if data:
        cache_path.write_text(json.dumps(data))
    return data


def track_asset(season, league_id, start_week, roster_id, asset_id, made_playoffs, drops_index, max_week=18):
    """How many real fantasy points this asset delivered in the ACQUIRING roster's starting
    lineup, counting from the week after the trade onward, split into regular-season vs
    playoff points. Mirrors the frozen 2021-2025 history's track_asset exactly:
      - a benched week scores 0 but keeps tracking open
      - the moment the asset leaves the roster (traded/dropped again), tracking stops there
        for good, and the transaction log says which one it was
      - if the acquiring manager's team did NOT make the playoffs, tracking is capped at the
        end of the regular season even if they still roster the asset
      - if a week's matchup data doesn't exist yet (season still in progress), tracking just
        stops for now with no stop_reason - a later scheduled run fills in newly played weeks
    Returns (points, reg_points, playoff_points, weeks_started, weeks_benched, stop_week, stop_reason)."""
    reg_end = reg_season_end_week(season)
    cap_week = max_week if made_playoffs else reg_end
    total = reg_total = playoff_total = 0.0
    weeks_started = weeks_benched = 0
    stop_week = None
    stop_reason = None
    pid_key = str(asset_id)
    for wk in range(start_week, max_week + 1):
        if wk > cap_week:
            stop_week = cap_week
            stop_reason = "season_ended"
            break
        data = fetch_matchup_week(season, league_id, wk)
        if not data:
            break  # not played/generated yet - try again on a later scheduled run
        entry = next((e for e in data if e.get("roster_id") == roster_id), None)
        if entry is None or pid_key not in (entry.get("players") or []):
            stop_week = wk - 1
            dtype = find_departure_type(drops_index, stop_week, roster_id, pid_key)
            stop_reason = {"traded": "traded", "dropped": "dropped"}.get(dtype, "unknown_departure")
            break
        started = pid_key in (entry.get("starters") or [])
        pts = (entry.get("players_points") or {}).get(pid_key) or 0.0
        if started:
            total += pts
            if wk <= reg_end:
                reg_total += pts
            else:
                playoff_total += pts
            weeks_started += 1
        else:
            weeks_benched += 1
        time.sleep(0.05)  # be polite to Sleeper's API
    return (round(total, 2), round(reg_total, 2), round(playoff_total, 2),
            weeks_started, weeks_benched, stop_week, stop_reason)


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
        if not raw_trades:
            continue
        bracket = fetch_winners_bracket(league_id)  # None until Sleeper generates the playoffs
        drops_index = build_drops_index(league_id)
        for t in raw_trades:
            start_week = t["week"] + 1
            # until the bracket exists, don't cap anyone - treat it like the regular season is
            # still ongoing (same "wait for real data" spirit as fetch_matchup_week)
            a_made_playoffs = bracket is None or t["roster_a"] in bracket
            b_made_playoffs = bracket is None or t["roster_b"] in bracket
            # a_gave assets end up on B's roster; b_gave assets end up on A's roster
            a_gave = []
            for asset in t["a_gave_raw"]:
                v, note = value_2026_plus(t["season"], t["week"], asset, season_max_tracker)
                fp, fpr, fpo, st, bn, stop_wk, stop_reason = track_asset(
                    t["season"], league_id, start_week, t["roster_b"], asset["id"],
                    b_made_playoffs, drops_index)
                a_gave.append({"name": asset["name"], "value": v, "note": note,
                                "fantasy_points": fp, "reg_points": fpr, "playoff_points": fpo,
                                "weeks_started": st, "weeks_benched": bn,
                                "stop_week": stop_wk, "stop_reason": stop_reason})
            b_gave = []
            for asset in t["b_gave_raw"]:
                v, note = value_2026_plus(t["season"], t["week"], asset, season_max_tracker)
                fp, fpr, fpo, st, bn, stop_wk, stop_reason = track_asset(
                    t["season"], league_id, start_week, t["roster_a"], asset["id"],
                    a_made_playoffs, drops_index)
                b_gave.append({"name": asset["name"], "value": v, "note": note,
                                "fantasy_points": fp, "reg_points": fpr, "playoff_points": fpo,
                                "weeks_started": st, "weeks_benched": bn,
                                "stop_week": stop_wk, "stop_reason": stop_reason})
            value_a = round(sum(x["value"] for x in a_gave), 2)
            value_b = round(sum(x["value"] for x in b_gave), 2)
            fp_a_received = round(sum(x["fantasy_points"] for x in b_gave), 2)
            fp_b_received = round(sum(x["fantasy_points"] for x in a_gave), 2)
            all_trades.append({
                "trade_id": t["trade_id"], "season": t["season"], "week": t["week"],
                "manager_a": t["manager_a"], "manager_b": t["manager_b"],
                "a_gave": a_gave, "b_gave": b_gave,
                "value_a_gave": value_a, "value_b_gave": value_b,
                "total_value": round(value_a + value_b, 2),
                "fp_a_received": fp_a_received, "fp_b_received": fp_b_received,
                "fp_net_a": round(fp_a_received - fp_b_received, 2),
            })
    return all_trades


def to_compact(trades):
    managers = set()
    out = []
    for t in trades:
        managers.add(t["manager_a"]); managers.add(t["manager_b"])
        asset_fields = lambda x: {"n": x["name"], "v": x["value"], "fp": x["fantasy_points"],
                                   "fpr": x["reg_points"], "fpo": x["playoff_points"],
                                   "st": x["weeks_started"], "bn": x["weeks_benched"],
                                   "stop": x["stop_reason"], "stopWk": x["stop_week"]}
        out.append({
            "s": t["season"], "w": t["week"], "a": t["manager_a"], "b": t["manager_b"],
            "ag": [asset_fields(x) for x in t["a_gave"]],
            "bg": [asset_fields(x) for x in t["b_gave"]],
            "va": t["value_a_gave"], "vb": t["value_b_gave"], "tv": t["total_value"],
            "fpa": t["fp_a_received"], "fpb": t["fp_b_received"], "fpNet": t["fp_net_a"],
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
