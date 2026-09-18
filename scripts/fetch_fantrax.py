"""
fetch_fantrax.py — pull league data from the public Fantrax API.

Public leagues on Fantrax return full data without any auth.
The endpoints below all worked anonymously with our test league.

Usage: python fetch_fantrax.py [--out data.json]
Writes JSON bundle with league, users, rosters, matchups, standings,
scoring config, and a slim players index (resolved from Fantrax IDs).
"""
import argparse
import json
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

BASE = "https://www.fantrax.com/fxea/general"
PLAYERS_CACHE = Path("cache/players_nfl.json")
PLAYERS_CACHE_MAX_AGE_DAYS = 7


def _current_period(league_info: dict) -> int:
    """
    Derive the current NFL scoring period from league_info.scoringPeriods.

    Each period has {number, startDate, endDate}. The current period is the
    one whose date window contains today (UTC). If today falls before any
    period (preseason) we return 1; if after the last period (postseason),
    we return the last period number.
    """
    periods = league_info.get("scoringPeriods") or []
    if not periods:
        return 1
    now = datetime.now(timezone.utc)
    # Sort defensively in case the API returns them out of order
    periods = sorted(periods, key=lambda p: p.get("number", 0))
    for p in periods:
        try:
            start = datetime.fromisoformat(p["startDate"].replace("Z", "+00:00"))
            end   = datetime.fromisoformat(p["endDate"].replace("Z", "+00:00"))
        except (KeyError, ValueError):
            continue
        if start <= now <= end:
            return int(p["number"])
    # Today is before any period (preseason) or after the last one
    try:
        first_start = datetime.fromisoformat(periods[0]["startDate"].replace("Z", "+00:00"))
        if now < first_start:
            return int(periods[0]["number"])
    except (KeyError, ValueError):
        pass
    try:
        return int(periods[-1]["number"])
    except (KeyError, ValueError):
        return 1


def _get(path: str, retries: int = 3, backoff: float = 1.5):
    """
    GET a Fantrax API endpoint with retry on transient failure.
    Fantrax's CDN (Cloudflare) sometimes returns 524/timeout under load,
    and sometimes returns a 200 with an error envelope ({error: ...}).
    Both are retried; after exhausting retries, raise.
    """
    url = f"{BASE}{path}"
    last_err = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(
                url,
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                    "Accept": "application/json,*/*",
                    "Accept-Language": "en-US,en;q=0.9",
                    "Referer": "https://www.fantrax.com/",
                },
            )
            with urllib.request.urlopen(req, timeout=30) as r:
                payload = json.loads(r.read().decode("utf-8"))

            # Detect Fantrax error envelope: {"error": {"code": ..., "message": ...}}
            if isinstance(payload, dict) and "error" in payload and isinstance(payload["error"], dict):
                err = payload["error"]
                raise RuntimeError(f"Fantrax API error: {err.get('code','?')}: {err.get('message','?')}")

            return payload
        except Exception as e:
            last_err = e
            if attempt < retries - 1:
                wait = backoff ** attempt
                print(f"  retry {attempt+1}/{retries} after {wait:.1f}s ({e})", file=sys.stderr)
                time.sleep(wait)
    raise RuntimeError(f"Fantrax API failed after {retries} attempts: {last_err}")


def fetch_players_index(league_id: str, force: bool = False) -> dict:
    """
    Fantrax returns an opteligiblePos map inside getLeagueInfo — but
    player IDs are Fantrax-internal and we need a name lookup.
    The public getPlayerIds endpoint returns sport=NFL players with name data.
    Cache it locally for 7 days.
    """
    if not force and PLAYERS_CACHE.exists():
        age_days = (time.time() - PLAYERS_CACHE.stat().st_mtime) / 86400
        if age_days < PLAYERS_CACHE_MAX_AGE_DAYS:
            return json.loads(PLAYERS_CACHE.read_text())

    print(f"[fetch_fantrax] downloading NFL player index (~2-5MB)...", file=sys.stderr)
    data = _get("/getPlayerIds?sport=NFL")
    PLAYERS_CACHE.parent.mkdir(parents=True, exist_ok=True)
    PLAYERS_CACHE.write_text(json.dumps(data, indent=2))
    return data


def slim_player(player: dict) -> dict:
    """Keep only the fields we actually render."""
    name = player.get("name") or ""
    # Fantrax sometimes splits first/last, sometimes gives a single name field
    if not name and player.get("firstName"):
        name = f"{player.get('firstName','')} {player.get('lastName','')}".strip()
    return {
        "name": name or "Unknown",
        "position": player.get("position") or player.get("pos") or "?",
        "team": player.get("teamAbbr") or player.get("team") or "FA",
        "status": player.get("status") or "Active",
    }


