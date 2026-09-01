"""
render.py — assemble the full page context and write index.html.

Usage: python render.py [--rankings rankings.json] [--commentary commentary.json] [--out index.html] [--archive editions/week-NN.html]
"""
import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape


def owner_label(team_name: str | None, display_name: str | None) -> str:
    return (team_name or "").strip() or (display_name or "Unknown")


def build_context(rankings: dict, commentary: dict | None, site_cfg: dict,
                   path_prefix: str = "") -> dict:
    wk = rankings.get("week")
    season = rankings.get("season")
    season_type = rankings.get("season_type", "regular")

    # Sensible defaults if commentary missing (template shouldn't blow up)
    lede          = (commentary or {}).get("lede")
    motw_blurb    = (commentary or {}).get("motw_blurb")
    pick          = (commentary or {}).get("pick")
    rankings_blurb= (commentary or {}).get("rankings_blurb")
    by_the_numbers = (commentary or {}).get("by_the_numbers") or [
        {"value": rankings["rankings"][0]["power_score"],     "label": "Top Power Score"},
        {"value": rankings["rankings"][-1]["power_score"],    "label": "Cellar Power Score"},
        {"value": len(rankings["rankings"]),                  "label": "Teams in the Hunt"},
        {"value": wk or "?",                                   "label": "Week Number"},
    ]
    closing = (commentary or {}).get("closing")

    return {
        "site_title":       site_cfg["site_title"],
        "tagline":          site_cfg.get("tagline", ""),
        "season":           season,
        "week":             wk,
        "season_type":      season_type,
        "rankings":         rankings["rankings"],
        "matchup_of_week":  rankings.get("matchup_of_week"),
        "lede":             lede,
        "motw_blurb":       motw_blurb,
        "pick":             pick,
        "rankings_blurb":   rankings_blurb,
        "by_the_numbers":   by_the_numbers,
        "closing":          closing,
        "path_prefix":      path_prefix,  # "" for index.html, "../" for editions/
        "generated_at":     datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
    }


def compute_issue_number(archive_dir: Path = Path("editions")) -> int:
    """
    Compute the next issue number by counting existing archive files.

    Counts editions/week-*.html files BEFORE writing the new archive,
    so today's run becomes Issue (count + 1). On the first run (no
    archives yet), returns 1.

    The archive counter is the source of truth — no separate state
    file. Deleting archives shifts the issue counter (acceptable,
    since the counter is just a masthead label).
    """
    existing = list(archive_dir.glob("week-*.html")) if archive_dir.exists() else []
    return len(existing) + 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rankings",   default="rankings.json")
    ap.add_argument("--commentary", default="commentary.json")
    ap.add_argument("--config",     default="config.json")
    ap.add_argument("--template",   default="templates/tribune.html.j2")
    ap.add_argument("--out",        default="index.html")
    ap.add_argument("--archive",    default=None,
                    help="If set, also write a copy to this path (e.g. editions/week-1.html)")
    ap.add_argument("--archive-dir", default="editions",
                    help="Directory containing past week-*.html files (for issue counter)")
    args = ap.parse_args()

    env = Environment(
        loader=FileSystemLoader(Path(args.template).parent),
        autoescape=select_autoescape(["html"]),
    )
    env.filters["capitalize"] = lambda s: (s or "").capitalize()
    template = env.get_template(Path(args.template).name)

    rankings   = json.loads(Path(args.rankings).read_text())
    site_cfg   = json.loads(Path(args.config).read_text())
    commentary_path = Path(args.commentary)
    commentary = json.loads(commentary_path.read_text()) if commentary_path.exists() else None

    issue = compute_issue_number(Path(args.archive_dir))
    print(f"[render] issue counter: No. {issue}")
    context = build_context(rankings, commentary, site_cfg)
    context["issue"] = issue
    html = template.render(**context)

    Path(args.out).write_text(html)
    print(f"[render] wrote {args.out} ({len(html)} bytes)")

    if args.archive:
        Path(args.archive).parent.mkdir(parents=True, exist_ok=True)
        # Re-render with path_prefix="../" so the archive's stylesheet
        # link and any other relative asset paths resolve correctly.
        # Carry the issue number forward so the masthead stays consistent.
        archive_context = build_context(rankings, commentary, site_cfg, path_prefix="../")
        archive_context["issue"] = issue
        archive_html = template.render(**archive_context)
        Path(args.archive).write_text(archive_html)
        print(f"[render] archived {args.archive}")


if __name__ == "__main__":
    main()