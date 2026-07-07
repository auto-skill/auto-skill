"""Mark stale or duplicate running scrape_runs rows.

This is a host-side operations helper for alpha deploys. It does not stop
processes; it only fixes bookkeeping rows after you have stopped extra workers.

Dry run:
    python cleanup_scrape_runs.py

Apply:
    python cleanup_scrape_runs.py --apply
"""

from __future__ import annotations

import argparse
import sqlite3
from datetime import datetime, timedelta, timezone

from local_store import DB_PATH, init_db


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Clean stale scrape_runs rows.")
    parser.add_argument("--apply", action="store_true", help="write changes; otherwise print a dry run")
    parser.add_argument(
        "--max-age-seconds",
        type=int,
        default=7200,
        help="mark running rows older than this as stale; default: 7200",
    )
    parser.add_argument(
        "--keep-newest-running",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="mark all but the newest running row stale; default: true",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    init_db()
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(seconds=args.max_age_seconds)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT id, started_at, status FROM scrape_runs "
            "WHERE status='running' ORDER BY started_at DESC"
        ).fetchall()
        stale_ids: list[tuple[str, str]] = []
        for index, row in enumerate(rows):
            started = _parse_time(row["started_at"])
            reasons: list[str] = []
            if started and started < cutoff:
                reasons.append(f"older than {args.max_age_seconds}s")
            if args.keep_newest_running and index > 0:
                reasons.append("duplicate running row")
            if reasons:
                stale_ids.append((row["id"], ", ".join(reasons)))

        print(f"DB: {DB_PATH}")
        print(f"running rows: {len(rows)}")
        print(f"rows to mark stale: {len(stale_ids)}")
        for run_id, reason in stale_ids:
            print(f"  {run_id}: {reason}")

        if not args.apply:
            print("dry run only; pass --apply to update rows")
            return 0

        for run_id, reason in stale_ids:
            conn.execute(
                "UPDATE scrape_runs SET status=?, finished_at=?, error=? WHERE id=?",
                (
                    "stale",
                    now.isoformat(),
                    f"Marked stale by cleanup_scrape_runs.py: {reason}.",
                    run_id,
                ),
            )
        conn.commit()
        print("updated rows")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
