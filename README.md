# FPL daily brief

Generates a Fantasy Premier League decision brief every day and commits it to
`briefs/`, so it exists whether or not any particular computer is switched on.

- `fpl_brief.py` - builds the daily decision brief
- `fpl_export.py` - dumps league data to CSV, run by hand when you want it
- `.github/workflows/fpl-brief.yml` - runs the brief daily on GitHub's runners

Everything uses the public FPL API. No credentials, no login, no secrets.

## Why GitHub Actions

The scripts fetch from `fantasy.premierleague.com`. Claude's cloud sandbox
blocks that host at its egress proxy, so a brief cannot be generated there.
GitHub's runners have unrestricted network access, so they do the fetching and
commit the result. Claude then reads the committed file, which needs nothing but
a git clone.

## Setup

1. Create a repo and push these files to it.
2. Settings -> Actions -> General -> Workflow permissions: set "Read and write
   permissions" so the workflow can commit the brief back.
3. Actions tab -> "FPL daily brief" -> "Run workflow" to test it once by hand.
4. Check that `briefs/brief-YYYY-MM-DD.md` appears.

## Schedule

The workflow runs at 14:45 UTC. GitHub cron is always UTC and does not follow
daylight saving, so that is 16:45 Copenhagen in summer and 15:45 in winter.

Two things worth knowing about GitHub's scheduler:

- Scheduled runs are queued, not guaranteed on the minute. Delays of 5-15
  minutes are normal at busy times, which is why the Claude task reads the brief
  20 minutes later rather than immediately.
- GitHub disables scheduled workflows after 60 days of no repository activity.
  The daily commit normally counts as activity, but if the briefs ever stop
  appearing, check the Actions tab first.

## Running by hand

    python3 fpl_brief.py --entry 8876628 --league 1840250 --out "briefs/brief-{date}.md"

`{date}` expands to today's date. Quote it, or your shell may try to glob it.

    python3 fpl_export.py --league 1840250 --gw 3 --outdir fpl_data
