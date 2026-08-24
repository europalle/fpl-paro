#!/usr/bin/env python3
"""
fpl_brief.py

Daily Fantasy Premier League decision brief.

Pulls live data from the public FPL API, cross-references it against your own
squad and your mini-league rivals, and writes a markdown brief telling you what
actually needs a decision today.

Everything is framed against the mini-league, not overall rank. A move that
gains you 3 points on the field but 0 on the five people you actually care
about is not a good move.

Designed to run unattended on a schedule. It never makes a transfer for you.

Usage:
    python3 fpl_brief.py --entry 8876628 --league 123456
    python3 fpl_brief.py --entry 8876628 --league 123456 --out "briefs/brief-{date}.md"

Exit codes:
    0  brief written
    1  network or API failure
    2  bad arguments
"""

import argparse
import datetime as dt
import json
import os
import statistics
import sys
import time
import urllib.error
import urllib.request
from collections import defaultdict

BASE = "https://fantasy.premierleague.com/api"
UA = "Mozilla/5.0 (compatible; fpl-brief/2.0)"
THROTTLE = 0.4

# Net transfer momentum percentile that counts as "the market is moving".
PRICE_ALERT_THRESHOLD = 85.0

# Below this many finished gameweeks, one rotation looks identical to a player
# who never plays. The brief refuses to call anyone a dead squad slot until it
# has enough evidence to mean it.
EARLY_SEASON_GWS = 4

# Minutes a player needs before over/underperformance against xG says anything.
UNDERLYING_MIN_MINUTES = 180

# Minutes before per-90 rates are stable enough to steer a captain pick. Below
# this, one 20-minute cameo with a big chance produces a monstrous xGI/90 and
# the armband follows it off a cliff.
XGI_TRUST_MINUTES = 270

# A player needs to be an actual starter to be worth captaining.
CAPTAIN_MIN_MINUTES = 45

# A differential captain must still project within this fraction of the best
# projected score available. Below it, you are not taking a calculated risk,
# you are just captaining a worse player.
CAPTAIN_EP_FLOOR = 0.8

# Before this many finished gameweeks, no points gap is a real gap. Totals are
# tiny, variance is enormous, and a single captain haul erases a fortnight of
# deficit. Chasing here is how you lose a season in August.
STANCE_SETTLE_GWS = 6

# Points per remaining gameweek you need to claw back. This, not the raw gap,
# is what says whether you can afford to be sensible.
PRESSURE_BALANCED = 0.75
PRESSURE_AGGRESSIVE = 2.0

CHIP_LABELS = {
    "wildcard": "Wildcard",
    "freehit": "Free Hit",
    "bboost": "Bench Boost",
    "3xc": "Triple Captain",
    "manager": "Assistant Manager",
}


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------

def _f(v):
    """FPL returns numbers as strings, nulls, and occasionally nothing."""
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def per90(total, minutes):
    return round(total * 90.0 / minutes, 2) if minutes else 0.0


def ordinal(n):
    if 10 <= n % 100 <= 20:
        return f"{n}th"
    return f"{n}{ {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th') }"


def chip_label(name):
    return CHIP_LABELS.get(name, name.title() if name else "")


# --------------------------------------------------------------------------
# transport
# --------------------------------------------------------------------------

def get(path, retries=3):
    url = f"{BASE}/{path.lstrip('/')}"
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    for attempt in range(1, retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=25) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (urllib.error.HTTPError, urllib.error.URLError):
            if attempt == retries:
                raise
            time.sleep(2 * attempt)
    return None


# --------------------------------------------------------------------------
# loading
# --------------------------------------------------------------------------

EXPECTED_ELEMENT_FIELDS = [
    "expected_goals", "expected_assists", "expected_goal_involvements",
    "ep_next", "starts", "transfers_in_event", "transfers_out_event",
    "cost_change_event", "goals_scored", "assists",
]


def check_fields(boot):
    """Warn loudly if the API stopped providing something we rely on.

    Every one of these is read with a default, so a rename would silently
    degrade the brief into confident nonsense rather than crashing. Better to
    say so on stderr where the Actions log will keep it.
    """
    sample = boot["elements"][0] if boot.get("elements") else {}
    missing = [f for f in EXPECTED_ELEMENT_FIELDS if f not in sample]
    if missing:
        print(f"WARNING: bootstrap-static elements are missing expected fields: "
              f"{', '.join(missing)}. Anything derived from them will read as "
              f"zero.", file=sys.stderr)
    return missing


