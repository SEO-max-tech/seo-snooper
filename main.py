"""Weekly competitor content-gap scan. Deterministic orchestration only —
see CLAUDE.md for the pipeline contract and failure policy.

Usage:
    python main.py                 # full run
    python main.py --competitor elevenlabs   # single competitor (debug)
    python main.py --dry-run       # everything except Chat send + alert rows
"""
from __future__ import annotations

PIPELINE = [
    "refresh_inventory",   # scripts.refresh_inventory.refresh()
    "scan_competitors",    # per-competitor: sitemaps -> diff -> extract
                           #   -> gap -> judge -> collect items
    "enrich_keywords",     # ahrefs provider, batched across competitors
    "notify",              # build_card + send + insert cm_alerts
    "record_run",          # cm_runs row with stats + errors
]


def run(args) -> int:
    """Per-competitor failures are caught, logged to run errors, and do not
    stop the loop. Hard-fail (exit 1) only if Supabase is unreachable after
    retries or every competitor failed."""
    raise NotImplementedError  # TODO(M8)


if __name__ == "__main__":
    raise SystemExit(run(None))  # TODO(M8) argparse
