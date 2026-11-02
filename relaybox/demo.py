"""Synthetic, repeatable state-machine walkthrough. No network calls."""

import json
import time

from .outbound import DEMO_SECRET, Outbox, Outcome
from .store import Inbox, ValidationError

# The demo receiver in examples/receive_webhooks.py listens here by default.
DEMO_ENDPOINT = "http://127.0.0.1:8788/demo"


def seed_demo(path) -> dict:
    """Seed an empty database with one delivery in each state; never erase data."""
    tick = [time.time() - 10]
    inbox = Inbox(path, clock=lambda: tick[0])
    outbox_tick = [time.time() - 10]
    outbox = Outbox(path, clock=lambda: outbox_tick[0], max_attempts=3, base_delay=2, max_delay=60)
    if sum(inbox.stats().values()) or sum(outbox.stats().values()) or outbox.subscriptions():
        raise ValidationError("demo requires an empty database; choose a new --db path")

    def ingest(index, description):
        payload = {"synthetic": True, "type": "demo.delivery", "sequence": index,
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

    # Outbound: one subscription and one delivery per state. Outcomes are
    # recorded directly, so no request is sent while seeding.
    outbox.subscribe(DEMO_ENDPOINT, ["demo.delivery"], secret=DEMO_SECRET)

    def publish(index, description):
        body = json.dumps({"synthetic": True, "type": "demo.delivery", "sequence": index,
                           "message": description}).encode()
        outbox.publish("demo.delivery", body, webhook_id=f"msg_demo_{index:03}")
        return outbox.claim("demo-dispatcher", lease_seconds=3600 if index == 2 else 60)

    claim = publish(1, "Delivered and acknowledged with 204")
    outbox.complete(claim["id"], claim["lease_token"], Outcome("success", 204))
    publish(2, "A dispatcher holds an expiring lease")
    claim = publish(3, "Three synthetic 503 answers; ready for replay")
    for index in range(3):
        outbox.complete(claim["id"], claim["lease_token"], Outcome(
            "retry", 503, f"Synthetic HTTP 503 on attempt {index + 1}; no network call was made"))
        if index < 2:
            outbox_tick[0] += 2 ** (index + 1)  # The full-jitter ceiling, so the retry is ready.
            claim = outbox.claim("demo-dispatcher")
    outbox.publish("demo.delivery", json.dumps({"synthetic": True, "type": "demo.delivery", "sequence": 4,
                                                "message": "Waiting for its first attempt"}).encode(),
                   webhook_id="msg_demo_004")
    return {"synthetic": True, "counts": inbox.stats(), "outbound_counts": outbox.stats(), "database": str(path)}