def load_core():
    boot = get("bootstrap-static/")
    check_fields(boot)
    time.sleep(THROTTLE)
    fixtures = get("fixtures/")

    teams = {t["id"]: t["short_name"] for t in boot["teams"]}
    postypes = {p["id"]: p["singular_name_short"] for p in boot["element_types"]}

    players = {}
    for p in boot["elements"]:
        mins = p["minutes"]
        goals = p.get("goals_scored", 0)
        assists = p.get("assists", 0)
        xg = _f(p.get("expected_goals"))
        xa = _f(p.get("expected_assists"))
        xgi = _f(p.get("expected_goal_involvements")) or (xg + xa)
        players[p["id"]] = {
            "id": p["id"],
            "name": p["web_name"],
            "team_id": p["team"],
            "club": teams.get(p["team"], "?"),
            "pos": postypes.get(p["element_type"], "?"),
            "price": p["now_cost"] / 10.0,
            "owned": _f(p["selected_by_percent"]),
            "points": p["total_points"],
            "ppg": _f(p["points_per_game"]),
            "form": _f(p["form"]),
            "minutes": mins,
            "status": p["status"],
            "chance": p["chance_of_playing_next_round"],
            "news": p["news"],
            "in_event": p.get("transfers_in_event", 0),
            "out_event": p.get("transfers_out_event", 0),
            "cost_change_event": p.get("cost_change_event", 0),
            "starts": p.get("starts", 0),
            "goals": goals,
            "assists": assists,
            "xg": xg,
            "xa": xa,
            "xgi": xgi,
            "xg90": per90(xg, mins),
            "xa90": per90(xa, mins),
            "xgi90": per90(xgi, mins),
            # positive means scoring more than the chances deserve
            "overperf": round((goals + assists) - xgi, 2),
            "ep_next": _f(p.get("ep_next")),
        }

    current, nxt = None, None
    for ev in boot["events"]:
        if ev.get("is_current"):
            current = ev
        if ev.get("is_next"):
            nxt = ev
    if nxt is None:
        upcoming = [e for e in boot["events"] if not e["finished"]]
        nxt = upcoming[0] if upcoming else boot["events"][-1]

    finished_gws = sum(1 for e in boot["events"] if e.get("finished"))

    return boot, teams, players, fixtures, current, nxt, finished_gws


def fixture_map(fixtures, teams, start_gw, horizon):
    """club short name -> list of (gw, opponent_label, fdr) for the horizon."""
    out = defaultdict(list)
    for f in fixtures:
        gw = f["event"]
        if gw is None or gw < start_gw or gw >= start_gw + horizon:
            continue
        h, a = teams.get(f["team_h"], "?"), teams.get(f["team_a"], "?")
        out[h].append((gw, f"{a} (H)", f["team_h_difficulty"]))
        out[a].append((gw, f"{h} (A)", f["team_a_difficulty"]))
    for club in out:
        out[club].sort(key=lambda x: x[0])
    return out


def load_league(league_id):
    results, page = [], 1
    name = f"league {league_id}"
    while True:
        data = get(f"leagues-classic/{league_id}/standings/?page_standings={page}")
        name = data["league"]["name"]
        block = data["standings"]
        results.extend(block["results"])
        if not block.get("has_next"):
            break
        page += 1
        time.sleep(THROTTLE)
    return name, results


def load_squads(standings, gw):
    """entry_id -> squad, including the multipliers needed for real ownership."""
    squads = {}
    for s in standings:
        time.sleep(THROTTLE)
        try:
            data = get(f"entry/{s['entry']}/event/{gw}/picks/")
        except urllib.error.HTTPError:
            continue
        picks = data["picks"]
        hist = data.get("entry_history") or {}
        squads[s["entry"]] = {
            "entry": s["entry"],
            "name": s["entry_name"],
            "manager": s["player_name"],
            "rank": s["rank"],
            "total": s["total"],
            "picks": [p["element"] for p in picks],
            "starters": [p["element"] for p in picks if p["position"] <= 11],
            "multipliers": {p["element"]: p["multiplier"] for p in picks},
            "captain": next((p["element"] for p in picks if p["is_captain"]), None),
            "vice": next((p["element"] for p in picks if p["is_vice_captain"]), None),
            "chip": data.get("active_chip"),
            "transfers": hist.get("event_transfers", 0),
            "hit": hist.get("event_transfers_cost", 0),
            "bench_pts": hist.get("points_on_bench", 0),
        }
    return squads


def load_chip_history(standings):
    """entry_id -> [(chip_name, gameweek), ...] used so far this season."""
    out = {}
    for s in standings:
        time.sleep(THROTTLE)
        try:
            data = get(f"entry/{s['entry']}/history/")
        except urllib.error.HTTPError:
            continue
        out[s["entry"]] = [(c.get("name"), c.get("event"))
                           for c in (data.get("chips") or [])]
    return out


def load_recent_transfers(standings, players, gws):
    """entry_id -> {gw: [(out_name, in_name), ...]} for the gameweeks asked for."""
    out = {}
    wanted = set(gws)
    for s in standings:
        time.sleep(THROTTLE)
        try:
            data = get(f"entry/{s['entry']}/transfers/")
        except urllib.error.HTTPError:
            continue
        moves = defaultdict(list)
        for t in (data or []):
            gw = t.get("event")
            if gw not in wanted:
                continue
            pin = players.get(t["element_in"], {}).get("name", t["element_in"])
            pout = players.get(t["element_out"], {}).get("name", t["element_out"])
            moves[gw].append((pout, pin))
        if moves:
            out[s["entry"]] = dict(moves)
    return out


