"""Synthetic, repeatable state-machine walkthrough. No network calls."""

import json
import time

from .store import Inbox, ValidationError


def seed_demo(path) -> dict:
    """Seed an empty database with one delivery in each state; never erase data."""
    tick = [time.time() - 10]
    inbox = Inbox(path, clock=lambda: tick[0])
    if sum(inbox.stats().values()):
        raise ValidationError("demo requires an empty database; choose a new --db path")

    def ingest(index, description):
        payload = {"synthetic": True, "event": "demo.delivery", "sequence": index,
                   "message": description, "note_zh": "仅供本地演示，不含真实用户数据。"}
        return inbox.ingest(f"demo-{index:03}", json.dumps(payload, ensure_ascii=False).encode())[0]

    succeeded = ingest(1, "An acknowledged delivery")
    claim = inbox.claim("demo-worker")
    inbox.ack(succeeded["id"], claim["lease_token"])

    ingest(2, "A worker holds an expiring lease")
    inbox.claim("demo-worker", lease_seconds=3600)

    failed = ingest(3, "Three synthetic failures; ready for explicit replay")
    for index in range(3):
        claim = inbox.claim("demo-worker")
        inbox.fail(failed["id"], claim["lease_token"], f"Synthetic timeout {index + 1}; no network call was made")
        if index < 2:
            tick[0] += 2 ** (index + 1)

    ingest(4, "Waiting for its first worker")
    return {"synthetic": True, "counts": inbox.stats(), "database": str(path)}
