"""Repair accounts whose refreshed Access Tokens are immediately revoked."""

from __future__ import annotations

import argparse
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import db, token_refresh


def _write_status(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(
        json.dumps(state, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(temp, path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--proxy", default="")
    parser.add_argument(
        "--status-file",
        default="runtime/relogin_repair_status.json",
    )
    args = parser.parse_args()

    emails = db.list_registered_emails(
        filter_rt="token_invalid",
        limit=50000,
    )
    if args.limit > 0:
        emails = emails[:args.limit]
    status_path = Path(args.status_file)
    state = {
        "status": "running",
        "total": len(emails),
        "done": 0,
        "success": 0,
        "failed": 0,
        "started_at": time.time(),
        "finished_at": None,
        "results": [],
    }
    _write_status(status_path, state)

    def repair(email: str) -> tuple[str, dict]:
        return email, token_refresh.relogin_registered_account(
            email,
            args.proxy,
        )

    workers = max(1, min(3, args.workers))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for email, result in pool.map(repair, emails):
            ok = bool(result.get("ok"))
            state["done"] += 1
            state["success" if ok else "failed"] += 1
            state["results"].append({
                "email": email,
                "ok": ok,
                "status": result.get("status") or "",
                "error": result.get("error") or "",
                "finished_at": time.time(),
            })
            _write_status(status_path, state)
            print(
                f"[{state['done']}/{state['total']}] "
                f"{'OK' if ok else 'FAIL'} {email} "
                f"{result.get('error') or ''}",
                flush=True,
            )

    state["status"] = "done"
    state["finished_at"] = time.time()
    _write_status(status_path, state)
    print(
        f"finished: success={state['success']} failed={state['failed']}",
        flush=True,
    )
    return 0 if state["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
