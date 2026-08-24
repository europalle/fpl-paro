#!/usr/bin/env python3
"""
fpl_export.py

Pulls Fantasy Premier League data from the public API and writes it to CSV.

No authentication needed for classic leagues. Everything used here is public
once the relevant gameweek deadline has passed.

Usage:
    python3 fpl_export.py --league 123456 --gw 1
    python3 fpl_export.py --league 123456 --gw 2 --outdir ./fpl_data

Finding your IDs:
    League ID: open the league in the browser, the URL looks like
        https://fantasy.premierleague.com/leagues/123456/standings/c
        -> 123456 is the league ID
    Entry ID:  open your own team, the URL looks like
        https://fantasy.premierleague.com/entry/7654321/event/1
        -> 7654321 is your entry ID (the script finds these automatically
           from the league, so you do not need to pass it)

Outputs:
    players.csv     every player in the game: price, ownership, form, points
    fixtures.csv    all fixtures with FDR for both sides
    standings.csv   the mini-league table
    picks.csv       one row per manager per player for the chosen gameweek
    ownership.csv   matrix of which manager owns which player (the useful one)
"""

import argparse
import csv
import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE = "https://fantasy.premierleague.com/api"
UA = "Mozilla/5.0 (compatible; fpl-export/1.0)"
THROTTLE = 0.4  # seconds between calls, be polite to the API


