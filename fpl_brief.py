#!/usr/bin/env python3
"""
fpl_brief.py

Daily Fantasy Premier League decision brief.

Pulls live data from the public FPL API, cross-references it against your own
squad and your mini-league rivals, and writes a markdown brief telling you what
actually needs a decision today.

Designed to run unattended on a schedule. It never makes a transfer for you.
It surfaces the three or four things worth thinking about and shuts up about
the rest.

Usage:
    python3 fpl_brief.py --entry 8876628 --league 123456
    python3 fpl_brief.py --entry 8876628 --league 123456 --horizon 6 --out brief.md

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
UA = "Mozilla/5.0 (compatible; fpl-brief/1.0)"
THROTTLE = 0.4

# How much a price change matters relative to points. Roughly: a 0.1m rise you
# missed is worth about a fifth of a point of real value in most weeks.
PRICE_ALERT_THRESHOLD = 85.0   # net transfer momentum percentile that flags a move


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

def load_core():
    boot = get("bootstrap-static/")
    time.sleep(THROTTLE)
    fixtures = get("fixtures/")

    teams = {t["id"]: t["short_name"] for t in boot["teams"]}
    postypes = {p["id"]: p["singular_name_short"] for p in boot["element_types"]}

    players = {}
    for p in boot["elements"]:
        players[p["id"]] = {
            "id": p["id"],
            "name": p["web_name"],
            "team_id": p["team"],
            "club": teams.get(p["team"], "?"),
            "pos": postypes.get(p["element_type"], "?"),
            "price": p["now_cost"] / 10.0,
            "owned": float(p["selected_by_percent"]),
            "points": p["total_points"],
            "ppg": float(p["points_per_game"] or 0),
            "form": float(p["form"] or 0),
            "minutes": p["minutes"],
            "status": p["status"],
            "chance": p["chance_of_playing_next_round"],
            "news": p["news"],
            "in_event": p.get("transfers_in_event", 0),
            "out_event": p.get("transfers_out_event", 0),
            "cost_change_event": p.get("cost_change_event", 0),
            "starts": p.get("starts", 0),
        }

    # next unfinished gameweek
    current, nxt = None, None
    for ev in boot["events"]:
        if ev.get("is_current"):
            current = ev
        if ev.get("is_next"):
            nxt = ev
    if nxt is None:
        upcoming = [e for e in boot["events"] if not e["finished"]]
        nxt = upcoming[0] if upcoming else boot["events"][-1]

    return boot, teams, players, fixtures, current, nxt


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
    """entry_id -> {'name':..., 'picks':[player_id...], 'captain':id}"""
    squads = {}
    for s in standings:
        time.sleep(THROTTLE)
        try:
            data = get(f"entry/{s['entry']}/event/{gw}/picks/")
        except urllib.error.HTTPError:
            continue
        captain = next((p["element"] for p in data["picks"] if p["is_captain"]), None)
        squads[s["entry"]] = {
            "name": s["entry_name"],
            "manager": s["player_name"],
            "rank": s["rank"],
            "total": s["total"],
            "picks": [p["element"] for p in data["picks"]],
            "captain": captain,
        }
    return squads


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


def squad_alerts(my_picks, players):
    """Anything in the squad that needs a human decision."""
    injured, doubtful, benchwarmers = [], [], []
    for pid in my_picks:
        p = players.get(pid)
        if not p:
            continue
        if p["status"] in ("i", "s", "u"):
            injured.append(p)
        elif p["status"] == "d":
            doubtful.append(p)
        # played essentially nothing all season and isn't new
        if p["minutes"] < 45 and p["starts"] == 0:
            benchwarmers.append(p)
    return injured, doubtful, benchwarmers


def price_watch(my_picks, players, all_players):
    """Flag squad members with strong transfer momentum in either direction."""
    nets = [abs(p["in_event"] - p["out_event"]) for p in all_players.values()]
    if not nets:
        return [], []
    cutoff = statistics.quantiles(nets, n=100)[int(PRICE_ALERT_THRESHOLD) - 1]
    rising, falling = [], []
    for pid in my_picks:
        p = players.get(pid)
        if not p:
            continue
        net = p["in_event"] - p["out_event"]
        if abs(net) < cutoff:
            continue
        (rising if net > 0 else falling).append((p, net))
    rising.sort(key=lambda x: -x[1])
    falling.sort(key=lambda x: x[1])
    return rising, falling


def league_ownership(squads, my_entry):
    """player_id -> (count, [team names]) across the mini-league."""
    owners = defaultdict(list)
    for entry, sq in squads.items():
        for pid in sq["picks"]:
            owners[pid].append(sq["name"])
    return owners


def captain_shortlist(my_picks, players, fixmap, next_gw, limit=5):
    """Rank own players for the armband on next gw fixture + form + role."""
    rows = []
    for pid in my_picks:
        p = players.get(pid)
        if not p or p["pos"] in ("GKP", "DEF"):
            continue
        if p["status"] != "a":
            continue
        runs = fixmap.get(p["club"], [])
        nxt = next((r for r in runs if r[0] == next_gw), None)
        if not nxt:
            continue
        _, opp, fdr = nxt
        # crude but effective: form carries most of the weight, fixture modulates
        score = (p["form"] * 1.6) + (5 - fdr) * 1.4 + (p["ppg"] * 0.6)
        rows.append({
            "name": p["name"], "club": p["club"], "opp": opp,
            "fdr": fdr, "form": p["form"], "ppg": p["ppg"], "score": round(score, 2),
        })
    rows.sort(key=lambda r: -r["score"])
    return rows[:limit]


def transfer_targets(sell_price, pos, players, fixmap, horizon, next_gw,
                     exclude_ids, limit=8):
    """Best available replacements at or under the budget."""
    rows = []
    for p in players.values():
        if p["id"] in exclude_ids or p["pos"] != pos:
            continue
        if p["price"] > sell_price + 1e-9:
            continue
        if p["status"] != "a":
            continue
        if p["minutes"] < 45:          # must actually be playing
            continue
        fsum, runs = fixture_score(p["club"], fixmap, horizon)
        nxt = next((r for r in runs if r[0] == next_gw), None)
        score = (p["form"] * 1.5) + (p["ppg"] * 1.0) + (3 * horizon - fsum) * 0.35
        rows.append({
            "name": p["name"], "club": p["club"], "price": p["price"],
            "owned": p["owned"], "form": p["form"], "ppg": p["ppg"],
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

    add(f"# FPL brief: {ctx['today']}")
    add("")
    add(f"**{ctx['league_name']}** | you are rank **{ctx['my_rank']}** of "
        f"{ctx['league_size']} on {ctx['my_total']} pts | "
        f"{ctx['gap_to_top']:+d} vs leader")
    add("")

    hrs = ctx["hours_to_deadline"]
    if hrs is not None:
        if hrs < 0:
            add(f"Gameweek {ctx['next_gw']} is underway. Team is locked.")
        elif hrs < 24:
            add(f"## DEADLINE IN {hrs:.1f} HOURS (GW{ctx['next_gw']})")
        else:
            add(f"Deadline for GW{ctx['next_gw']}: **{ctx['deadline_local']}** "
                f"({hrs/24:.1f} days away)")
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
        add("**Not playing at all (dead squad slots):**")
        for p in ctx["benchwarmers"]:
            add(f"- {p['name']} ({p['club']}) £{p['price']:.1f}m, "
                f"{p['minutes']} mins all season")
    if not urgent:
        add("")
        add("Nothing urgent. Squad is fit and everyone is getting minutes.")
    add("")

    # ---- price movement
    if ctx["rising"] or ctx["falling"]:
        add("## Price watch")
        for p, net in ctx["rising"]:
            add(f"- {p['name']} rising ({net:+,} net transfers in). "
                f"Buy now if you were going to.")
        for p, net in ctx["falling"]:
            add(f"- {p['name']} falling ({net:+,} net). "
                f"Sell before the drop if you were going to.")
        add("")

    # ---- captain
    add(f"## Captain shortlist for GW{ctx['next_gw']}")
    add("")
    add("| Player | Club | Opponent | FDR | Form | PPG |")
    add("|---|---|---|---|---|---|")
    for r in ctx["captains"]:
        add(f"| {r['name']} | {r['club']} | {r['opp']} | {r['fdr']} | "
            f"{r['form']} | {r['ppg']} |")
    add("")

    # ---- league intel
    add("## Mini-league intel")
    add("")
    if ctx["template"]:
        add("**Owned by most of the league (captaining these gains you nothing):**")
        add("")
        add(", ".join(f"{n} ({c}/{ctx['league_size']})" for n, c in ctx["template"]))
        add("")
    if ctx["my_differentials"]:
        add("**Your differentials (nobody else in the league owns these):**")
        add("")
        add(", ".join(ctx["my_differentials"]))
        add("")
    if ctx["leader_only"]:
        add(f"**{ctx['leader_name']} owns, you don't:**")
        add("")
        add(", ".join(ctx["leader_only"]))
        add("")

    # ---- targets
    if ctx["targets"]:
        add("## Replacement options")
        for slot, rows in ctx["targets"].items():
            add("")
            add(f"**Replacing {slot}:**")
            add("")
            add("| Player | Club | £ | Owned% | Form | Next | FDR sum |")
            add("|---|---|---|---|---|---|---|")
            for r in rows:
                add(f"| {r['name']} | {r['club']} | {r['price']:.1f} | "
                    f"{r['owned']} | {r['form']} | {r['next']} | {r['fdr_sum']} |")
        add("")

    add("---")
    add("")
    add("Generated automatically. Fixture difficulty is the official FDR, which is "
        "crude. Form is last 30 days. Nothing here accounts for team news that "
        "broke in the last hour, so check press conferences before the deadline.")
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

    boot, teams, players, fixtures, current, nxt = load_core()
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

    injured, doubtful, benchwarmers = squad_alerts(my_picks, players)
    rising, falling = price_watch(my_picks, players, players)
    owners = league_ownership(squads, args.entry)
    size = len(squads)

    template = sorted(
        [(players[pid]["name"], len(v)) for pid, v in owners.items()
         if len(v) >= max(3, size // 2) and pid in my_picks],
        key=lambda x: -x[1])

    my_differentials = [players[pid]["name"] for pid in my_picks
                        if len(owners.get(pid, [])) == 1
                        and players[pid]["minutes"] >= 45]

    leader = min(squads.values(), key=lambda s: s["rank"])
    leader_only = [players[pid]["name"] for pid in leader["picks"]
                   if pid not in my_picks]

    captains = captain_shortlist(my_picks, players, fixmap, next_gw)

    # replacement options for anything flagged as dead weight or injured
    targets = {}
    for p in (benchwarmers + injured)[:3]:
        rows = transfer_targets(p["price"] + bank, p["pos"], players, fixmap,
                                args.horizon, next_gw, set(my_picks))
        if rows:
            targets[f"{p['name']} (£{p['price']:.1f}m + £{bank:.1f}m bank)"] = rows

    dl = nxt.get("deadline_time")
    ctx = {
        "today": dt.date.today().isoformat(),
        "league_name": league_name,
        "league_size": size,
        "my_rank": me["rank"],
        "my_total": me["total"],
        "gap_to_top": me["total"] - leader["total"],
        "leader_name": leader["name"],
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
        "captains": captains,
        "template": template,
        "my_differentials": my_differentials,
        "leader_only": leader_only,
        "targets": targets,
    }

    text = render(ctx)
    if args.out:
        out_path = args.out.replace("{date}", dt.date.today().isoformat())
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
