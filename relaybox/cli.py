"""Local CLI for serving, inspecting and demonstrating the inbox."""

import argparse
import json
import os
from pathlib import Path
import sqlite3
import sys
import time

from .auth import sign
from .backup import backup_database, verify_backup
from .demo import seed_demo
from .dispatch import run_dispatcher
from .outbound import DEMO_SECRET as OUTBOUND_DEMO_SECRET, Outbox, RelayHandler
from .server import make_server
from .store import STATES, Conflict, Inbox, InvalidTransition, ValidationError, parse_payload, validate_delivery_id
from .worker import ReceiptHandler, run_worker

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
    result = argparse.ArgumentParser(prog="relaybox", description="Local durable signed webhook inbox with Standard Webhooks outbound delivery.")
    result.add_argument("--db", type=Path, default=Path("inbox.sqlite3"), help="SQLite database path (place before the subcommand)")
    commands = result.add_subparsers(dest="command", required=True)
    commands.add_parser("init", help="initialize the local database")
    commands.add_parser("demo", help="populate an empty database with four synthetic delivery states")
    commands.add_parser("stats", help="print counts for every state")
    for name, argument, help_text in [
        ("backup", "destination", "publish a consistent snapshot of --db to a new path"),
        ("verify-backup", "backup", "read and verify a standalone trusted snapshot"),
        ("restore", "backup", "restore a snapshot into a nonexistent --db path"),
    ]:
        command = commands.add_parser(name, help=help_text)
        command.add_argument(argument, type=Path)
        command.add_argument("--timeout", type=float, default=10, help="shared deadline in seconds (0.1–60, default 10)")
    listing = commands.add_parser("list", help="list latest job summaries")
    listing.add_argument("--state", choices=["queued", "leased", "succeeded", "dead_letter"])
    listing.add_argument("--limit", type=int, default=100)
    show = commands.add_parser("show", help="inspect one delivery")
    show.add_argument("job_id", type=int)
    claim = commands.add_parser("claim", help="claim one ready delivery; output includes the private fencing token")
    claim.add_argument("--worker", required=True)
    claim.add_argument("--lease-seconds", type=float, default=30)
    for name in ["ack", "fail", "replay", "renew"]:
        command = commands.add_parser(name, help={"ack": "acknowledge a current lease", "fail": "fail a current lease with bounded retry", "replay": "explicitly reset a dead letter's attempt budget", "renew": "extend a current unexpired lease"}[name])
        command.add_argument("job_id", type=int)
        if name != "replay":
            command.add_argument("--token", required=True)
        if name == "fail":
            command.add_argument("--error", required=True)
        if name == "renew":
            command.add_argument("--lease-seconds", type=float, default=30)
    work = commands.add_parser("work", help="process bounded ready work into local synthetic JSON receipts")
    work.add_argument("--receipts", type=Path, required=True)
    work.add_argument("--worker", default="local-worker")
    work.add_argument("--max-jobs", type=int, default=1)
    work.add_argument("--lease-seconds", type=float, default=30)
    serve = commands.add_parser("serve", help="serve dashboard and webhook endpoint on IPv4 loopback")
    serve.add_argument("--port", type=int, default=8787)
    serve.add_argument("--demo-secret", action="store_true", help="use the public synthetic demo key; no real data")
    serve.add_argument("--allow-replay", action="store_true", help="let the dashboard replay dead-letter outbound deliveries")
    subscription = commands.add_parser("subscription", help="manage outbound subscriptions")
    actions = subscription.add_subparsers(dest="action", required=True)
    add = actions.add_parser("add", help="subscribe an endpoint URL; prints its new signing secret once")
    add.add_argument("url")
    add.add_argument("--event-type", action="append", required=True, help="repeatable; '*' matches every type")
    add_secret = add.add_mutually_exclusive_group()
    add_secret.add_argument("--secret", help="use this whsec_ secret instead of generating one")
    add_secret.add_argument("--demo-secret", action="store_true", help="use the public synthetic outbound demo key")
    add.add_argument("--allow-remote", action="store_true", help="accept a non-loopback https:// endpoint")
    actions.add_parser("list", help="list subscriptions and secret fingerprints, never secret values")
    rotate = actions.add_parser("rotate-secret", help="add a new current secret; older ones sign until the grace period ends")
    rotate.add_argument("subscription_id", type=int)
    rotate.add_argument("--grace-seconds", type=float, default=86400)
    rotate.add_argument("--secret", help="use this whsec_ secret instead of generating one")
    for name in ["enable", "disable"]:
        actions.add_parser(name, help=f"{name} a subscription").add_argument("subscription_id", type=int)
    publish = commands.add_parser("publish", help="store a JSON message and queue one delivery per matching subscription")
    publish.add_argument("event_type")
    publish.add_argument("payload", type=Path)
    publish.add_argument("--webhook-id", help="idempotency key sent as webhook-id (default: random msg_ ID)")
    relay = commands.add_parser("relay", help="fan ready inbound jobs out to subscriptions by their payload 'type'")
    relay.add_argument("--worker", default="relay-worker")
    relay.add_argument("--max-jobs", type=int, default=1)
    relay.add_argument("--lease-seconds", type=float, default=30)
    deliver = commands.add_parser("deliver", help="attempt bounded ready outbound deliveries")
    deliver.add_argument("--worker", default="local-dispatcher")
    deliver.add_argument("--max-deliveries", type=int, default=1)
    deliver.add_argument("--lease-seconds", type=float, default=60)
    deliver.add_argument("--timeout", type=float, default=15, help="per-attempt deadline in seconds (0.01–60)")
    deliver.add_argument("--wait", type=float, default=0, help="keep claiming for up to this many seconds while retries are pending")
    deliver.add_argument("--allow-remote", action="store_true", help="allow non-loopback https:// endpoints")
    deliveries = commands.add_parser("deliveries", help="list latest outbound delivery summaries and counts")
    deliveries.add_argument("--state", choices=list(STATES))
    deliveries.add_argument("--limit", type=int, default=100)
    commands.add_parser("replay-delivery", help="requeue a dead-letter outbound delivery with the same webhook-id").add_argument("delivery_id", type=int)
    signing = commands.add_parser("sign", help="print signature headers for an exact UTF-8 JSON file")
    signing.add_argument("delivery_id")
    signing.add_argument("payload", type=Path)
    signing.add_argument("--timestamp", type=int)
    signing.add_argument("--demo-secret", action="store_true")
    return result


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    try:
        # Recovery must never initialize Inbox or create source/destination parents.
        if args.command == "backup":
            _print(backup_database(args.db, args.destination, timeout=args.timeout))
            return 0
        if args.command == "verify-backup":
            _print(verify_backup(args.backup, timeout=args.timeout))
            return 0
        if args.command == "restore":
            _print(backup_database(args.backup, args.db, timeout=args.timeout))
            return 0
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
        if args.command in {"subscription", "publish", "relay", "deliver", "deliveries", "replay-delivery"}:
            return _outbound(args, Outbox(args.db))
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
        elif args.command == "renew":
            job = inbox.renew(args.job_id, args.token, lease_seconds=args.lease_seconds)
            job.pop("lease_token", None)
            _print({"job": job})
        elif args.command == "work":
            counts = run_worker(inbox, ReceiptHandler(args.receipts), worker=args.worker,
                                max_jobs=args.max_jobs, lease_seconds=args.lease_seconds)
            _print(counts)
            return 1 if counts["failed"] or counts["lost_leases"] else 0
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
            server = make_server(inbox, secret, port=args.port, allow_replay=args.allow_replay)
            print(f"RelayBox listening at http://127.0.0.1:{args.port}", flush=True)
            if args.demo_secret:
                print("Synthetic demo key enabled. Use synthetic data only.", flush=True)
            if args.allow_replay:
                print("Dashboard replay of dead-letter deliveries is enabled.", flush=True)
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                pass
            finally:
                server.server_close()
        return 0
    except (ValidationError, Conflict, InvalidTransition, KeyError, OSError, sqlite3.Error) as exc:
        missing = {"subscription": "subscription not found", "replay-delivery": "delivery not found"}.get(args.command, "job not found")
        message = missing if isinstance(exc, KeyError) else str(exc)
        # JSON escapes terminal control characters and preserves machine-readable errors.
        print(json.dumps({"error": message}, ensure_ascii=True), file=sys.stderr)
        return 2