def get(path, retries=3):
    """GET a JSON endpoint with a couple of retries."""
    url = f"{BASE}/{path.lstrip('/')}"
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    for attempt in range(1, retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                raise
            if attempt == retries:
                raise
            time.sleep(2 * attempt)
        except urllib.error.URLError:
            if attempt == retries:
                raise
            time.sleep(2 * attempt)
    return None


def write_csv(path, rows, fieldnames):
    if not rows:
        print(f"  skipped {os.path.basename(path)} (no rows)")
        return
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    print(f"  wrote {os.path.basename(path)} ({len(rows)} rows)")


def fetch_standings(league_id):
    """Walk every page of a classic league's standings."""
    results, page = [], 1
    while True:
        data = get(f"leagues-classic/{league_id}/standings/?page_standings={page}")
        block = data["standings"]
        results.extend(block["results"])
        if not block.get("has_next"):
            break
        page += 1
        time.sleep(THROTTLE)
    return data["league"]["name"], results


def main():
    ap = argparse.ArgumentParser(description="Export FPL data to CSV.")
    ap.add_argument("--league", type=int, required=True, help="classic league ID")
    ap.add_argument("--gw", type=int, required=True, help="gameweek number")
    ap.add_argument("--outdir", default="fpl_data", help="output directory")
    args = ap.parse_args()

    os.makedirs(args.outdir, exist_ok=True)
    out = lambda name: os.path.join(args.outdir, name)

    print("Fetching bootstrap-static ...")
    boot = get("bootstrap-static/")
    teams = {t["id"]: t["short_name"] for t in boot["teams"]}
    positions = {p["id"]: p["singular_name_short"] for p in boot["element_types"]}

    players = {}
    player_rows = []
    for p in boot["elements"]:
        name = f"{p['first_name']} {p['second_name']}".strip()
        players[p["id"]] = {
            "web_name": p["web_name"],
            "full_name": name,
            "team": teams.get(p["team"], "?"),
            "pos": positions.get(p["element_type"], "?"),
        }
        player_rows.append({
            "id": p["id"],
            "web_name": p["web_name"],
            "full_name": name,
            "team": teams.get(p["team"], "?"),
            "pos": positions.get(p["element_type"], "?"),
            "price": p["now_cost"] / 10.0,
            "selected_by_pct": p["selected_by_percent"],
            "total_points": p["total_points"],
            "form": p["form"],
            "points_per_game": p["points_per_game"],
            "minutes": p["minutes"],
            "goals": p["goals_scored"],
            "assists": p["assists"],
            "clean_sheets": p["clean_sheets"],
            "bonus": p["bonus"],
            "expected_goals": p.get("expected_goals"),
            "expected_assists": p.get("expected_assists"),
            "status": p["status"],           # a=available, d=doubtful, i=injured
            "chance_next_round": p["chance_of_playing_next_round"],
            "news": p["news"],
        })

    write_csv(out("players.csv"), player_rows, list(player_rows[0].keys()))

    print("Fetching fixtures ...")
    time.sleep(THROTTLE)
    fixture_rows = []
    for f in get("fixtures/"):
        fixture_rows.append({
            "gw": f["event"],
            "kickoff": f["kickoff_time"],
            "home": teams.get(f["team_h"], "?"),
            "away": teams.get(f["team_a"], "?"),
            "home_fdr": f["team_h_difficulty"],
            "away_fdr": f["team_a_difficulty"],
            "home_score": f["team_h_score"],
            "away_score": f["team_a_score"],
            "finished": f["finished"],
        })
    write_csv(out("fixtures.csv"), fixture_rows,
              list(fixture_rows[0].keys()) if fixture_rows else [])

    print(f"Fetching league {args.league} ...")
    time.sleep(THROTTLE)
    league_name, standings = fetch_standings(args.league)
    print(f"  league: {league_name} ({len(standings)} managers)")

    standing_rows = [{
        "rank": s["rank"],
        "last_rank": s["last_rank"],
        "entry_id": s["entry"],
        "team_name": s["entry_name"],
        "manager": s["player_name"],
        "gw_points": s["event_total"],
        "total_points": s["total"],
    } for s in standings]
    write_csv(out("standings.csv"), standing_rows, list(standing_rows[0].keys()))

    print(f"Fetching live points for GW{args.gw} ...")
    time.sleep(THROTTLE)
    live = {e["id"]: e["stats"] for e in get(f"event/{args.gw}/live/")["elements"]}

    print(f"Fetching picks for GW{args.gw} ...")
    pick_rows = []
    owners = {}   # player_id -> list of team names
    for s in standings:
        time.sleep(THROTTLE)
        try:
            data = get(f"entry/{s['entry']}/event/{args.gw}/picks/")
        except urllib.error.HTTPError as exc:
            print(f"  skipped {s['entry_name']}: HTTP {exc.code}")
            continue

        for pick in data["picks"]:
            pid = pick["element"]
            meta = players.get(pid, {})
            stats = live.get(pid, {})
            pick_rows.append({
                "entry_id": s["entry"],
                "team_name": s["entry_name"],
                "manager": s["player_name"],
                "player": meta.get("web_name", pid),
                "club": meta.get("team", "?"),
                "pos": meta.get("pos", "?"),
                "slot": pick["position"],
                "starting": pick["position"] <= 11,
                "is_captain": pick["is_captain"],
                "is_vice": pick["is_vice_captain"],
                "multiplier": pick["multiplier"],
                "gw_points": stats.get("total_points", 0),
                "minutes": stats.get("minutes", 0),
            })
            owners.setdefault(pid, []).append(s["entry_name"])

        print(f"  {s['entry_name']}: {len(data['picks'])} picks")

    if pick_rows:
        write_csv(out("picks.csv"), pick_rows, list(pick_rows[0].keys()))

    # Ownership matrix: one row per player owned by anyone in the league,
    # one column per manager. This is the file worth actually looking at.
    team_names = [s["entry_name"] for s in standings]
    own_rows = []
    for pid, holders in owners.items():
        meta = players.get(pid, {})
        row = {
            "player": meta.get("web_name", pid),
            "club": meta.get("team", "?"),
            "pos": meta.get("pos", "?"),
            "owned_by_count": len(holders),
            "owned_by_pct": round(100.0 * len(holders) / max(len(team_names), 1), 1),
        }
        for tn in team_names:
            row[tn] = 1 if tn in holders else 0
        own_rows.append(row)

    own_rows.sort(key=lambda r: (-r["owned_by_count"], r["player"]))
    write_csv(out("ownership.csv"), own_rows,
              ["player", "club", "pos", "owned_by_count", "owned_by_pct"] + team_names)

    print(f"\nDone. Files in: {os.path.abspath(args.outdir)}")


if __name__ == "__main__":
    try:
        main()
    except urllib.error.HTTPError as exc:
        sys.exit(f"HTTP error {exc.code}: {exc.reason}")
    except KeyboardInterrupt:
        sys.exit("\nInterrupted.")