# --------------------------------------------------------------------------
# analysis
# --------------------------------------------------------------------------

def fixture_score(club, fixmap, horizon):
    """Lower is better. Returns (sum_fdr, per_gw_list). Missing gw counts as 3."""
    runs = fixmap.get(club, [])
    fdrs = [f for _, _, f in runs]
    while len(fdrs) < horizon:
        fdrs.append(3)
    return sum(fdrs), runs


def squad_alerts(my_picks, players, finished_gws):
    """Anything in the squad that needs a human decision.

    The dead-slot test scales with how much football has been played. One
    rotation in August is not evidence that a player never starts, and calling
    it evidence produces a panic transfer every week of the early season.
    """
    injured, doubtful, benchwarmers = [], [], []
    for pid in my_picks:
        p = players.get(pid)
        if not p:
            continue
        if p["status"] in ("i", "s", "u"):
            injured.append(p)
        elif p["status"] == "d":
            doubtful.append(p)

    if finished_gws >= EARLY_SEASON_GWS:
        available = 90.0 * finished_gws
        for pid in my_picks:
            p = players.get(pid)
            if not p:
                continue
            if p["starts"] == 0 and p["minutes"] < 0.25 * available:
                benchwarmers.append(p)

    return injured, doubtful, benchwarmers


def price_watch(my_picks, players, all_players):
    """Price movement, read correctly for a player you already own.

    Everything here is in your squad, so a rise is value you have already
    banked, not a reason to buy. Only a fall is actionable, and only if you
    were selling anyway.
    """
    nets = [abs(p["in_event"] - p["out_event"]) for p in all_players.values()]
    if not nets:
        return [], [], []
    cutoff = statistics.quantiles(nets, n=100)[int(PRICE_ALERT_THRESHOLD) - 1]

    rising, falling, changed = [], [], []
    for pid in my_picks:
        p = players.get(pid)
        if not p:
            continue
        if p["cost_change_event"]:
            changed.append(p)
        net = p["in_event"] - p["out_event"]
        if abs(net) < cutoff:
            continue
        (rising if net > 0 else falling).append((p, net))

    rising.sort(key=lambda x: -x[1])
    falling.sort(key=lambda x: x[1])
    changed.sort(key=lambda p: -abs(p["cost_change_event"]))
    return rising, falling, changed


def trusted_xgi90(p):
    """xGI/90 only when there are enough minutes behind it, else None.

    Printing a rate off 60 minutes next to a note saying xG is not yet
    meaningful makes the brief argue with itself.
    """
    return p["xgi90"] if p["minutes"] >= XGI_TRUST_MINUTES else None


def market_movers(players, my_picks, limit=4):
    """Players the market is piling into that you do not own. Actual buy signals."""
    owned = set(my_picks)
    rows = []
    for p in players.values():
        if p["id"] in owned or p["status"] != "a":
            continue
        net = p["in_event"] - p["out_event"]
        if net <= 0:
            continue
        rows.append((p, net))
    rows.sort(key=lambda x: -x[1])
    return rows[:limit]


def ownership_and_eo(squads):
    """Real ownership across the mini-league.

    owners: player_id -> [team names] holding him at all.
    eo:     player_id -> effective ownership percent, counting only starters
            and weighting by multiplier, so a captain counts double and a
            triple captain counts treble. This is the number that decides
            whether a haul actually moves you in the table.
    """
    size = max(len(squads), 1)
    owners = defaultdict(list)
    weight = defaultdict(float)
    for sq in squads.values():
        for pid in sq["picks"]:
            owners[pid].append(sq["name"])
        for pid in sq["starters"]:
            weight[pid] += sq["multipliers"].get(pid, 1)
    eo = {pid: round(100.0 * w / size, 1) for pid, w in weight.items()}
    return owners, eo


def league_position(squads, my_entry):
    # Entry id is the final tiebreak purely so the ordering is STABLE. Without
    # it, two managers level on points swap places between runs and the brief
    # reports a different rank each day from identical data.
    ordered = sorted(squads.items(),
                     key=lambda kv: (kv[1]["rank"], -kv[1]["total"], kv[0]))
    ids = [eid for eid, _ in ordered]
    if my_entry not in ids:
        return None
    idx = ids.index(my_entry)
    me = squads[my_entry]
    leader = ordered[0][1]
    above = ordered[idx - 1][1] if idx > 0 else None
    below = ordered[idx + 1][1] if idx < len(ordered) - 1 else None
    tied = [sq["name"] for eid, sq in ordered
            if eid != my_entry and sq["total"] == me["total"]]
    return {
        # FPL's own rank, not our index into the sorted list. When two managers
        # are level, our index is arbitrary and theirs is not.
        "rank": me["rank"],
        "tied_with": tied,
        "size": len(ordered),
        "me": me,
        "leader": leader,
        "above": above,
        "below": below,
        "gap_leader": me["total"] - leader["total"],
        "gap_above": (me["total"] - above["total"]) if above else None,
        "gap_below": (me["total"] - below["total"]) if below else None,
    }


