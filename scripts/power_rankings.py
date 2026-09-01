"""
power_rankings.py — compute Composite Power Score and Matchup of the Week
for the Fantrax league.

Formula (per config.json weights):
  PowerScore = w_win * W%
             + w_pf  * (PFpg / league_avg_PFpg)
             + w_sos * SoS_factor
             + w_ap  * AllPlayW%
         multiplied by te_premium_factor (TE premium 1.5/rec in this league)

Usage: python power_rankings.py [--in data.json] [--out rankings.json]
"""
import argparse
import json
import urllib.request
from collections import defaultdict
from pathlib import Path


def load_generic_names(repo_root: Path) -> dict[str, str]:
    """
    Load commissioner-provided display-name overrides from league_context.json.
    Returns {team_name_or_id: display_name}. Fantrax team names are visible
    on the page; we don't override them with handles here.
    """
    ctx_path = repo_root / "league_context.json"
    if not ctx_path.exists():
        return {}
    try:
        ctx = json.loads(ctx_path.read_text())
        members = ctx.get("members") or {}
        # Map Fantrax team_name -> handle (used as the byline for the family 4)
        return {
            (info.get("team_name") or handle): handle
            for handle, info in members.items()
            if info.get("team_name")
        }
    except Exception:
        return {}


def build_user_map(users, generic_names: dict[str, str] | None = None):
    """
    Returns dict[team_id] -> {display_name, team_name, handle}.

    For Fantrax, "display_name" is the team_name (which is what readers see),
    and "handle" is the Fantrax username when we have it from league_context.
    """
    generic_names = generic_names or {}
    out = {}
    for u in users:
        tid = u.get("team_id") or u.get("user_id")
        team_name = u.get("team_name") or "Unknown"
        handle = generic_names.get(team_name)  # may be None for non-family
        out[tid] = {
            "display_name": team_name,   # what shows in the Tribune
            "team_name":    "",          # we already use display_name as the team label
            "handle":       handle,
        }
    return out


def current_week(bundle: dict) -> int:
    """
    Best-effort current week. Fantrax doesn't expose a /state/nfl equivalent
    we can hit cheaply; we use the first scoringPeriod that's not "off" if
    available, otherwise default to 1.
    """
    raw = bundle.get("_raw_league_info") or {}
    periods = raw.get("scoringPeriods") or []
    for p in periods:
        # Some scoringPeriods are dicts with status info, others are ints
        if isinstance(p, dict):
            status = (p.get("status") or "").lower()
            if status not in ("off", "final", "completed", ""):
                return p.get("period", 1)
        elif isinstance(p, int) and p == 1:
            return 1
    return 1


def standings_to_enriched(standings: list, user_map: dict, roster_keys: list) -> list[dict]:
    """
    Build the per-team enriched dict from Fantrax standings + roster keys.
    """
    by_id = {s.get("teamId"): s for s in standings if isinstance(s, dict)}
    enriched = []
    for tid in roster_keys:
        s = by_id.get(tid) or {}
        wins   = 0
        losses = 0
        ties   = 0
        # Fantrax encodes W-L-T as a string like "0-0-0" in standings["points"]
        wlt = (s.get("points") or "0-0-0").split("-")
        try:
            wins, losses, ties = int(wlt[0]), int(wlt[1]), int(wlt[2]) if len(wlt) > 2 else 0
        except (ValueError, IndexError):
            pass
        games   = wins + losses + ties
        pf      = float(s.get("totalPointsFor") or 0)
        enriched.append({
            "team_id":      tid,
            "owner":        user_map.get(tid, {}),
            "wins":         wins,
            "losses":       losses,
            "ties":         ties,
            "games":        games,
            "points_for":   pf,
            "points_against": 0.0,  # not exposed in public standings yet
            "win_pct":      (wins + 0.5 * ties) / games if games else 0.0,
            "pf_per_game":  pf / games if games else 0.0,
        })
    return enriched


