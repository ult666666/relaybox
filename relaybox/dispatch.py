"""Bounded outbound HTTP attempts and the lease-driven dispatcher loop.

Each attempt has a hard wall-clock deadline, reads at most a fixed number of
response-body bytes and never follows redirects. Classification follows the
Standard Webhooks "Delivery success and failure" guidance:
https://github.com/standard-webhooks/standard-webhooks/blob/main/spec/standard-webhooks.md
"""

from dataclasses import dataclass
from email.utils import parsedate_to_datetime
import http.client
import re
import socket
import ssl
import threading
import time

from . import __version__, standard_webhooks
from .outbound import Outcome, parse_target
from .store import InvalidTransition, ValidationError, _claim_options, _number

RETRYABLE_4XX = frozenset({408, 429})  # Request Timeout, Too Many Requests.
MAX_RESPONSE_BYTES = 65_536
EXCERPT_CHARACTERS = 200
# The largest allowed max_delay. The wait is capped at max_delay anyway, so a
# longer request changes nothing, and the receiver cannot push it out of range.
MAX_RETRY_AFTER = 86_400.0


class DeliveryError(Exception):
    """A transport failure before a complete status line arrived."""


@dataclass(frozen=True)
class Response:
    status: int
    retry_after: str | None
    body: bytes


def _abort(connection, expired):
    expired.set()
    sock = connection.sock
    if sock is not None:
        try:
            sock.shutdown(socket.SHUT_RDWR)  # Wakes a blocked read in the sending thread.
        except OSError:
            pass


def _describe(exc, timeout):
    if isinstance(exc, ConnectionRefusedError):
        return "connection refused"
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return f"timed out after {timeout:g} s"
    if isinstance(exc, ssl.SSLError):
        return "TLS handshake or certificate verification failed"
    if isinstance(exc, http.client.RemoteDisconnected):
        return "connection closed before a response"
    if isinstance(exc, ConnectionResetError):
        return "connection reset"
    if isinstance(exc, http.client.HTTPException):
        return "invalid HTTP response"
    return f"network error ({type(exc).__name__})"


def send(target, headers, body, *, timeout, max_response_bytes=4096) -> Response:
    """POST body to target within timeout seconds of wall-clock time.

    The socket timeout bounds connect and every read; a watchdog shuts the
    socket down at the deadline, so a receiver that trickles bytes cannot hold
    the attempt open. Redirects are returned as statuses, never followed.
    """
    if target.scheme == "https":
        connection = http.client.HTTPSConnection(target.host, target.port, timeout=timeout,
                                                 context=ssl.create_default_context())
    else:
        connection = http.client.HTTPConnection(target.host, target.port, timeout=timeout)
    expired = threading.Event()
    watchdog = threading.Timer(timeout, _abort, (connection, expired))
    watchdog.daemon = True
    watchdog.start()
    try:
        connection.connect()
        if expired.is_set():
            raise TimeoutError("deadline passed while connecting")
        connection.request("POST", target.path, body=body, headers=headers)
        response = connection.getresponse()
        try:
            excerpt = response.read(max_response_bytes)
        except (OSError, http.client.HTTPException):
            excerpt = b""  # The status line already decided the outcome.
        return Response(response.status, response.getheader("Retry-After"), excerpt)
    except (OSError, http.client.HTTPException) as exc:
        if expired.is_set():  # Same wording as a socket timeout: both mean the deadline passed.
            raise DeliveryError(f"timed out after {timeout:g} s") from None
        raise DeliveryError(_describe(exc, timeout)) from None
    finally:
        watchdog.cancel()
        connection.close()


def parse_retry_after(value, now):
    """Seconds requested by Retry-After (RFC 9110 §10.2.3), or None if unusable.

    Accepts delay-seconds or an HTTP-date; a date in the past means zero, and
    anything longer than MAX_RETRY_AFTER counts as MAX_RETRY_AFTER.
    """
    if value is None:
        return None
    value = value.strip()
    if re.fullmatch(r"[0-9]{1,10}", value):
        return min(float(int(value)), MAX_RETRY_AFTER)
    try:
        moment = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError, OverflowError):
        return None
    if moment is None or moment.tzinfo is None:
        return None
    return min(max(0.0, moment.timestamp() - now), MAX_RETRY_AFTER)


def _excerpt(body):
    text = body[:EXCERPT_CHARACTERS * 4].decode("utf-8", errors="replace")
    text = "".join(ch if ch.isprintable() else " " for ch in text).strip()
    return text[:EXCERPT_CHARACTERS]