def risk_stance(pos, finished_gws, total_gws):
    """How much variance your league position actually calls for.

    Judged as points per REMAINING gameweek, not raw points. Twenty-nine behind
    with thirty-seven weeks to play is under a point a week and needs no heroics.
    The same gap with five weeks left is desperate. The old version could not
    tell those apart and told you to gamble in August.
    """
    remaining = max(total_gws - finished_gws, 1)
    behind = abs(pos["gap_leader"])
    above, gap_above = pos["above"], pos["gap_above"]

    if not above:
        catch = ""
    elif gap_above == 0:
        catch = f"level on points with {above['name']}"
    else:
        catch = f"{abs(gap_above)} points behind {above['name']}"

    if finished_gws < STANCE_SETTLE_GWS:
        return ("balanced", (
            f"Only {finished_gws} of {total_gws} gameweeks have been played, so "
            f"no gap in this table is real yet. You are {behind} behind the "
            f"leader and {catch}, which is about {behind / remaining:.2f} points "
            f"per remaining week, less than one good captaincy call. Pick the "
            f"best players available and let the season come to you. Chasing "
            f"this early is how people lose seasons in August."))

    if pos["rank"] == 1:
        lead = abs(pos["gap_below"]) if pos["gap_below"] is not None else 0
        return ("protect", (
            f"You lead by {lead} points with {remaining} weeks left. Own what "
            f"your rivals own. Every template player you share is a week they "
            f"cannot gain on you. Differentials are how leads get thrown away, "
            f"not defended."))

    pressure = behind / remaining

    if pressure < PRESSURE_BALANCED:
        return ("balanced", (
            f"You are {behind} behind and {catch}, with {remaining} weeks left. "
            f"That is {pressure:.2f} points a week, well inside normal variance. "
            f"Take the best expected points on offer. You do not need a gamble "
            f"to close this."))
    if pressure < PRESSURE_AGGRESSIVE:
        return ("lean aggressive", (
            f"You are {behind} behind and {catch}, with {remaining} weeks left. "
            f"That is {pressure:.2f} points a week, which the template alone "
            f"will not deliver. You want one or two players your rivals do not "
            f"have, justified on their own merits rather than picked for "
            f"novelty."))
    return ("aggressive", (
        f"You are {behind} behind and {catch}, with only {remaining} weeks left. "
        f"That is {pressure:.2f} points a week, which sensible management does "
        f"not produce. This gap closes when players your rivals do not own "
        f"return heavily and theirs do not. Take the variance, because the safe "
        f"route runs out of road."))


def captain_shortlist(my_picks, players, fixmap, next_gw, eo, limit=6):
    """Rank your own players for the armband.

    Deliberately does NOT add form and points-per-game together. Early in a
    season they are computed from the same handful of matches, so weighting
    both double counts one good afternoon. Underlying output per 90 carries the
    weight instead, fixture modulates it, and the FPL projection breaks ties.
    """
    rows = []
    for pid in my_picks:
        p = players.get(pid)
        # Goalkeepers and defenders are not captaincy material. Their ceiling
        # is a clean sheet and they cannot be rescued by a hat-trick.
        if not p or p["pos"] in ("GKP", "DEF") or p["status"] != "a":
            continue
        if p["minutes"] < CAPTAIN_MIN_MINUTES:
            continue
        runs = fixmap.get(p["club"], [])
        nxt = next((r for r in runs if r[0] == next_gw), None)
        if not nxt:
            continue
        _, opp, fdr = nxt
        # per-90 rates are only load bearing once there are enough minutes
        # behind them. Before that, lean on the projection instead of
        # pretending a tiny sample is a rate.
        trusted = p["minutes"] >= XGI_TRUST_MINUTES
        score = ((p["xgi90"] * 3.0) if trusted else 0.0) \
            + ((5 - fdr) * 1.2) \
            + (p["ep_next"] * (0.8 if trusted else 2.2))
        rows.append({
            "id": pid, "name": p["name"], "club": p["club"], "opp": opp,
            "fdr": fdr, "form": p["form"],
            "xgi90": p["xgi90"] if trusted else None,
            "ep": p["ep_next"], "eo": eo.get(pid, 0.0),
            "score": round(score, 2),
        })
    rows.sort(key=lambda r: -r["score"])
    return rows[:limit]


