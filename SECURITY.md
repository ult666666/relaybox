# Security

RelayBox is an educational, local-only demo. It has no supported production deployment configuration or independent security audit. Do not expose it through a tunnel, reverse proxy, public interface, or shared environment containing real data.

The HTTP service uses a shared HMAC secret for ingestion and checks timestamp freshness. It refuses non-loopback binding, rejects unacceptable Host and cross-site headers, bounds JSON bodies and concurrent handlers, and uses parameterized SQL. These controls do not authenticate readers on the local machine. Any process that can access the loopback port can read stored payloads; any user with the database can change state through the library or CLI.

The public `--demo-secret` key is for synthetic data only. For local experiments, use a private random key in `RELAYBOX_SECRET`, a private database directory, and avoid putting secrets in payloads, command history, screenshots, issues, or source control. Data is stored unencrypted with no automatic retention or backup. The repository ignores `.env` and SQLite files, but this is not a data-loss prevention system.

Leases provide at-least-once processing. A stale token cannot acknowledge or fail a reclaimed job, but tokens do not prevent external side effects. There is no external delivery worker. Local clock changes affect leases and timestamp freshness.

If you find a vulnerability, use the repository's private vulnerability reporting option if available. If unavailable, open a minimal public issue requesting a private contact route without exploit payloads, secrets, or sensitive data. No response-time guarantee is offered. Include version, a synthetic reproduction, expected behavior, and the affected boundary after a private route is established.