def _outbound(args, outbox):
    """Outbound commands. Secrets print only when created; tokens never print."""
    if args.command == "subscription":
        if args.action == "add":
            secret = OUTBOUND_DEMO_SECRET if args.demo_secret else args.secret
            subscription, secret = outbox.subscribe(args.url, args.event_type, secret=secret, allow_remote=args.allow_remote)
            _print({"subscription": subscription, "secret": secret,
                    "notice": "Give this secret to the receiver now; RelayBox lists only its fingerprint."})
        elif args.action == "list":
            _print({"subscriptions": outbox.subscriptions()})
        elif args.action == "rotate-secret":
            subscription, secret = outbox.rotate_secret(args.subscription_id, secret=args.secret, grace_seconds=args.grace_seconds)
            _print({"subscription": subscription, "secret": secret})
        else:
            _print({"subscription": outbox.set_subscription_state(args.subscription_id, active=args.action == "enable")})
    elif args.command == "publish":
        message, created = outbox.publish(args.event_type, args.payload.read_bytes(), webhook_id=args.webhook_id)
        _print({"created": created, "message": message})
    elif args.command == "relay":
        counts = run_worker(Inbox(args.db), RelayHandler(outbox), worker=args.worker,
                            max_jobs=args.max_jobs, lease_seconds=args.lease_seconds)
        _print(counts)
        return 1 if counts["failed"] or counts["lost_leases"] else 0
    elif args.command == "deliver":
        counts = run_dispatcher(outbox, worker=args.worker, max_deliveries=args.max_deliveries,
                                lease_seconds=args.lease_seconds, timeout=args.timeout,
                                wait=args.wait, allow_remote=args.allow_remote)
        _print(counts)
        return 1 if counts["awaiting_retry"] or counts["dead_lettered"] or counts["lost_leases"] else 0
    elif args.command == "deliveries":
        _print(outbox.list_deliveries(state=args.state, limit=args.limit))
    else:
        _print({"delivery": outbox.replay(args.delivery_id)})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