def recommend_captain(rows, stance):
    """Pick from the shortlist in a way that matches the league situation."""
    if not rows:
        return None, ""
    top = rows[0]

    if stance == "balanced":
        # No need for ownership games. Take the best player on the board.
        return top, (f"{top['name']} is simply the strongest pick available "
                     f"({top['eo']:.0f}% effective ownership here). Nothing "
                     f"about your position calls for a gamble, so do not "
                     f"manufacture one.")

    if stance == "protect":
        safe = max(rows[:3], key=lambda r: r["eo"])
        if safe["id"] == top["id"]:
            return top, (f"{top['name']} is both the strongest pick and the most "
                         f"owned option here at {top['eo']:.0f}% effective "
                         f"ownership. No reason to be clever.")
        return safe, (f"{safe['name']} at {safe['eo']:.0f}% effective ownership. "
                      f"{top['name']} scores slightly higher, but holding the "
                      f"same armband as your rivals is what defends a lead.")

    # Chasing: prefer lower effective ownership, but only among picks that are
    # genuinely competitive on PROJECTED POINTS. Gating on the composite score
    # lets a good fixture drag a low-ceiling player into contention, and then
    # low ownership hands him the armband. That is how you end up captaining a
    # defender projected for two points because nobody else owns him.
    best_ep = max(r["ep"] for r in rows)
    pool = [r for r in rows if r["ep"] >= CAPTAIN_EP_FLOOR * best_ep] or [top]
    pick = min(pool, key=lambda r: r["eo"])

    if pick["id"] == top["id"]:
        if top["eo"] >= 100:
            return top, (f"{top['name']} anyway. His {top['eo']:.0f}% effective "
                         f"ownership means the armband gains you almost nothing "
                         f"here, but nothing else on your bench is close enough "
                         f"to justify the risk, and captaining a worse player to "
                         f"be different loses more than it wins. Take him, and "
                         f"find your edge in the other ten places.")
        return top, (f"{top['name']} is the strongest pick and only "
                     f"{top['eo']:.0f}% effective ownership. Best of both, take it.")

    return pick, (f"{pick['name']} at {pick['eo']:.0f}% effective ownership "
                  f"against {top['name']} at {top['eo']:.0f}%. {top['name']} "
                  f"projects a little higher, but your rivals have him too, so a "
                  f"haul from him barely moves you in this league.")


def underlying_flags(my_picks, players):
    """Who is riding luck and who is due, on expected goal involvement."""
    over, under = [], []
    for pid in my_picks:
        p = players.get(pid)
        if not p or p["minutes"] < UNDERLYING_MIN_MINUTES:
            continue
        if p["overperf"] >= 1.5:
            over.append(p)
        elif p["overperf"] <= -1.5:
            under.append(p)
    over.sort(key=lambda p: -p["overperf"])
    under.sort(key=lambda p: p["overperf"])
    return over, under


def transfer_targets(sell_price, pos, players, fixmap, horizon, next_gw,
                     exclude_ids, limit=8):
    """Best available replacements at or under the budget."""
    rows = []
    for p in players.values():
        if p["id"] in exclude_ids or p["pos"] != pos:
            continue
        if p["price"] > sell_price + 1e-9:
            continue
        if p["status"] != "a" or p["minutes"] < 45:
            continue
        fsum, runs = fixture_score(p["club"], fixmap, horizon)
        nxt = next((r for r in runs if r[0] == next_gw), None)
        x = trusted_xgi90(p)
        score = ((x * 2.5) if x is not None else 0.0) \
            + (p["ppg"] * (1.0 if x is not None else 2.0)) \
            + (3 * horizon - fsum) * 0.35
        rows.append({
            "name": p["name"], "club": p["club"], "price": p["price"],
            "owned": p["owned"], "xgi90": x, "ppg": p["ppg"],
            "pts": p["points"], "fdr_sum": fsum,
            "next": nxt[1] if nxt else "-", "score": round(score, 2),
        })
    rows.sort(key=lambda r: -r["score"])
    return rows[:limit]


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------

def hours_until(iso):
    if not iso:
        return None
    when = dt.datetime.fromisoformat(iso.replace("Z", "+00:00"))
    return (when - dt.datetime.now(dt.timezone.utc)).total_seconds() / 3600.0


