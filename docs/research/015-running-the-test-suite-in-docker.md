# Research 015 — Running the Test Suite in Docker

| Status | Date       | Wingman Version |
|--------|------------|-----------------|
| Draft  | 2026-09-24 | 1.8.11          |

## Question

`make test` expects an X display. Machines without one (CI runners, headless
boxes, Claude Code cloud sessions) cannot run it as-is, and one cloud session
was working around that by changing the tests. Can a Dockerfile supply the
display instead, so the suite runs unchanged anywhere Docker runs?

## Answer

Yes. A Docker image with a virtual X display (Xvfb) runs the full suite with
no test changes and no failures: **1983 passed, 0 failed** on
`secondary_attack`, and 1621 passed on `main`, the same result as a machine
with a real display. The image was built and run end to end in a Claude Code
cloud session.

## What Actually Needed the Display

Less than expected. Without a display, only two things break:

- **Two live-capture tests** in `tests/test_automated_levels.py` fail: `mss`
  raises `$DISPLAY not set` when it tries to grab the screen.
- **Collection stops outright** if the OS package `python3-tk` is missing,
  because `tests/calibrate.py` imports `tkinter`. That package cannot come
  from `uv.lock`.

Everything else already passes headless. The image supplies both missing
pieces, so no test needs rewriting.

## Results

All runs were made in a Claude Code cloud session on 2026-09-24. The first
five rows are `main` at `2e37d72`; the last is `secondary_attack` at
`b2f3c8f`, which has more tests.

| Where the suite ran                          | Result                                         |
|----------------------------------------------|------------------------------------------------|
| No display, no `python3-tk`                  | Stopped before any test ran (`tkinter` import) |
| No display, with `python3-tk`                | 1619 passed, **2 failed**, 59 skipped          |
| Xvfb virtual display, tests unchanged        | 1621 passed, 0 failed, 59 skipped              |
| Docker image, as root (cloud session)        | 1621 passed, 0 failed, 59 skipped              |
| Docker image, as uid 1000 (dev machine)      | 1621 passed, 0 failed, 59 skipped              |
| Docker image, as root, on `secondary_attack` | 1983 passed, 0 failed, 75 skipped              |

- Every skip is the untracked screenshot corpus (ADR 100 D7) or a
  machine-local log, not the display. On veda the corpus is part of the
  mounted checkout, so those tests should run there. That is not yet verified.
- A full run takes about 5 minutes on `main` and 8 on `secondary_attack`. The
  container adds no measurable time: on `main` it took 290 s against 303 s
  on the host.

## What Was Built

| File            | Purpose                                                                                     |
|-----------------|---------------------------------------------------------------------------------------------|
| `Dockerfile`    | Ubuntu 24.04 (matches the dev host), the locked Python environment, Xvfb, `python3-tk`, EasyOCR weights |
| `.dockerignore` | Sends only `pyproject.toml`, `uv.lock` and `.python-version` to the build                   |
| `Makefile`      | `make docker-test`, `make docker-shell`, `make docker-build`                                |
| `CLAUDE.md`     | Tells display-less sessions to use `make docker-test` instead of editing tests              |

Usage:

```bash
make docker-test                          # make test, in the container
make docker-test DOCKER_CMD="make lint"   # any other target
make docker-shell                         # interactive shell, same setup
```

## Key Design Choices

- **Dependencies only in the image.** The checkout is mounted at run time, so
  code edits never need a rebuild. Only changes to `pyproject.toml`,
  `uv.lock` or `.python-version` do. Results land in `tests/test-output/` as
  usual.
- **Runs as your user.** Files the tests write stay owned by you, not root.
- **No network during tests.** The EasyOCR weights are baked into the image.
  The suite passes without them, but the corpus-backed OCR tests and
  `make ocr` need them, and a throwaway container would otherwise download
  about 100 MB on every run.
- **Screen sized to the capture region (1920x1200).** On a smaller virtual
  screen the two live-capture tests quietly skip as "MetalStorm not running"
  instead of passing.
- **Test keystrokes stay in the container.** Injected keys land on the
  container's own display, never on the operator's desktop (see the
  stuck-key incident in `tests/conftest.py`).
- **Same Python rules as the dev host.** The venv sits on the system Python,
  `python3-tk` comes from apt, every package comes from `uv.lock`, and no X
  server is installed on the host (`CLAUDE.md`, "Python Environment"). The
  `python3-gi` binding is left out because only the Wayland capture backend
  uses it.

## Hurdles in Claude Code Cloud Sessions

Docker works in these sandboxes, but five things got in the way. Each is now
handled in the image or documented in `CLAUDE.md`.

| Hurdle                            | What happened                                   | Resolution                                                        |
|-----------------------------------|-------------------------------------------------|-------------------------------------------------------------------|
| Docker daemon not running         | Docker is installed but not started             | Start it once per session (command in `CLAUDE.md`)                |
| GitHub container registry blocked | Downloads from `ghcr.io` refused                | `uv` is installed from PyPI instead                               |
| HTTPS interception                | Containers rejected the sandbox's certificates  | The Makefile hands the sandbox's CA to the build as a secret; it is never stored in the image |
| Docker Hub rate limit             | `429 Too Many Requests`, twice                  | A `BASE_IMAGE` override pulls the same image from Google's mirror |
| Disk allowance                    | The first build ran out of space                | A cold build needs about 21 GB free; skip the host `uv sync`      |

## Costs and Limits

- **Image size: 13.8 GB on disk.** Most of it is the CUDA build of torch that
  `uv.lock` pins, although OCR runs on the CPU (`use_gpu: false`). A CPU-only
  torch would cut roughly 5 GB, but it changes the lock on veda too, so it is
  an operator decision.
- **Cold build: about 9 minutes** (548 s): OS packages 115 s, Python
  environment and weights 75 s, writing the image 353 s.
- **Scope.** Covers `make test`, `make lint` and similar targets. The `tp`
  gates still need veda's corpus, and the game itself cannot run in the
  container.
- **Quicker option for cloud sessions.** Xvfb is already installed there, so
  `apt-get install -y python3-tk` followed by `xvfb-run -a make test` gives
  the same result without building the image.

## Status and Next Steps

- [x] Measure what fails without a display
- [x] Confirm a virtual display fixes it with the tests unchanged
- [x] Build the image and run the full suite in it, as root and as uid 1000
- [x] Reimplement the change on `secondary_attack` and re-run the suite there
- [ ] Commit and push to `secondary_attack` (awaiting operator review). The
      other cloud session working on that branch must pull before it pushes
      again.
- [ ] Run the container on veda with the corpus mounted and confirm the
      corpus-gated tests pass
- [ ] Decide on a CPU-only torch to shrink the image
- [ ] Optionally run `make docker-test` in CI, which today runs only
      `tests/test_analyzer.py`. Check the runner's free disk first: a cold
      build needs about 21 GB.

## Related Documents

- `Dockerfile`, `.dockerignore` and the `docker-*` targets in `Makefile`: the
  implementation, with the reasoning in their comments.
- `docs/hldd/009-nested-display-isolation-hldd.md`: how the app chooses
  displays at run time. The container does not change that path.
- `docs/adr/100-repository-growth-and-generated-artifacts.md`: D7 moved the
  screenshot corpus out of git, which is why the corpus tests skip.
- `scripts/setup-linux.sh`: the dev host's package list that the image
  mirrors.
