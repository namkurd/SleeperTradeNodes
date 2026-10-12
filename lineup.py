"""Best-lineup value of a trade ("Lineup Δ").

For each side of a trade and each week after it, take the roster the manager really had that week
and ask: what is the best lineup that roster could have fielded?  Then undo the trade (take away
the players received, put back the players given away) and ask the same question.  The difference
is what the trade was worth to that manager's scoring that week; summed over the weeks the trade
was "live" it is the trade's Lineup Δ for that side.

  * "Best lineup" = hindsight-optimal: every player on the roster (bench included) is scored with
    his real points that week and the slots of that season's lineup (QB, RB, WR, TE, FLEX,
    REC_FLEX, SUPER_FLEX, K, DEF) are filled to maximise the total.  Both versions use the same
    rule, so any hindsight advantage cancels out of the difference.
  * Points are the league-scoring points used everywhere else (raw Sleeper stat lines x the
    league's scoring_settings; identical to Sleeper's own weekly points).
  * Everything else the manager did stays as it was (waiver pickups, other trades, drops) - the
    only change is the trade itself.  Depth the manager already had, and depth he gave up, are
    therefore priced at what they really produced, not at a generic replacement level.
  * Window: starts the first week the trade counts (the week after it; the same rule the rest of
    the page uses) and ends the last week the manager still rostered at least one player he
    received, or the end of the season (regular season only if the team missed the playoffs),
    whichever comes first.  Only completed weeks count.
  * The two sides do not have to cancel out: a deal can help both rosters, or hurt both.

2021-2025 results are frozen in lineup_static.json (like the trade history); 2026+ trades are
recomputed on every run from completed weeks.
"""
import json
from pathlib import Path

import par

HERE = Path(__file__).parent
STATIC_FILE = HERE / "lineup_static.json"
MAX_WEEK = 17      # last week of the fantasy season (NFL week 18 is not played in this league)

# which positions may fill each lineup slot
ELIGIBLE = {
    "QB": {"QB"}, "RB": {"RB"}, "WR": {"WR"}, "TE": {"TE"}, "K": {"K"}, "DEF": {"DEF"},
    "FLEX": {"RB", "WR", "TE"}, "REC_FLEX": {"WR", "TE"}, "WRRB_FLEX": {"RB", "WR"},
    "SUPER_FLEX": {"QB", "RB", "WR", "TE"},
}
IGNORED_SLOTS = {"BN", "IR", "TAXI"}


def lineup_slots(roster_positions):
    """Lineup slots of a season, ordered most-restrictive first (so a greedy fill is optimal: the
    eligible sets form a chain  REC_FLEX < FLEX < SUPER_FLEX)."""
    slots = [s for s in roster_positions if s not in IGNORED_SLOTS]
    for s in slots:
        if s not in ELIGIBLE:
            raise ValueError(f"unsupported lineup slot {s}")
    return sorted(slots, key=lambda s: (len(ELIGIBLE[s]), s))


def best_lineup(slots, pool):
    """pool = [(pid, position, points)].  Returns (total_points, [pids used]).
    Greedy over slots from the most to the least restrictive, always taking the highest-scoring
    unused eligible player - optimal because the eligible sets are nested."""
    used, total = set(), 0.0
    ranked = sorted(pool, key=lambda p: -p[2])
    for s in slots:
        elig = ELIGIBLE[s]
        for pid, pos, pts in ranked:
            if pid in used or pos not in elig:
                continue
            used.add(pid)
            total += pts
            break
    return total, used


def _roster_positions(league_id):
    p = par.CACHE_DIR / f"lineup_league_{league_id}.json"
    if p.exists():
        return json.loads(p.read_text())
    L = par._get(f"{par.SLEEPER}/league/{league_id}")
    pos = L["roster_positions"]
    par.CACHE_DIR.mkdir(exist_ok=True)
    p.write_text(json.dumps(pos))
    return pos