def fetch_sleeper_projections(season: int = 2026, week: int = 1) -> dict:
    """
    Fetch Sleeper's weekly projections and return a tuple of two lookups
    so we can match Fantrax players (whose IDs don't map to Sleeper's)
    to projection data AND career metadata by name + NFL team.

    Returns (projection_lookup, meta_lookup) where:
    - projection_lookup: {(last, first|TEAM) or (first last|TEAM): pts_half_ppr}
    - meta_lookup:       {(last, first|TEAM): {"years_exp": int, "age": int, ...}}

    The endpoint is public: https://api.sleeper.app/v1/projections/nfl/regular/{season}/{week}
    """
    import urllib.request
    url = f"https://api.sleeper.app/v1/projections/nfl/regular/{season}/{week}"
    try:
        with urllib.request.urlopen(url, timeout=30) as r:
            data = json.loads(r.read())
    except Exception as e:
        print(f"[fetch_fantrax] WARN: sleeper projections fetch failed ({e})", file=sys.stderr)
        return {}, {}

    # Build a name+team -> half-PPR projection lookup
    # Sleeper projection keys are player IDs; the projection dict doesn't include
    # player names directly, so we additionally fetch the players DB.
    players_url = "https://api.sleeper.app/v1/players/nfl"
    try:
        with urllib.request.urlopen(players_url, timeout=60) as r:
            players_db = json.loads(r.read())
    except Exception as e:
        print(f"[fetch_fantrax] WARN: sleeper players DB fetch failed ({e})", file=sys.stderr)
        players_db = {}

    lookup = {}
    meta_lookup = {}
    for pid, proj in data.items():
        if not isinstance(proj, dict):
            continue
        pts = proj.get("pts_half_ppr")
        if not isinstance(pts, (int, float)):
            continue
        pinfo = players_db.get(pid) or {}
        first = (pinfo.get("first_name") or "").strip().lower()
        last = (pinfo.get("last_name") or "").strip().lower()
        team = (pinfo.get("team") or "").strip().upper()
        if not first or not last or not team or team in ("FA", ""):
            continue
        # Two key formats: "last, first|TEAM" (Fantrax "Higgins, Tee" → "higgins, tee")
        # and "first last|TEAM" — match either.
        key_csv = f"{last}, {first}|{team}"
        key_fs  = f"{first} {last}|{team}"
        lookup.setdefault(key_csv, pts)
        lookup.setdefault(key_fs, pts)
        # Capture career metadata for the LLM prompt guard. years_exp == 0
        # means rookie; years_exp >= 1 means experienced. Use the CSV key as
        # the canonical form so the lookup matches Fantrax's "Last, First".
        meta = {}
        if isinstance(pinfo.get("years_exp"), int):
            meta["years_exp"] = pinfo["years_exp"]
        if isinstance(pinfo.get("age"), (int, float)):
            meta["age"] = int(pinfo["age"])
        if meta:
            meta_lookup[key_csv] = meta
            meta_lookup[key_fs] = meta
    print(f"[fetch_fantrax] built projection lookup for {len(lookup)} player/team combos", file=sys.stderr)
    return lookup, meta_lookup


def normalize_name_for_lookup(name: str) -> str:
    """
    Fantrax stores names as "Last, First". Return lowercase "last, first"
    so we can match Sleeper's "first_name last_name" via the inverted key.
    """
    if not name:
        return ""
    name = name.strip().lower()
    return name  # already in "last, first" form


def projected_points_for_fantrax_team(team_roster: list, projection_lookup: dict) -> float:
    """
    Sum projected half-PPR points for the active roster slots of a Fantrax team.
    Fantrax exposes ALL roster items in getTeamRosters; for projection purposes
    we treat every ACTIVE starter-eligible position as contributing. Since
    period=1 returns the full roster (we don't have a starters-only flag yet),
    we compute total projections for the full roster as a rough estimate —
    the spread comparison is what matters, and both sides use the same heuristic.
    """
    return sum(
        (pts for pts in (
            lookup_player_projection(slot, projection_lookup)
            for slot in (team_roster or [])
            if is_active_starter(slot)
        ) if pts),
        0.0,
    )


def is_active_starter(slot: dict) -> bool:
    """True if a roster slot should count toward projections / key-players ranking."""
    status = slot.get("status")
    if status and status not in ("ACTIVE", "Active", None):
        return False
    player = slot.get("player") or {}
    if not player.get("name") or not player.get("team"):
        return False
    return True


def lookup_player_projection(slot: dict, projection_lookup: dict) -> float:
    """Return the projected half-PPR points for a single roster slot, or 0."""
    if not projection_lookup:
        return 0.0
    player = slot.get("player") or {}
    name = player.get("name") or ""
    team = (player.get("team") or "").upper()
    if not name or not team:
        return 0.0
    key = f"{normalize_name_for_lookup(name)}|{team}"
    pts = projection_lookup.get(key)
    return float(pts) if pts else 0.0


