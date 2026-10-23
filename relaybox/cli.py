"""Local CLI for serving, inspecting and demonstrating the inbox."""

import argparse
import json
import os
from pathlib import Path
import sqlite3
import sys
import time

from .auth import sign
from .demo import seed_demo
from .server import make_server
from .store import Conflict, Inbox, InvalidTransition, ValidationError, parse_payload, validate_delivery_id

DEMO_SECRET = b"relaybox-synthetic-demo-secret-only"


def _secret(args):
    if args.demo_secret:
        return DEMO_SECRET
    value = os.environ.get("RELAYBOX_SECRET", "").encode("utf-8")
    if len(value) < 16:
        raise ValidationError("set RELAYBOX_SECRET to at least 16 bytes, or use --demo-secret for synthetic local demos")
    return value


def _print(value):
    print(json.dumps(value, ensure_ascii=True, allow_nan=False, indent=2))


def parser():
    result = argparse.ArgumentParser(prog="relaybox", description="Local durable signed webhook inbox; no outbound delivery.")
    result.add_argument("--db", type=Path, default=Path("inbox.sqlite3"), help="SQLite database path (place before the subcommand)")
    commands = result.add_subparsers(dest="command", required=True)
    commands.add_parser("init", help="initialize the local database")
    commands.add_parser("demo", help="populate an empty database with four synthetic delivery states")
    commands.add_parser("stats", help="print counts for every state")
    listing = commands.add_parser("list", help="list latest job summaries")
    listing.add_argument("--state", choices=["queued", "leased", "succeeded", "dead_letter"])
    listing.add_argument("--limit", type=int, default=100)
    show = commands.add_parser("show", help="inspect one delivery")
    show.add_argument("job_id", type=int)
    claim = commands.add_parser("claim", help="claim one ready delivery; output includes the private fencing token")
    claim.add_argument("--worker", required=True)
    claim.add_argument("--lease-seconds", type=float, default=30)
    for name in ["ack", "fail", "replay"]:
        command = commands.add_parser(name, help={"ack": "acknowledge a current lease", "fail": "fail a current lease with bounded retry", "replay": "explicitly reset a dead letter's attempt budget"}[name])
        command.add_argument("job_id", type=int)
        if name != "replay":
            command.add_argument("--token", required=True)
        if name == "fail":
            command.add_argument("--error", required=True)
    serve = commands.add_parser("serve", help="serve dashboard and webhook endpoint on IPv4 loopback")
    serve.add_argument("--port", type=int, default=8787)
    serve.add_argument("--demo-secret", action="store_true", help="use the public synthetic demo key; no real data")
    signing = commands.add_parser("sign", help="print signature headers for an exact UTF-8 JSON file")
    signing.add_argument("delivery_id")
    signing.add_argument("payload", type=Path)
    signing.add_argument("--timestamp", type=int)
    signing.add_argument("--demo-secret", action="store_true")
    return result


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "sign":
            validate_delivery_id(args.delivery_id)
            body = args.payload.read_bytes()
            parse_payload(body)
            timestamp = int(time.time()) if args.timestamp is None else args.timestamp
            if not 0 <= timestamp <= 999_999_999_999:
                raise ValidationError("timestamp must be a nonnegative Unix second with at most 12 digits")
            _print({"Content-Type": "application/json", "X-RelayBox-ID": args.delivery_id,
                    "X-RelayBox-Timestamp": str(timestamp),
                    "X-RelayBox-Signature": sign(_secret(args), timestamp, args.delivery_id, body)})
            return 0
        if args.command == "serve":
            secret = _secret(args)
            if not 1 <= args.port <= 65535:
                raise ValidationError("port must be between 1 and 65535")
        args.db.parent.mkdir(parents=True, exist_ok=True)
        if args.command == "demo":
            _print(seed_demo(args.db))
            return 0
        inbox = Inbox(args.db)
        if args.command in {"init", "stats"}:
            _print({"database": str(args.db), "counts": inbox.stats()})
        elif args.command == "list":
            _print({"jobs": inbox.list_jobs(state=args.state, limit=args.limit)})
        elif args.command == "show":
            job = inbox.get(args.job_id)
            job.pop("lease_token", None)
            _print({"job": job})
        elif args.command == "claim":
            _print({"job": inbox.claim(args.worker, lease_seconds=args.lease_seconds)})
        elif args.command == "ack":
            inbox.ack(args.job_id, args.token)
            _print({"ok": True})
        elif args.command == "fail":
            inbox.fail(args.job_id, args.token, args.error)
            _print({"ok": True})
        elif args.command == "replay":
            job = inbox.replay(args.job_id)
            job.pop("lease_token", None)
            _print({"job": job})
        elif args.command == "serve":
            server = make_server(inbox, secret, port=args.port)
            print(f"RelayBox listening at http://127.0.0.1:{args.port}", flush=True)
            if args.demo_secret:
                print("Synthetic demo key enabled. Use synthetic data only.", flush=True)
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                pass
            finally:
                server.server_close()
        return 0
    except (ValidationError, Conflict, InvalidTransition, KeyError, OSError, sqlite3.Error) as exc:
        message = "job not found" if isinstance(exc, KeyError) else str(exc)
        # JSON escapes terminal control characters and preserves machine-readable errors.
        print(json.dumps({"error": message}, ensure_ascii=True), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