class _Season:
    """Everything needed to evaluate rosters of one season."""

    def __init__(self, U, season, league_id):
        self.U, self.season, self.league_id = U, season, league_id
        self.slots = lineup_slots(_roster_positions(league_id))
        self.scoring = par._league(league_id)["scoring_settings"]
        self.players = U.load_players_cache()
        self.reg_end = U.reg_season_end_week(season)
        self.done = min(MAX_WEEK, U.last_complete_week(season))
        self._matchups, self._pts = {}, {}
        self._bracket = "unset"

    def matchups(self, week):
        if week not in self._matchups:
            self._matchups[week] = self.U.fetch_matchup_week(self.season, self.league_id, week)
        return self._matchups[week]

    def roster(self, week, roster_id):
        m = self.matchups(week)
        if not m:
            return None
        return next((e for e in m if e.get("roster_id") == roster_id), None)

    def pts(self, week):
        if week not in self._pts:
            S = par._stats(self.season, week)
            out = {}
            for pid, s in S.items():
                if pid.startswith("TEAM_"):
                    continue
                if (s.get("gp") or 0) <= 0 and not (s.get("gms_active") or 0):
                    continue
                out[pid] = sum(v * self.scoring[k] for k, v in s.items() if k in self.scoring)
            self._pts[week] = out
        return self._pts[week]

    def pool(self, week, pids):
        P = self.pts(week)
        out = []
        for pid in pids:
            pos = (self.players.get(pid) or {}).get("position")
            if pos:
                out.append((pid, pos, P.get(pid, 0.0)))
        return out

    def drops(self):
        if getattr(self, "_drops", None) is None:
            self._drops = self.U.build_drops_index(self.league_id)
        return self._drops

    def departure(self, roster_id, pid, start_week, cap):
        """When did `pid` leave roster_id's roster (first missing week in start_week..cap), and how
        (dropped / traded on / unknown)?  None while he is still there at the end of the window."""
        for wk in range(start_week, cap + 1):
            e = self.roster(wk, roster_id)
            if e is None:
                return None
            if str(pid) not in set(map(str, e.get("players") or [])):
                last = wk - 1
                kind = self.U.find_departure_type(self.drops(), last, roster_id, str(pid))
                return {"k": {"traded": "traded", "dropped": "dropped"}.get(kind, "left"), "w": last}
        return None

    def made_playoffs(self, roster_id):
        if self._bracket == "unset":
            U = self.U
            st = U.nfl_state_memo()
            over = U.is_regular_season_over(st, self.season, self.reg_end)
            self._bracket = U.fetch_winners_bracket(self.league_id) if over else None
        return self._bracket is None or roster_id in self._bracket


def side_value(S, roster_id, received, given, start_week, names=None, other_roster=None):
    """Lineup Δ for the manager of roster_id who received `received` pids and gave `given` pids.
    Also reports a per-player effect (leave-one-out): a received player's figure is how much the best
    lineup drops when just he is removed (everything else as is); a given-up player's figure is minus
    what the best lineup would gain if just he were put back.  With overlapping players the figures
    need not add up to the total."""
    names = names or {}
    cap = MAX_WEEK if S.made_playoffs(roster_id) else S.reg_end
    cap = min(cap, S.done)
    rec, giv = set(map(str, received)), set(map(str, given))
    tot_with = tot_without = 0.0
    weeks = []
    phi, started = {}, {}
    end = None
    for wk in range(start_week, cap + 1):
        e = S.roster(wk, roster_id)
        if e is None:
            break
        have = set(map(str, e.get("players") or []))
        if not (have & rec):
            break                      # none of the players he received is still on his roster
        base = have - rec
        parts = [(p, True) for p in sorted(have & rec)] + [(p, False) for p in sorted(giv)]   # (pid, is_received)
        pool = lambda pids: S.pool(wk, pids)
        a, used_with = best_lineup(S.slots, pool(have))
        b, used_without = best_lineup(S.slots, pool(base | giv))
        tot_with += a
        tot_without += b
        weeks.append([wk, round(a - b, 1)])
        end = wk
        # leave-one-out effect of each part of the deal: best lineup as it really was (all of the trade
        # in place) minus the best lineup with only that player's part undone.  Overlapping players
        # (two RBs for one slot) can make these not add up to the weekly total.
        for p, is_rec in parts:
            alt = (have - {p}) if is_rec else (have | {p})
            phi[p] = phi.get(p, 0.0) + (a - best_lineup(S.slots, pool(alt))[0])
            if (is_rec and p in used_with) or (not is_rec and p in used_without):
                started[p] = started.get(p, 0) + 1
    if not weeks:
        return None
    out = {"d": round(tot_with - tot_without, 1), "on": round(tot_with, 1), "off": round(tot_without, 1),
           "w0": start_week, "w1": end, "n": len(weeks), "wk": weeks}
    pl = []
    for p in sorted(phi, key=lambda q: -abs(phi[q])):
        row = {"n": names.get(p, p), "d": round(phi[p], 1), "s": started.get(p, 0), "r": 1 if p in rec else 0}
        try:
            dep = S.departure(roster_id if p in rec else other_roster, p, start_week, cap) if (p in rec or other_roster) else None
        except Exception:  # noqa: BLE001 - departure notes are informational only
            dep = None
        if dep:
            row["x"] = dep
        pl.append(row)
    out["pl"] = pl
    return out