def te_premium_factor(rosters_flat: dict, team_id: str, players_index: dict) -> float:
    """
    Weight a team's TE holdings — in this league TEs get 1.5/rec vs 1.0 PPR
    for everyone else. A roster with multiple elite TEs is structurally stronger.

    Returns a small multiplier (~0.95-1.05) intended to nudge the composite
    without dominating the other factors.
    """
    if team_id not in rosters_flat:
        return 1.0
    te_count = sum(
        1 for item in rosters_flat[team_id]
        if item.get("position") == "TE"
        and item.get("status") == "ACTIVE"
    )
    # 0 TEs = 0.97, 1 = 1.0, 2+ = 1.03 (slight premium signal)
    return {0: 0.97, 1: 1.0, 2: 1.03}.get(min(te_count, 2), 1.03)


def pick_key_players(roster: list, players_index: dict, n: int = 2) -> list[dict]:
    """
    Choose up to N ACTIVE players from a Fantrax roster. Order priority:
    QB first, then RB, then WR/TE. Resolved via players_index.
    """
    pos_priority = {"QB": 0, "RB": 1, "WR": 2, "TE": 3, "RWT": 4, "K": 5, "DST": 6}
    active = [it for it in (roster or []) if it.get("status") == "ACTIVE"]
    active.sort(key=lambda it: pos_priority.get(it.get("position", "?"), 99))
    out = []
    for item in active[:n]:
        pid = item.get("player_id")
        info = players_index.get(pid) or item.get("player") or {}
        out.append({
            "name":     info.get("name") or pid,
            "position": item.get("position"),
            "team":     info.get("team") or "FA",
        })
    return out


