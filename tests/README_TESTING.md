# Testing

The suite has two kinds of tests:

- **pure tests** — no browser, no network; they finish in a few seconds;
- **`browser` tests** — launch the locally installed Google Chrome headless
  (never Playwright's Chromium, never the network: `tests/fake_x` serves x.com
  through a Playwright route). They dominate the suite's wall time, because
  every test pays a fresh Chrome start on its own temporary profile.

`live` tests are skipped unless `XPOSTER_LIVE=1` points at a real signed-in
session; they are never part of the normal loop.

## While iterating (seconds, not minutes)

```bash
make check-fast                 # ruff + format-check + mypy + every non-browser test
make test-file FILE=tests/test_poster.py   # one file, serial, no xdist
make test-browser               # only the real-Chrome tests, parallel
```

`make check-fast` skips `browser` and `live` tests (`pytest -m "not browser and
not live" -n auto`). It is the inner-loop command: it must stay under roughly
30 seconds. Run the targeted file(s) you changed next to it:

```bash
make test-file FILE=tests/test_pacing.py
make test-file FILE=tests/test_poster.py   # slower: real Chrome per test
```

Add `-k <pattern>` by calling pytest directly when you only need one case:

```bash
.venv/bin/pytest tests/test_poster.py -k media -x
```

## Before declaring done (once)

```bash
make check
```

`make check` is the definition of done: `ruff check`, `ruff format --check`,
`mypy --strict`, then the **whole** suite under xdist. Do not replace it with
`check-fast` or a single file at the end of a task.

The full run defaults to six xdist workers with `--dist load`: tests are spread
one by one, because each test owns its temporary home/profile and no test
depends on another in the same file. `--dist loadfile` is also safe but much
slower here: `tests/test_poster.py` alone holds ~80 Chrome tests, so one worker
becomes the critical path. Override the worker count when the machine is busy or
has fewer cores:

```bash
XPOSTER_TEST_WORKERS=4 make check       # busy machine
XPOSTER_TEST_WORKERS=12 make check      # free 18-core Mac: ~2 min instead of ~4.5
```

Measured on an 18-core Mac (2026-11-10, ~1,080 tests):

| command | wall time |
| --- | --- |
| `pytest` (serial) | ~11–12 min |
| `make check` (`-n 6`, `--dist load`) | ~4–4.5 min |
| `make check` (`-n 12`, `--dist load`) | ~2.2 min |
| `make test-browser` (`-n 12`) | ~1.5 min |
| `make check-fast` | ~7 s |

Six workers mean up to six concurrent Chrome instances. On the 18-core dev Mac
twelve are still reliable, but keep the default at six so a laptop or a machine
shared with other agents does not thrash; raise it through the environment
variable when the full run is the only thing on the box.

## Keeping tests xdist-safe

Every browser test must:

- use `tmp_home` (or its own `tmp_path`-rooted `XPosterConfig`) for the Chrome
  profile and all state — never the operator's real `~/.olsa` home;
- bind loopback servers to port `0` and read the assigned port back;
- avoid fixed file names outside `tmp_path`, and avoid mutating process-global
  state except through `monkeypatch` (which is per-test and restored);
- avoid `time.sleep` for synchronisation; wait for the actual condition
  (Playwright `expect`/`wait_for_function`) or use the injected sleep seam.

Tests that monkeypatch something on a shared module (`browser.subprocess`,
`os.kill`, `time.monotonic`, `XPOSTER_*` environment variables) are safe under
xdist because every worker is a separate process and `monkeypatch` unwinds per
test; keep those patches scoped to a single test.

## Finding the slow ones

```bash
.venv/bin/pytest --durations=30                    # serial, no other load
.venv/bin/pytest -m browser --durations=20 -n 12
```

The slowest tests are the poster/diagnose flows that navigate several pages and
exercise fallback selectors: the fake's "new" DOM matches the fifth or sixth
candidate of selectors such as `composer_root` and `composer_textarea_0`, and
each miss costs its share of `default_timeout_ms` (minimum 400 ms). They are
slow by design, not flaky; do not delete the fallbacks or shrink the selector
budgets to make them faster.

A browser test that spends seconds waiting on a fixed timeout is a test bug:
inject the shorter timeout through the existing config/argument, do not lower a
production default.