def trade_value(S, raw, created_ms):
    """raw = trade dict from fetch_trades_for_season.  a_gave goes to B, b_gave goes to A."""
    start = S.U.first_counted_week(raw["season"], raw["week"], created_ms or raw.get("created_ms"))
    a_gave = [str(x["id"]) for x in raw["a_gave_raw"]]
    b_gave = [str(x["id"]) for x in raw["b_gave_raw"]]
    names = {str(x["id"]): x["name"] for x in raw["a_gave_raw"] + raw["b_gave_raw"]}
    va = side_value(S, raw["roster_a"], b_gave, a_gave, start, names, raw["roster_b"])
    vb = side_value(S, raw["roster_b"], a_gave, b_gave, start, names, raw["roster_a"])
    return va, vb, start <= S.done


def _fields(va, vb, settled):
    out = {}
    if va is None and vb is None:
        if not settled:                  # first counted week has not been played yet: nothing to show
            return out
        # played weeks exist but none of the incoming players was ever on the roster for one (dropped at once)
        return {"lga": 0.0, "lgb": 0.0, "lgNet": 0.0, "lgw": 0, "lgd": {"a": None, "b": None}}
    da, db = (va or {}).get("d", 0.0), (vb or {}).get("d", 0.0)
    out["lga"], out["lgb"] = da, db
    out["lgNet"] = round(da - db, 1)
    out["lgw"] = max((va or {}).get("n", 0), (vb or {}).get("n", 0))
    out["lgd"] = {"a": va, "b": vb}
    return out


def add_lineup(trades, U):
    """Mutates compact trades with the best-lineup value (lga / lgb / lgNet / lgw / lgd) and removes
    nothing.  2021-2025 from lineup_static.json; later seasons computed live."""
    static = json.loads(STATIC_FILE.read_text()) if STATIC_FILE.exists() else {}
    live_seasons = sorted({t["s"] for t in trades if str(t["id"]) not in static})
    raw_by_id, seasons = {}, {}
    players = U.load_players_cache() if live_seasons else None
    for s in live_seasons:
        lid = U.LEAGUE_IDS.get(s) or par.HISTORY_LEAGUE_IDS.get(s)
        if not lid:
            continue
        try:
            seasons[s] = _Season(U, s, lid)
            for r in U.fetch_trades_for_season(s, lid, players):
                raw_by_id[str(r["trade_id"])] = r
        except Exception as e:  # noqa: BLE001
            print(f"  warning: lineup values for {s} skipped ({e})")
    n = 0
    for t in trades:
        tid = str(t["id"])
        if tid in static:
            t.update(static[tid])
            n += 1
            continue
        raw, S = raw_by_id.get(tid), seasons.get(t["s"])
        if not raw or not S:
            continue
        try:
            f = _fields(*trade_value(S, raw, t.get("ts")))
        except Exception as e:  # noqa: BLE001
            print(f"  warning: lineup value for trade {tid} skipped ({e})")
            continue
        if f:
            t.update(f)
            n += 1
    print(f"best-lineup values: {n} trades")


def build_static(U, frozen_trades):
    """One-off freeze of the 2021-2025 trades (python lineup.py)."""
    out = {}
    for s in sorted(par.HISTORY_LEAGUE_IDS):
        lid = par.HISTORY_LEAGUE_IDS[s]
        S = _Season(U, s, lid)
        players = U.load_players_cache()
        raw = {str(r["trade_id"]): r for r in U.fetch_trades_for_season(s, lid, players)}
        for t in [x for x in frozen_trades if x["s"] == s]:
            r = raw.get(str(t["id"]))
            if not r:
                print("  no raw trade for", t["id"])
                continue
            f = _fields(*trade_value(S, r, None))
            if f:
                out[str(t["id"])] = f
        print(s, "done")
    STATIC_FILE.write_text(json.dumps(out, separators=(",", ":")) + "\n")
    print("lineup_static.json:", len(out), "trades")


if __name__ == "__main__":
    import importlib.util
    spec = importlib.util.spec_from_file_location("update_league", HERE / "update_league.py")
    U = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(U)
    frozen = json.loads((HERE / "frozen_history_2021_2025.json").read_text())["trades"]
    build_static(U, frozen)
