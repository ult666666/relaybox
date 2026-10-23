# Installed browser check — unfinished checkpoint

This source-only development check currently **fails intentionally** on the released
0.4.0 renderer: installed HTTP returns exact `4.625` seconds and the page displays
`4.625 seconds since ready`, while the expected readable display is
`About 4.6 seconds since ready`. The formatter has not been implemented. Probes
after that assertion have been written but have not yet run; they are not verified
coverage. No hosted browser job exists yet.

Use Node 24 and an isolated Python environment with RelayBox installed from a
wheel. From this directory:

```bash
npm ci --ignore-scripts
npx playwright install chromium
npm test -- /absolute/path/to/installed/environment/bin/python
```

On Linux CI the planned installation is `npx playwright install --with-deps chromium`.
Missing Chromium fails; it is never silently skipped. Playwright 1.61.1 is locked
as a development dependency and is not part of the Python runtime package.

The harness starts a standard-library Python fixture with `-I` outside the checkout,
asserts the package comes from site-packages, binds the real server to loopback port
0, and asks it to shut down/close/join at the end. The observed RED run shut down
cleanly with child exit 0. Data is synthetic and uses an explicit contemporary
supplied clock, not actual wall-clock latency. The fixture accepts only known
synthetic state actions on stdin and adds no product route.

Actual installed API/assets/first-load probes precede separately labeled metrics
rendering and response-delay/failure probes. The existing no-JavaScript page is
only a static shell; this check does not claim a functional data fallback or a full
accessibility, security, or performance audit. Scratch receipts and any synthetic
screenshots go under ignored `.local/browser/`; historical screenshots are untouched.

Resume by implementing the duration formatter from the T18 brief, running this
check past RED, then completing and verifying the remaining probes, hosted job,
0.4.1 version/docs/package checks and independent review before publication.
