# Contributing

Use Python 3.11 or newer. The runtime and Python unittest suite need no third-party packages.

1. Create a focused branch and reproduce the behavior with a failing `unittest`.
2. Make the smallest implementation change that resolves the reproduction.
3. Run `python3 -m unittest discover -v`.
4. In a fresh virtual environment, run `python -m pip install .` and `python examples/verify_install.py` to check packaged assets and the CLI outside the checkout.
5. Update the API/architecture docs when a contract changes. For dashboard changes, check keyboard interaction, empty/error states, hostile text, and a narrow viewport.

For dashboard changes, also run the installed-wheel Chromium check in [`browser/`](browser/README.md) with Node 24, passing the fresh environment's interpreter:

```bash
cd browser
npm ci --ignore-scripts
npx playwright install chromium
npm test -- /absolute/path/to/fresh/environment/bin/python
```

Playwright 1.61.1 is locked there as a development dependency; it never enters the Python runtime. The check fails when Chromium is missing. It covers installed HTTP/assets, the readable wait display, independent refresh failures, late responses, hostile text, keyboard and narrow layouts, and saves synthetic screenshots plus a receipt under ignored `.local/browser/`. The older source-checkout script, `node examples/verify_dashboard.mjs /absolute/path/to/playwright python3.13`, still works for a quick check before packaging. CI runs the Python suites with installed HTTP/asset/worker/recovery checks, and a separate job runs the browser check against the built wheel.

Use synthetic data only. Do not commit local databases, screenshots containing secrets, generated package directories, or credentials. Respect the local-only boundary; internet-facing deployment or outbound delivery would require a separate threat model and design.

Describe the trigger, observable change, verification commands, and remaining limits in a pull request. Disclose material AI assistance and explain the review performed. Security reports follow [SECURITY.md](SECURITY.md).