def classify(response, *, now) -> Outcome:
    """Map a response to an outcome. See the table in docs/ARCHITECTURE.md."""
    status = response.status
    if 200 <= status <= 299:
        return Outcome("success", status)
    excerpt = _excerpt(response.body)
    detail = f"HTTP {status}" + (f": {excerpt}" if excerpt else "")
    if 300 <= status <= 399:
        return Outcome("permanent", status, f"HTTP {status}: redirect not followed; update the subscription URL")
    if status == 410:
        return Outcome("gone", status, "HTTP 410 Gone: subscription disabled")
    if status in RETRYABLE_4XX or 500 <= status <= 599:
        return Outcome("retry", status, detail, parse_retry_after(response.retry_after, now))
    if 400 <= status <= 499:
        return Outcome("permanent", status, detail)
    return Outcome("retry", status, f"HTTP {status}: unexpected status code")


def attempt(delivery, *, timeout, allow_remote=False, max_response_bytes=4096, clock=time.time) -> Outcome:
    """Sign and send one claimed delivery; never raises for network failures."""
    if delivery["subscription_state"] != "active":
        return Outcome("permanent", None, "subscription is disabled; no request was sent")
    try:
        target = parse_target(delivery["url"], allow_remote=allow_remote)
    except ValidationError as exc:
        return Outcome("permanent", None, f"target refused, no request was sent: {exc}"[:500])
    if not delivery["secrets"]:
        return Outcome("permanent", None, "no active signing secret; no request was sent")
    # The id is the message's; only the timestamp and signatures change per attempt.
    timestamp = int(clock())
    headers = {"Content-Type": "application/json", "User-Agent": f"RelayBox/{__version__}", "Connection": "close",
               "webhook-id": delivery["webhook_id"], "webhook-timestamp": str(timestamp),
               "webhook-signature": standard_webhooks.signature_header(
                   delivery["secrets"], delivery["webhook_id"], timestamp, delivery["body"])}
    try:
        response = send(target, headers, delivery["body"], timeout=timeout, max_response_bytes=max_response_bytes)
    except DeliveryError as exc:
        return Outcome("retry", None, str(exc))
    return classify(response, now=clock())


def run_dispatcher(outbox, *, worker="local-dispatcher", max_deliveries=1, lease_seconds=60, timeout=15,
                   allow_remote=False, max_response_bytes=4096, wait=0, sleep=time.sleep) -> dict:
    """Attempt up to max_deliveries claims, then stop.

    With wait > 0, keep claiming for up to that many seconds while retries or
    leases are pending; otherwise stop as soon as nothing is ready. A lease
    must outlast the attempt deadline, so no lease renewal is needed.
    Counts are per attempt; awaiting_retry counts deliveries this run left
    queued for a later retry.
    """
    if isinstance(max_deliveries, bool) or not isinstance(max_deliveries, int) or not 1 <= max_deliveries <= 1000:
        raise ValidationError("max_deliveries must be an integer between 1 and 1000")
    _claim_options(worker, lease_seconds)
    _number(timeout, 0.01, 60, "timeout")
    _number(wait, 0, 3600, "wait")
    if isinstance(max_response_bytes, bool) or not isinstance(max_response_bytes, int) or not 0 <= max_response_bytes <= MAX_RESPONSE_BYTES:
        raise ValidationError(f"max_response_bytes must be an integer between 0 and {MAX_RESPONSE_BYTES}")
    if lease_seconds <= timeout:
        raise ValidationError("lease_seconds must be greater than timeout so a lease outlasts its attempt")
    counts = {"claimed": 0, "succeeded": 0, "retried": 0, "dead_lettered": 0, "lost_leases": 0}
    final = {}  # Delivery ID -> state after this run's latest attempt on it.
    stop = time.monotonic() + wait
    while counts["claimed"] < max_deliveries:
        delivery = outbox.claim(worker, lease_seconds=lease_seconds)
        if delivery is None:
            due, remaining = outbox.next_due(), stop - time.monotonic()
            if due is None or remaining <= 0 or due - outbox.clock() > remaining:
                break
            sleep(max(0.0, due - outbox.clock()) + 0.01)
            continue
        counts["claimed"] += 1
        outcome = attempt(delivery, timeout=timeout, allow_remote=allow_remote,
                          max_response_bytes=max_response_bytes, clock=outbox.clock)
        try:
            state = outbox.complete(delivery["id"], delivery["lease_token"], outcome)
        except InvalidTransition:
            counts["lost_leases"] += 1
        else:
            counts[{"succeeded": "succeeded", "queued": "retried", "dead_letter": "dead_lettered"}[state]] += 1
            final[delivery["id"]] = state
    counts["awaiting_retry"] = sum(state == "queued" for state in final.values())
    return counts