def render(ctx):
    L = []
    add = L.append
    size = ctx["league_size"]

    add(f"# FPL brief: {ctx['today']}")
    add("")
    add(f"**{ctx['league_name']}** | {ctx['my_team_name']} is rank "
        f"**{ctx['my_rank']}** of {size} on {ctx['my_total']} pts")
    add("")

    # gaps that actually matter: the man above you, then the leader
    bits = []
    if ctx["gap_above"] is not None:
        bits.append(f"level with {ctx['above_name']}" if ctx["gap_above"] == 0
                    else f"{abs(ctx['gap_above'])} behind {ctx['above_name']}")
    if ctx["gap_below"] is not None:
        bits.append(f"level with {ctx['below_name']}" if ctx["gap_below"] == 0
                    else f"{abs(ctx['gap_below'])} ahead of {ctx['below_name']}")
    if ctx["my_rank"] > 1:
        bits.append(f"{abs(ctx['gap_leader'])} behind {ctx['leader_name']}")
    if bits:
        add(" | ".join(bits))
        add("")
    if ctx["tied_with"]:
        add(f"Level on points with {', '.join(ctx['tied_with'])}. FPL splits "
            f"you on its own tiebreak, so the position between you can move "
            f"without either of you scoring.")
        add("")

    hrs = ctx["hours_to_deadline"]
    if hrs is not None:
        if hrs < 0:
            add(f"Gameweek {ctx['next_gw']} is underway. Team is locked.")
        elif hrs < 24:
            add(f"## DEADLINE IN {hrs:.1f} HOURS (GW{ctx['next_gw']})")
        else:
            add(f"Deadline for GW{ctx['next_gw']}: **{ctx['deadline_local']}** "
                f"({hrs / 24:.1f} days away)")
    add("")
    add(f"Bank: £{ctx['bank']:.1f}m | Squad value: £{ctx['value']:.1f}m")
    add("")

    # ---- decisions needed
    add("## Needs a decision")
    urgent = False
    if ctx["injured"]:
        urgent = True
        add("")
        add("**Injured or unavailable:**")
        for p in ctx["injured"]:
            add(f"- {p['name']} ({p['club']}) {p['news'] or 'unavailable'}")
    if ctx["doubtful"]:
        urgent = True
        add("")
        add("**Doubtful:**")
        for p in ctx["doubtful"]:
            add(f"- {p['name']} ({p['club']}) {p['chance']}% - {p['news']}")
    if ctx["benchwarmers"]:
        urgent = True
        add("")
        add("**Not playing (dead squad slots):**")
        for p in ctx["benchwarmers"]:
            add(f"- {p['name']} ({p['club']}) £{p['price']:.1f}m, "
                f"{p['minutes']} mins across {ctx['finished_gws']} gameweeks, "
                f"{p['starts']} starts")
    if not urgent:
        add("")
        add("Nothing urgent. Squad is fit and everyone is getting minutes.")
        if ctx["finished_gws"] < EARLY_SEASON_GWS:
            add("")
            add(f"(Only {ctx['finished_gws']} gameweek(s) played, so minutes are "
                f"not yet being judged. One benching is not a pattern.)")
    add("")

    # ---- league position and what it implies
    add("## Where you stand")
    add("")
    add(f"**Stance: {ctx['stance'].upper()}.** {ctx['stance_reason']}")
    add("")

    # ---- captain
    add(f"## Captain for GW{ctx['next_gw']}")
    add("")
    if ctx["captain_pick"]:
        add(f"**Pick: {ctx['captain_pick']['name']} "
            f"({ctx['captain_pick']['club']} vs {ctx['captain_pick']['opp']}).** "
            f"{ctx['captain_reason']}")
        add("")
    add("| Player | Club | Opponent | FDR | xGI/90 | Form | Proj | League EO |")
    add("|---|---|---|---|---|---|---|---|")
    picked_id = ctx["captain_pick"]["id"] if ctx["captain_pick"] else None
    for r in ctx["captains"]:
        chosen = r["id"] == picked_id
        label = f"**{r['name']} (pick)**" if chosen else r["name"]
        add(f"| {label} | {r['club']} | {r['opp']} | {r['fdr']} | "
            f"{r['xgi90'] if r['xgi90'] is not None else 'n/a'} | "
            f"{r['form']} | {r['ep']} | {r['eo']:.0f}% |")
    add("")
    add("Rows are ordered by overall score, so the recommended pick is not "
        "always the top line. When it is not, the reasoning above says why.")
    add("")
    add("League EO is effective ownership inside your mini-league: how much of "
        "the field starts him, with captaincy counted twice and a triple "
        "captain three times, so it can exceed 100%. High EO means a haul gains "
        "you almost nothing here. xGI/90 shows n/a until a player has "
        f"{XGI_TRUST_MINUTES} minutes behind the rate.")
    add("")

    # ---- underlying
    if ctx["over"] or ctx["under"]:
        add("## Underlying numbers")
        add("")
        if ctx["over"]:
            add("**Scoring above their chances (regression risk):**")
            for p in ctx["over"]:
                add(f"- {p['name']}: {p['goals']}G {p['assists']}A from "
                    f"{p['xgi']:.2f} xGI, {p['overperf']:+.2f} above expected")
            add("")
        if ctx["under"]:
            add("**Creating more than they score (buy low):**")
            for p in ctx["under"]:
                add(f"- {p['name']}: {p['goals']}G {p['assists']}A from "
                    f"{p['xgi']:.2f} xGI, {p['overperf']:+.2f} below expected")
            add("")
    elif ctx["finished_gws"] < 3:
        add("## Underlying numbers")
        add("")
        add(f"Not enough minutes played yet for xG to say anything honest. "
            f"Needs {UNDERLYING_MIN_MINUTES} minutes per player.")
        add("")

    # ---- price
    if ctx["changed"] or ctx["rising"] or ctx["falling"] or ctx["movers"]:
        add("## Price watch")
        add("")
        for p in ctx["changed"]:
            d = p["cost_change_event"] / 10.0
            add(f"- {p['name']} {'rose' if d > 0 else 'fell'} £{abs(d):.1f}m "
                f"this gameweek, now £{p['price']:.1f}m.")
        for p, net in ctx["rising"]:
            add(f"- {p['name']} is being bought heavily ({net:+,} net). "
                f"You own him, so this is team value accruing. Nothing to do.")
        for p, net in ctx["falling"]:
            add(f"- {p['name']} is being sold heavily ({net:+,} net) and looks "
                f"likely to drop. Only act if you were selling anyway.")
        if ctx["movers"]:
            add("")
            add("**Market is moving toward these, and you do not own them:**")
            for p, net in ctx["movers"]:
                x = trusted_xgi90(p)
                tail = f", {x} xGI/90" if x is not None else ""
                add(f"- {p['name']} ({p['club']}) £{p['price']:.1f}m, "
                    f"{net:+,} net in{tail}")
        add("")

    # ---- league intel
    add("## Mini-league intel")
    add("")
    if ctx["template"]:
        add("**Template (you and most of the league own these):**")
        add("")
        add(", ".join(f"{n} ({c}/{size}, {e:.0f}% EO)" for n, c, e in ctx["template"]))
        add("")
    if ctx["my_differentials"]:
        add("**Your differentials (nobody else in the league owns these):**")
        add("")
        add(", ".join(ctx["my_differentials"]))
        add("")
    if ctx["leader_only"]:
        add(f"**{ctx['leader_name']} owns, you do not:**")
        add("")
        add(", ".join(ctx["leader_only"]))
        add("")

    # ---- what rivals did
    add("## Rival moves")
    add("")
    if ctx["chip_alerts"]:
        add("**Chips played:**")
        for line in ctx["chip_alerts"]:
            add(f"- {line}")
        add("")
    if ctx["chips_used"]:
        add("**Chips used so far this season:**")
        for line in ctx["chips_used"]:
            add(f"- {line}")
        add("")
    if ctx["rival_transfers"]:
        add("**Transfers:**")
        for line in ctx["rival_transfers"]:
            add(f"- {line}")
        add("")
    if not (ctx["chip_alerts"] or ctx["chips_used"] or ctx["rival_transfers"]):
        add("No chips played and no transfers made by anyone in the league yet.")
        add("")

    # ---- targets
    if ctx["targets"]:
        add("## Replacement options")
        for slot, rows in ctx["targets"].items():
            add("")
            add(f"**Replacing {slot}:**")
            add("")
            add("| Player | Club | £ | Owned% | xGI/90 | PPG | Next | FDR sum |")
            add("|---|---|---|---|---|---|---|---|")
            for r in rows:
                add(f"| {r['name']} | {r['club']} | {r['price']:.1f} | "
                    f"{r['owned']} | "
                    f"{r['xgi90'] if r['xgi90'] is not None else 'n/a'} | "
                    f"{r['ppg']} | {r['next']} | {r['fdr_sum']} |")
        add("")

    add("---")
    add("")
    add("Generated automatically. Effective ownership is based on the last "
        "completed gameweek, since upcoming teams are hidden until the "
        "deadline. Fixture difficulty is the official FDR, which is crude. "
        "Nothing here accounts for team news that broke in the last hour, so "
        "check press conferences before the deadline.")
    return "\n".join(L)


