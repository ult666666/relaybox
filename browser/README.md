# Installed dashboard check in Chromium

Development tooling only: it is not part of the Python package, adds no runtime
dependency and adds no product route. It drives the dashboard served by a RelayBox
wheel installed in an isolated environment, using synthetic data. CI runs it in the
`browser` job of `.github/workflows/ci.yml`.

Use Node 24 and an environment where RelayBox was installed from a wheel. From this
directory:

```bash
npm ci --ignore-scripts
npx playwright install chromium   # Linux CI: npx playwright install --with-deps chromium
npm test -- /absolute/path/to/installed/environment/bin/python
```

Playwright 1.61.1 is locked as a development dependency. Chromium is required: when
it is missing the check fails; it is never skipped. Exit status 0 means every check
passed and the fixture shut down cleanly.

`fixture.py` runs with the given interpreter and `-I` from a temporary directory
outside the checkout. It asserts that `relaybox` comes from site-packages with
matching metadata, seeds nine synthetic deliveries at a supplied clock of
2026-10-23 20:00:00.25 UTC, and serves the installed app on loopback port 0. It
accepts only fixed synthetic actions on stdin and shuts down, closes and joins the
server at the end.

- **Installed behavior:** the actual API keeps exact seconds (`4.625`) while the
  page shows `About 4.6 seconds since ready`; counts, dates, state filter, payload
  as plain text, keyboard focus, manual refresh after a supplied-clock change and
  an empty inbox, no page overflow at 1440/1280/390/320 px, and the static
  no-JavaScript shell (data requires JavaScript).
- **Controlled probes** in the same browser, labeled separately in the receipt:
  substituted metrics responses for duration boundaries, null/zero/invalid waits,
  en-US and de-DE number formatting and year-9999 dates; injected 503s for
  independent section failures; and actual responses held back to show that late
  metrics, list and detail responses cannot replace newer ones.

Output goes to `../.local/browser/` (ignored), or to `RELAYBOX_BROWSER_OUTPUT` when
set: `receipt.json` and `synthetic-{1440,1280,390,320}.png`. These are focused
Chromium checks, not an accessibility, security, performance or cross-browser audit.
