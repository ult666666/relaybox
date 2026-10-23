# Contributing

Use Python 3.11 or newer. The runtime and Python unittest suite need no third-party packages.

1. Create a focused branch and reproduce the behavior with a failing `unittest`.
2. Make the smallest implementation change that resolves the reproduction.
3. Run `python3 -m unittest discover -v`.
4. In a fresh virtual environment, run `python -m pip install .` and `python examples/verify_install.py` to check packaged assets and the CLI outside the checkout.
5. Update the API/architecture docs when a contract changes. For dashboard changes, check keyboard interaction, empty/error states, hostile text, and a narrow viewport.

Optional dashboard verification uses separately installed development tools, with no repository npm setup or runtime dependency:

```bash
node examples/verify_dashboard.mjs /absolute/path/to/playwright python3.13
```

The script requires Playwright 1.61.1 with its Chromium browser already installed. It starts only a local test server, drives a logical-clock fixture, and checks independent refresh failures, stale response ordering, hostile text, keyboard and mobile behavior. Screenshots are saved under ignored `.local/`. This optional browser check is not part of `unittest` or a claimed hosted CI result. Standard CI runs the Python suites and installed HTTP/asset/worker/recovery checks.

Use synthetic data only. Do not commit local databases, screenshots containing secrets, generated package directories, or credentials. Respect the local-only boundary; internet-facing deployment or outbound delivery would require a separate threat model and design.

Describe the trigger, observable change, verification commands, and remaining limits in a pull request. Disclose material AI assistance and explain the review performed. Security reports follow [SECURITY.md](SECURITY.md).