# --------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Generate a daily FPL decision brief.")
    ap.add_argument("--entry", type=int, required=True, help="your FPL entry ID")
    ap.add_argument("--league", type=int, required=True, help="classic league ID")
    ap.add_argument("--horizon", type=int, default=6, help="fixture lookahead in GWs")
    ap.add_argument("--out", default=None,
                    help="output path; {date} expands to today's ISO date "
                         "(default: stdout)")
    args = ap.parse_args()

    boot, teams, players, fixtures, current, nxt, finished_gws = load_core()
    next_gw = nxt["id"]
    last_gw = current["id"] if current else max(1, next_gw - 1)

    fixmap = fixture_map(fixtures, teams, next_gw, args.horizon)

    time.sleep(THROTTLE)
    entry = get(f"entry/{args.entry}/")
    bank = entry.get("last_deadline_bank", 0) / 10.0
    value = entry.get("last_deadline_value", 1000) / 10.0

    time.sleep(THROTTLE)
    league_name, standings = load_league(args.league)
    squads = load_squads(standings, last_gw)

    me = squads.get(args.entry)
    if not me:
        sys.exit(f"Entry {args.entry} is not in league {args.league} "
                 f"(or its GW{last_gw} picks are not public yet).")
    my_picks = me["picks"]

    owners, eo = ownership_and_eo(squads)
    pos = league_position(squads, args.entry)
    total_gws = len(boot["events"]) or 38
    stance, stance_reason = risk_stance(pos, finished_gws, total_gws)
    size = len(squads)

    injured, doubtful, benchwarmers = squad_alerts(my_picks, players, finished_gws)
    rising, falling, changed = price_watch(my_picks, players, players)
    movers = market_movers(players, my_picks)
    over, under = underlying_flags(my_picks, players)

    captains = captain_shortlist(my_picks, players, fixmap, next_gw, eo)
    captain_pick, captain_reason = recommend_captain(captains, stance)

    template = sorted(
        [(players[pid]["name"], len(v), eo.get(pid, 0.0))
         for pid, v in owners.items()
         if len(v) > size / 2 and pid in my_picks],
        key=lambda x: -x[1])

    my_differentials = [players[pid]["name"] for pid in my_picks
                        if len(owners.get(pid, [])) == 1
                        and players[pid]["minutes"] >= 45
                        and players[pid]["status"] == "a"]

    leader = pos["leader"]
    leader_only = [f"{players[pid]['name']} ({players[pid]['points']} pts)"
                   for pid in sorted(
                       [q for q in leader["picks"] if q not in my_picks],
                       key=lambda q: -players[q]["points"])[:6]]

    # ---- what the rest of the league actually did
    chips_hist = load_chip_history(standings)
    recent = load_recent_transfers(standings, players, [last_gw, next_gw])

    def who_is(eid):
        return "You" if eid == args.entry else squads.get(eid, {}).get("name", str(eid))

    chip_alerts = []
    for eid, sq in squads.items():
        if sq["chip"]:
            chip_alerts.append(f"{who_is(eid)} played {chip_label(sq['chip'])} "
                               f"in GW{last_gw}.")

    chips_used = []
    for eid, used in chips_hist.items():
        if used:
            chips_used.append(f"{who_is(eid)}: " + ", ".join(
                f"{chip_label(n)} (GW{g})" for n, g in used))

    rival_transfers = []
    for eid, by_gw in recent.items():
        for gw in sorted(by_gw):
            moves = ", ".join(f"{o} -> {i}" for o, i in by_gw[gw])
            tag = "already made for" if gw == next_gw else "in"
            rival_transfers.append(f"{who_is(eid)} {tag} GW{gw}: {moves}")

    # ---- replacement options, grouped so identical budgets share one table
    groups = {}
    for p in (injured + benchwarmers)[:4]:
        groups.setdefault((p["pos"], round(p["price"] + bank, 1)), []).append(p)

    targets = {}
    for (pos_code, budget), plist in list(groups.items())[:3]:
        rows = transfer_targets(budget, pos_code, players, fixmap,
                                args.horizon, next_gw, set(my_picks))
        if rows:
            names = " or ".join(p["name"] for p in plist)
            targets[f"{names} ({pos_code}, £{budget:.1f}m to spend)"] = rows

    dl = nxt.get("deadline_time")
    ctx = {
        "today": dt.date.today().isoformat(),
        "league_name": league_name,
        "league_size": size,
        "finished_gws": finished_gws,
        "my_rank": pos["rank"],
        "my_total": me["total"],
        "my_team_name": me["name"],
        "tied_with": pos["tied_with"],
        "leader_name": leader["name"],
        "above_name": pos["above"]["name"] if pos["above"] else None,
        "below_name": pos["below"]["name"] if pos["below"] else None,
        "gap_leader": pos["gap_leader"],
        "gap_above": pos["gap_above"],
        "gap_below": pos["gap_below"],
        "stance": stance,
        "stance_reason": stance_reason,
        "next_gw": next_gw,
        "deadline_local": dl,
        "hours_to_deadline": hours_until(dl),
        "bank": bank,
        "value": value,
        "injured": injured,
        "doubtful": doubtful,
        "benchwarmers": benchwarmers,
        "rising": rising,
        "falling": falling,
        "changed": changed,
        "movers": movers,
        "over": over,
        "under": under,
        "captains": captains,
        "captain_pick": captain_pick,
        "captain_reason": captain_reason,
        "template": template,
        "my_differentials": my_differentials,
        "leader_only": leader_only,
        "chip_alerts": chip_alerts,
        "chips_used": chips_used,
        "rival_transfers": rival_transfers,
        "targets": targets,
    }

    text = render(ctx)
    if args.out:
        out_path = args.out.replace("{date}", dt.date.today().isoformat())
        parent = os.path.dirname(os.path.abspath(out_path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as fh:
            fh.write(text)
        print(f"Wrote {os.path.abspath(out_path)}")
    else:
        print(text)


if __name__ == "__main__":
    try:
        main()
    except (urllib.error.HTTPError, urllib.error.URLError) as exc:
        sys.exit(f"API unreachable: {exc}")
    except KeyboardInterrupt:
        sys.exit("\nInterrupted.")