def compute_rankings(bundle: dict, weights: dict,
                     generic_names: dict[str, str] | None = None) -> dict:
    users   = bundle.get("users") or []
    rosters_flat = bundle.get("rosters") or {}
    standings = bundle.get("standings") or []
    players_index = bundle.get("players_index") or {}

    user_map = build_user_map(users, generic_names=generic_names)
    enriched = standings_to_enriched(standings, user_map, list(rosters_flat.keys()))

    week = current_week(bundle)

    played = [r for r in enriched if r["games"] > 0]

    # Preseason / week-1 path — season hasn't started. Return zeroed rankings
    # with a schedule preview for week 1 (period 1 matchups from league_info).
    if not played:
        raw = bundle.get("_raw_league_info") or {}
        period1 = next((p for p in (raw.get("matchups") or []) if p.get("period") == 1), None)
        preview_motw = None
        if period1 and enriched:
            # Build a lookup by team_id
            by_team = {r["team_id"]: r for r in enriched}
            pairs = period1.get("matchupList") or []
            # Pick the first matchup as the preview
            if pairs:
                p = pairs[0]
                a_id = p.get("away", {}).get("id")
                b_id = p.get("home", {}).get("id")
                a = by_team.get(a_id) if a_id else None
                b = by_team.get(b_id) if b_id else None
                if a and b:
                    preview_motw = {
                        "status": "preview",
                        "team_a": {
                            "name": a["owner"].get("display_name", "?"),
                            "team":  a["owner"].get("team_name", ""),
                            "rank":  0,
                            "score": 50.0,
                            "record": "0-0",
                            "key_players": pick_key_players(rosters_flat.get(a_id, []), players_index, n=2),
                        },
                        "team_b": {
                            "name": b["owner"].get("display_name", "?"),
                            "team":  b["owner"].get("team_name", ""),
                            "rank":  0,
                            "score": 50.0,
                            "record": "0-0",
                            "key_players": pick_key_players(rosters_flat.get(b_id, []), players_index, n=2),
                        },
                    }
        return {
            "league":      bundle.get("league", {}).get("name"),
            "week":        week,
            "season":      bundle.get("league", {}).get("season"),
            "season_type": "regular",
            "rankings": [
                {**r, "pf_per_game": 0.0, "sos_factor": 1.0,
                 "all_play_pct": 0.5, "raw_score": 0.0, "power_score": 50.0,
                 "te_factor": 1.0, "rank": i + 1}
                for i, r in enumerate(enriched)
            ],
            "matchup_of_week": preview_motw,
        }

    # In-season path — composite ranking
    pf_by_team = {r["team_id"]: r["pf_per_game"] for r in enriched}
    league_avg = sum(pf_by_team.values()) / max(len(pf_by_team), 1)

    for r in enriched:
        r["sos_factor"]   = 1.0  # SoS deferred until we have at least one week of matchup data
        r["all_play_pct"] = r["win_pct"]
        r["te_factor"]    = te_premium_factor(rosters_flat, r["team_id"], players_index)

    for r in enriched:
        pf_norm = (r["pf_per_game"] / league_avg) if league_avg else 1.0
        r["raw_score"] = (
            weights.get("win_pct", 0.40)            * r["win_pct"]
          + weights.get("pf_per_game", 0.25)        * pf_norm
          + weights.get("strength_of_schedule", 0.20) * r["sos_factor"]
          + weights.get("all_play_win_pct", 0.15)   * r["all_play_pct"]
        ) * r["te_factor"]

    ranked = sorted(enriched, key=lambda x: x["raw_score"], reverse=True)
    top = ranked[0]["raw_score"]
    bot = ranked[-1]["raw_score"]
    spread = top - bot if top != bot else 1.0
    for i, r in enumerate(ranked):
        r["power_score"] = 100 - ((top - r["raw_score"]) / spread) * 60
        r["rank"] = i + 1

    # Matchup of the Week — top-2 if they play each other this week; else
    # top-ranked team vs. highest-ranked available opponent
    raw = bundle.get("_raw_league_info") or {}
    week_matchups = next((p for p in (raw.get("matchups") or []) if p.get("period") == week), None)
    week_pairs = []
    if week_matchups:
        for m in week_matchups.get("matchupList", []):
            a = m.get("away", {}).get("id")
            b = m.get("home", {}).get("id")
            if a and b:
                week_pairs.append({a, b})

    motw = None
    motw_status = "preview"
    top_two = (ranked[0]["team_id"], ranked[1]["team_id"])
    for pair in week_pairs:
        if top_two[0] in pair and top_two[1] in pair:
            motw = (ranked[0], ranked[1])
            break
    if motw is None and ranked:
        for pair in week_pairs:
            if ranked[0]["team_id"] in pair:
                opp_id = next(t for t in pair if t != ranked[0]["team_id"])
                opp = next((x for x in ranked if x["team_id"] == opp_id), None)
                if opp:
                    motw = (ranked[0], opp)
                break

    def motw_team_side(team_row, tid):
        return {
            "name":   team_row["owner"].get("display_name", "?"),
            "team":   team_row["owner"].get("team_name", ""),
            "rank":   team_row["rank"],
            "score":  team_row["power_score"],
            "record": f"{team_row['wins']}-{team_row['losses']}" + (f"-{team_row['ties']}" if team_row["ties"] else ""),
            "key_players": pick_key_players(rosters_flat.get(tid, []), players_index, n=2),
        }

    return {
        "league":      bundle.get("league", {}).get("name"),
        "week":        week,
        "season":      bundle.get("league", {}).get("season"),
        "season_type": "regular",
        "rankings":    ranked,
        "matchup_of_week": {
            "status": motw_status,
            "team_a": motw_team_side(motw[0], motw[0]["team_id"]),
            "team_b": motw_team_side(motw[1], motw[1]["team_id"]),
        } if motw else None,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", default="data.json")
    ap.add_argument("--config", default="config.json")
    ap.add_argument("--out", default="rankings.json")
    args = ap.parse_args()

    bundle = json.loads(Path(args.inp).read_text())
    cfg    = json.loads(Path(args.config).read_text())
    generic_names = load_generic_names(Path(".").resolve())
    result = compute_rankings(bundle, cfg["weights"], generic_names=generic_names)

    Path(args.out).write_text(json.dumps(result, indent=2))
    print(f"[power_rankings] wrote {args.out}")
    print(f"  {len(result['rankings'])} teams ranked, week {result['week']} ({result['season_type']})")
    for r in result["rankings"]:
        owner = r["owner"].get("display_name", "?") if r["owner"] else "?"
        print(f"  #{r['rank']:>2}  {owner:<28}  Power {r['power_score']:5.1f}  "
              f"W% {r['win_pct']:.3f}  PFpg {r['pf_per_game']:>6.1f}  "
              f"TE×{r.get('te_factor',1.0):.2f}")
    if result["matchup_of_week"]:
        m = result["matchup_of_week"]
        print(f"  Matchup of the Week: #{m['team_a']['rank']} {m['team_a']['name']} "
              f"vs #{m['team_b']['rank']} {m['team_b']['name']}")


if __name__ == "__main__":
    main()