def per_starter_projections(team_roster: list, projection_lookup: dict) -> dict[str, float]:
    """
    Per-starter projected points for a Fantrax team. Returns
    {fantrax_player_id: projected_points} for every ACTIVE slot that has a
    matching projection. Used by power_rankings.pick_key_players to rank
    starters by projected points (instead of by position, which always
    surfaces QB + RB1).
    """
    out: dict[str, float] = {}
    if not projection_lookup:
        return out
    for slot in team_roster or []:
        if not is_active_starter(slot):
            continue
        pid = slot.get("player_id")
        if not pid:
            continue
        pts = lookup_player_projection(slot, projection_lookup)
        if pts > 0:
            out[pid] = round(pts, 2)
    return out


def fetch(league_id: str) -> dict:
    print(f"[fetch_fantrax] pulling getLeagueInfo...", file=sys.stderr)
    league_info = _get(f"/getLeagueInfo?leagueId={league_id}")

    # Pull rosters and standings. Period 1 is the safe choice for the
    # preseason snapshot; we'll switch to current week once the season starts.
    print(f"[fetch_fantrax] pulling rosters (period=1)...", file=sys.stderr)
    rosters_raw = _get(f"/getTeamRosters?leagueId={league_id}&period=1")

    print(f"[fetch_fantrax] pulling standings...", file=sys.stderr)
    standings = _get(f"/getStandings?leagueId={league_id}")

    # Projected points — pull Sleeper's weekly projections early so we can
    # use the meta_lookup to enrich players_index with career metadata
    # (years_exp, age) before the index is built.
    season_year = league_info.get("seasonYear") or 2026
    projection_lookup, meta_lookup = fetch_sleeper_projections(season=season_year, week=1)

    # Resolve player IDs → slim index, only for players actually on rosters.
    # Fantrax IDs are like "04mnz" for players, "20090" for DST/team-offense.
    # The players DB keys DSTs as "20090#1090" and team-offense as "20090#1060".
    # We try the bare ID first, then #<pos-suffix> fallbacks.
    players_db = fetch_players_index(league_id)
    DST_SUFFIX = "#1090"  # Defense/Special Teams position group

    def resolve(pid: str) -> dict | None:
        if pid in players_db:
            return slim_player(players_db[pid])
        # Try DST suffix for unmapped IDs (typical of DEF/team-offense slots)
        if pid and "#" not in pid:
            dst_key = pid + DST_SUFFIX
            if dst_key in players_db:
                return slim_player(players_db[dst_key])
        return None

    on_roster_ids = set()
    for team_id, team in rosters_raw.get("rosters", {}).items():
        for item in team.get("rosterItems", []):
            pid = item.get("id")
            if pid:
                on_roster_ids.add(pid)

    players_index = {}
    for pid in on_roster_ids:
        s = resolve(pid)
        if s:
            players_index[pid] = s

    # Enrich players_index with career metadata (years_exp, age) from Sleeper's
    # players DB. Match by (last, first|TEAM) since Fantrax IDs don't map to
    # Sleeper IDs. This lets the LLM prompt guard against player-fact
    # hallucinations (e.g. "Don't call a player a rookie unless years_exp==0").
    if meta_lookup:
        enriched = 0
        for pid, info in players_index.items():
            name = info.get("name") or ""
            team = (info.get("team") or "").upper()
            if not name or not team:
                continue
            # Fantrax stores names as "Last, First"; reverse to match lookup key
            if "," in name:
                parts = [p.strip().lower() for p in name.split(",", 1)]
                if len(parts) == 2:
                    key = f"{parts[0]}, {parts[1]}|{team}"
                else:
                    continue
            else:
                # Fallback: "First Last"
                parts = name.lower().split()
                if len(parts) >= 2:
                    key = f"{parts[0]} {parts[-1]}|{team}"
                else:
                    continue
            meta = meta_lookup.get(key)
            if meta:
                info["years_exp"] = meta.get("years_exp")
                info["age"] = meta.get("age")
                enriched += 1
        print(f"[fetch_fantrax] enriched {enriched}/{len(players_index)} players with career metadata", file=sys.stderr)

    # Flatten rosters into a {team_id: [player, ...]} shape for downstream use.
    # for downstream code reuse. Keep raw data under "rosters_raw" for now.
    rosters_flat = {}
    for team_id, team in rosters_raw.get("rosters", {}).items():
        rosters_flat[team_id] = [
            {
                "player_id": item.get("id"),
                "position": item.get("position"),
                "status": item.get("status"),
                **({"player": resolve(item.get("id"))} if resolve(item.get("id")) else {}),
            }
            for item in team.get("rosterItems", [])
        ]

    # Build a users list — Fantrax doesn't expose a separate users endpoint,
    # so we derive it from the matchups/standings.
    users = {}
    # Source 1: standings
    for entry in standings:
        tid = entry.get("teamId")
        if tid and tid not in users:
            users[tid] = {
                "user_id": tid,
                "team_id": tid,
                "team_name": entry.get("teamName"),
                "handle": None,  # unknown without users endpoint
            }
    # Source 2: matchups (gives all 12 even if 0-0-0 tie)
    for period_block in league_info.get("matchups", []):
        for matchup in period_block.get("matchupList", []):
            for side in ("away", "home"):
                team = matchup.get(side, {})
                tid = team.get("id")
                if tid and tid not in users:
                    users[tid] = {
                        "user_id": tid,
                        "team_id": tid,
                        "team_name": team.get("name"),
                        "handle": None,
                    }

    # Per-team projected points AND per-starter projection map (for the MOTW
    # key_players selector in power_rankings.py — sort starters by projected
    # points instead of position order, so the LLM highlights the actual
    # top scorers). projection_lookup was already built above for player
    # metadata enrichment.
    team_projected = {}
    team_starter_projections = {}  # tid -> {fantrax_player_id: projected_points}
    for tid, roster in rosters_flat.items():
        team_projected[tid] = projected_points_for_fantrax_team(roster, projection_lookup)
        team_starter_projections[tid] = per_starter_projections(roster, projection_lookup)

    # Attach projected_points + spread to every matchup in matchups[0]
    matchups_with_proj = []
    for period_block in league_info.get("matchups", []):
        new_block = dict(period_block)
        new_matchup_list = []
        for matchup in period_block.get("matchupList", []):
            away_id = matchup.get("away", {}).get("id")
            home_id = matchup.get("home", {}).get("id")
            away_proj = team_projected.get(away_id, 0.0)
            home_proj = team_projected.get(home_id, 0.0)
            spread = round(abs(away_proj - home_proj), 2)
            new_matchup = dict(matchup)
            new_matchup["away_projected_points"] = away_proj
            new_matchup["home_projected_points"] = home_proj
            new_matchup["matchup_projected_spread"] = spread
            # Attach per-side projected_points + signed spread (favorite = negative)
            for side, pid, proj in (("away", away_id, away_proj), ("home", home_id, home_proj)):
                if side == "away":
                    new_matchup["away_projected_spread"] = -spread if proj >= home_proj else spread
                else:
                    new_matchup["home_projected_spread"] = -spread if proj >  away_proj else spread
            new_matchup_list.append(new_matchup)
        new_block["matchupList"] = new_matchup_list
        matchups_with_proj.append(new_block)

    return {
        "league": {
            "name": league_info.get("leagueName"),
            "id": league_id,
            "season": league_info.get("seasonYear"),
            "start_date": league_info.get("startDate"),
            "end_date": league_info.get("endDate"),
            "format": (league_info.get("scoringSystem") or {}).get("type"),
        },
        "users": list(users.values()),
        "rosters": rosters_flat,
        "matchups": matchups_with_proj,
        "standings": standings,
        "scoring_system": league_info.get("scoringSystem"),
        "scoring_categories": (league_info.get("scoringSystem") or {}).get("scoringCategories"),
        "players_index": players_index,
        "week": _current_period(league_info),  # dynamic from scoringPeriods/today
        "projections_available": bool(projection_lookup),
        "scoring_format": "half_ppr_proxy",
        # Per-starter projections keyed by team_id -> {player_id: pts}. Used
        # by power_rankings.pick_key_players to surface actual top scorers
        # (not always QB+RB1 by position order).
        "team_starter_projections": team_starter_projections,
        # Raw league_info retained for reference; not used by downstream code yet
        "_raw_league_info": league_info,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--league-id", default="agjsij71msd7ufmw")
    ap.add_argument("--out", default="data.json")
    ap.add_argument("--config", default="config.json")
    args = ap.parse_args()

    config_path = Path(args.config)
    if config_path.exists():
        cfg = json.loads(config_path.read_text())
    else:
        cfg = {}

    league_id = cfg.get("league_id", args.league_id)

    try:
        bundle = fetch(league_id)
    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f"[fetch_fantrax] ERROR: {e}", file=sys.stderr)
        sys.exit(1)

    Path(args.out).write_text(json.dumps(bundle, indent=2))
    print(f"[fetch_fantrax] wrote {args.out}")
    print(f"  league: {bundle['league'].get('name')!r}")
    print(f"  season: {bundle['league'].get('season')}  format: {bundle['league'].get('format')}")
    print(f"  users: {len(bundle['users'])}  rosters: {len(bundle['rosters'])}  players: {len(bundle['players_index'])}")


if __name__ == "__main__":
    main()
