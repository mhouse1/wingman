# Makefile for wingman project
# Usage:
#   make test        -> run all tests
#   make test1       -> run region 33 continue-text OCR test
#   make test2       -> run region 9 INCO-text OCR test
#   make test-perf   -> run tests + generate CSV + chart
#   make tp              -> run fast preview (tests + ADR044/ADR045 runtime gates + charts)
#   make tp-full         -> run full preview (tp + ADR037 PATH1/PATH2 OCR lane)
#   make test-perf-csv   -> generate performance CSV from git history
#   make test-perf-chart -> generate performance visualization chart
#   make runtime-perf-csv-release -> generate runtime release aggregate CSV
#   make runtime-perf-csv-preview -> generate runtime preview aggregate CSV
#   make runtime-perf-release -> generate runtime release chart artifacts
#   make runtime-perf-preview -> generate runtime preview chart artifacts
#   make report      -> run tests and generate HTML report
#   make clean       -> remove test output and screenshots
#   make wrelease    -> force add performance.json and commit with current version
#   make status      -> git status
#   make diff        -> git diff
#   make commit      -> commit all changes with a default message
#   make p           -> stage, commit (message "."), and push
#   make p "msg"     -> stage, commit with "msg", and push
#   make calibrate   -> calibrate all crop regions interactively (offline, no game needed)
#   make calibrate-crop CROP=<name> -> calibrate a single named crop (e.g. CROP=respawn)
#   make add-crops -> calibrate every image in test_screenshots/to_be_added as a new crop named after filename
#   make g           -> launch MetalStorm only, without starting Wingman (Linux only)
#   make r           -> run wingman (Linux: auto-launches game; Windows: game must be running)
#   make rd          -> run wingman with DEBUG log to wingman.log (same auto-launch on Linux)
#   make rg          -> alias for r (backwards compat)
#   make launch-game -> launch MetalStorm via umu-run in background (kills stale instance first)
#   make wait-game   -> poll until Metalstorm.exe process is alive, wait for lobby
#   make setup-capture -> one-time GNOME window picker: select MetalStorm, saves restore token
#   make y           -> run ADR37 replay integration smoke test (placeholder screenshots)
#   make ti          -> run integration tests (PATH1 + PATH2 real-OCR, alias for make ocr)
#   make newpaths    -> capture screenshots for PATH1 or PATH2 using live Wingman play
#   make leak-check  -> ADR 092 leak gate over logs/ (0 pass, 1 fail, 2 insufficient)
#   make p1          -> capture screenshots for PATH1 using live Wingman play
#   make p2          -> capture screenshots for PATH2 using live Wingman play

.PHONY: leak-check leak-check-gate test test1 test2 test-perf tp tp-full test-perf-csv test-perf-chart runtime-perf-csv-release runtime-perf-csv-preview runtime-perf-release runtime-perf-preview clean wrelease s d c t f n p squash q g r rd rg launch-game wait-game setup-capture capture-frame find-game move-game-window undecorate-game-window debug-crops y newpaths p1 p2 p3 rr-path1 rr-validate-path1 rr-path1-gate rr-live-path1 rr-live-validate-path1 rr-live-path1-gate calibrate recalibrate calibrate-crop add-crops ti preflight

PYTHON ?= python
HAS_UV := $(shell if command -v uv >/dev/null 2>&1; then echo 1; else echo 0; fi)
PYTEST_RUN := $(if $(filter 1,$(HAS_UV)),uv run --active pytest,$(PYTHON) -m pytest)
PYTHON_RUN := $(if $(filter 1,$(HAS_UV)),uv run --active python,$(PYTHON))
# Performance 008: cap glibc malloc arenas for every wingman run.
# The OCR pool runs 13 worker threads; glibc gives each its own arena, and
# freed blocks are returned to the arena rather than the OS. Measured
# 2026-08-21 over identical workloads (same n_ocr, same ocr_med):
#   default arenas   681 ->  4598 ->  7112 -> 10144 MB at 0/300/600/900 s
#   MALLOC_ARENA_MAX=2  684 ->  2453 ->  2637 ->  2545 MB  (plateau, memory returned)
# 2.5 GB is wingman's real footprint (13 thread-local EasyOCR readers);
# everything above it was arena fragmentation, not live data. Must be set
# before the process starts — glibc reads it at first malloc.
WINGMAN_ENV := MALLOC_ARENA_MAX=2
CAPTURE_PATH ?= PATH1
# Real-game capture (make p1/p2/p3) must outlast a full mission cycle
# (~6 min lobby->battle->missiles empty->respawn->match end), not the ~24 s
# replay pacing the out-of-order deadline is derived from. The ADR 045
# presenter lane hardcodes its own 30 s and is unaffected.
CAPTURE_TIMEOUT_S ?= 600.0
# Own artifact path, NOT wingman.log: this target rm -f's its log before
# launching, and pointing it at the production filename silently deleted real
# session logs every time the gate ran (the 2026-07-30 sessions were lost this
# way before log rotation existed; rotation cannot defend against a pre-launch rm).
RR_PATH1_LOG ?= tests/test-output/runtime_replay.path1.log
RR_PATH1_ASSERTIONS ?= tests/test-output/replay_assertions.path1.json
RR_PATH1_INTENTS ?= tests/test-output/replay_action_intents.path1.json
RR_PATH1_REPORT ?= tests/test-output/replay_required_screenshots.path1.json
RR_PATH1_SUMMARY ?= tests/test-output/runtime_replay_validation.path1.json
RR_PATH1_CONFIG ?= tests/replay_paths/adr044_runtime_path1.yaml
RR_PATH1_NAME ?= PATH1_RUNTIME
RR_LIVE_PATH1_LOG ?= wingman_live.log
RR_LIVE_PATH1_CAPTURE_CONFIG ?= tests/replay_paths/adr045_live_path1.yaml
RR_LIVE_PATH1_NAME ?= PATH1_LIVE
RR_LIVE_PATH1_CAPTURE_DIR ?= tests/test-output/live_capture_path1
RR_LIVE_PATH1_CAPTURE_SUMMARY ?= tests/test-output/capture_summary.path1.live.json
RR_LIVE_PATH1_VALIDATION_SUMMARY ?= tests/test-output/runtime_live_validation.path1.json
RR_LIVE_PATH1_PRESENTER_LOG ?= tests/test-output/live_presenter.path1.log
RR_LIVE_PATH1_PRESENTER_GRACE_S ?= 8.0

# Coding standard gate (Research 006). Rules and rationale live in
# pyproject.toml [tool.ruff.lint]; this target is what make tp gates on.
#
# `ruff format` is deliberately NOT part of this gate yet. Research 006 step 3
# calls for applying it once as a dedicated, behaviour-free commit; until that
# lands, `make lint` would fail on formatting alone and hide real findings.
# Sequence: run `make format` on its own, review and commit that diff, then move
# `format-check` into `lint` below.
lint:
	uv run ruff check .
	@echo "PASS: ruff lint clean"

# One-time (then routine) formatter pass — review the diff before committing.
format:
	uv run ruff format .

format-check:
	uv run ruff format --check .

# Validate host environment before first run (ADR 047)
preflight:
	$(PYTHON_RUN) tests/preflight.py

# Requirements gate (research 002 / ADR 066): validates the .sdoc documents and
# the @relation() source markers in wingman/. Exits 1 on any dangling relation
# in either direction. Fast (~4s), runs inside make tp.
reqs-gate:
	uv run strictdoc export . --formats=markdown --output-dir tests/test-output/strictdoc
	@echo "PASS: requirements + source traceability validated"

# Regenerate the committed markdown export beside the .sdoc sources.
reqs: reqs-gate
	cp tests/test-output/strictdoc/markdown/docs/requirements/*.md docs/requirements/
	@echo "docs/requirements markdown export refreshed"

# Generate HTML report for automated levels test
test:
	$(PYTEST_RUN) tests/test_automated_levels.py tests/test_main_game_end.py tests/test_analyzer.py tests/test_analyzer_lifecycle.py tests/test_mission_cancel.py tests/test_mission_stats.py tests/test_controller_no_keyboard.py tests/test_telemetry.py tests/test_eject_closed_loop.py tests/test_disengage_roll.py tests/test_missile_evade.py tests/test_resource_monitor.py tests/test_heap_census.py tests/test_performance_aggregate.py tests/test_climb_mode.py tests/test_live_capture_engine.py tests/test_replay.py tests/test_target_tracking.py tests/test_waiting_fallback.py tests/test_health_respawn.py tests/test_event_registry.py tests/test_stall_recovery.py tests/test_tick_handlers.py tests/test_engage_nav.py tests/test_minimap_bearing.py tests/test_behavior_tree.py tests/test_config_schema.py tests/test_controller_config.py tests/test_calibrate_config_writer.py tests/test_input_linux.py tests/test_invite_policy.py tests/test_account_tag.py tests/test_ocr_reader_reuse.py tests/test_lobby_popup_coverage.py tests/test_stall_profile_recovery.py tests/test_liveness_guard.py tests/test_leak_gate.py tests/test_handle_construction_sites.py tests/test_keybindings.py tests/test_finish_round_then_exit.py tests/test_reaction_segments.py --html=tests/test-output/report.html --self-contained-html

# Run region 33 OCR check for "lick to C" on continue screenshots
test1:
	$(PYTEST_RUN) tests/test_automated_levels.py -k level4_region33_contains_lick_to_c -q

# Run region 9 OCR check for "INCO" on INCOMING screenshots
test2:
	$(PYTEST_RUN) tests/test_automated_levels.py -k level4_region9_contains_inco -q

# Generate CSV with performance trends from git history
test-perf-csv:
	$(PYTHON_RUN) tests/performance_tracking.py --csv

# Generate HTML visualization of performance trends
test-perf-chart:
	$(PYTHON_RUN) tests/performance_tracking.py --chart

# Generate runtime aggregate CSV from release run_*.json
runtime-perf-csv-release:
	$(PYTHON_RUN) tests/runtime_performance_tracking.py --mode release --csv

# Generate runtime aggregate CSV from release + current run_*.json
runtime-perf-csv-preview:
	$(PYTHON_RUN) tests/runtime_performance_tracking.py --mode preview --csv

# Generate runtime release artifacts (release CSV + release chart)
runtime-perf-release:
	$(PYTHON_RUN) tests/runtime_performance_tracking.py --mode release --all

# Generate runtime preview artifacts (preview CSV + preview chart)
runtime-perf-preview:
	$(PYTHON_RUN) tests/runtime_performance_tracking.py --mode preview --all

# Run full workflow: test → CSV → chart
# after running this: git add -f 'c:/dev-tools/github/wingman/tests/test-output/performance.json'
# and commit that file to preserve performance history in git: git commit -m "v1.0.0: performance baseline"
# Note: performance.json is ignored by default, so you need to force add it if you want to keep it in git
# Then you can view the performance trends in tests/test-output/performance-trends.html and see how your changes affected performance over time
test-perf: test test-perf-csv test-perf-chart
	@echo ""
	@echo "✅ Performance test complete!"
	@echo "📊 View trends: tests/test-output/performance-trends.html"
	@echo "📈 CSV data: tests/test-output/performance-history.csv"
	@echo ""

# Preview performance trends including current uncommitted data
# Includes ADR044 PATH1 runtime replay gate and ADR045 live-screen gate.
# ADR 092: leak gate over the archived RESOURCE lines. Three outcomes, and
# INSUFFICIENT is never a pass — short sessions underread an accumulating defect
# roughly tenfold, so a green light from a 20-minute run retires the question
# while the defect is live.
#
#   exit 0 PASS   exit 1 FAIL   exit 2 INSUFFICIENT DATA
#
# tp warns on INSUFFICIENT and keeps going: it runs constantly and must stay
# usable without a recent soak. wrelease treats it as a failure, because
# shipping a build whose memory behaviour was never measured on a long session
# is exactly how the last leak shipped.
leak-check:
	@$(PYTHON_RUN) scripts/leak-check.py $(LEAK_ARGS)

leak-check-gate:
	@$(PYTHON_RUN) scripts/leak-check.py $(LEAK_ARGS); rc=$$?; \
	if [ $$rc -eq 1 ]; then \
		echo ""; echo "❌ LEAK GATE FAILED — see ADR 092 / Performance 008"; exit 1; \
	elif [ $$rc -eq 2 ]; then \
		echo ""; echo "⚠️  LEAK GATE: insufficient data — NOT a pass."; \
		echo "   Run a session of at least 1h before trusting a clean result."; \
	fi

# The gate set both preview targets must run. Shared so the two cannot drift:
# tp-full is documented as "tp + the ADR037 real-OCR lane" (CLAUDE.md), and it
# had silently fallen behind — reqs-gate was missing, and leak-check-gate was
# added to tp alone, leaving the "full" gate weaker than the fast one. A single
# variable makes that class of mistake impossible rather than comment-enforced.
TP_GATES := lint test reqs-gate rr-path1-gate rr-live-path1-gate leak-check-gate

tp: $(TP_GATES)
	$(PYTHON_RUN) tests/performance_tracking.py --include-current --chart
	@$(MAKE) runtime-perf-preview
	@echo ""
	@echo "✅ Performance preview complete (lint + test + ADR044/ADR045 runtime gates + runtime metrics)!"
	@echo "📊 View trends: tests/test-output/performance-trends.html"
	@echo "📈 CSV data: tests/test-output/performance-history.csv"
	@echo "📊 Runtime preview: docs/performance/runtime-performance-trends.preview.html"
	@echo "📈 Runtime CSV: docs/performance/current/runtime-performance-preview.csv"
	@echo ""
	@echo "⚠️  Chart includes UNCOMMITTED data - run 'make wrelease' to commit and finalize"
	@echo "🖥️  Live-screen ADR045 lane included in this preview run"
	@echo ""

# Full preview including ADR037 PATH1/PATH2 real-OCR integration tests.
tp-full: $(TP_GATES) ocr
	$(PYTHON_RUN) tests/performance_tracking.py --include-current --chart
	@$(MAKE) runtime-perf-preview
	@echo ""
	@echo "✅ Full performance preview complete (test + ADR037 + ADR044/ADR045 runtime gates + runtime metrics)!"
	@echo "📊 View trends: tests/test-output/performance-trends.html"
	@echo "📈 CSV data: tests/test-output/performance-history.csv"
	@echo "📊 Runtime preview: docs/performance/runtime-performance-trends.preview.html"
	@echo "📈 Runtime CSV: docs/performance/current/runtime-performance-preview.csv"
	@echo ""
	@echo "⚠️  Chart includes UNCOMMITTED data - run 'make wrelease' to commit and finalize"
	@echo "🧪 ADR037 PATH1/PATH2 OCR lane included in this full preview run"
	@echo "🖥️  Live-screen ADR045 lane included in this full preview run"
	@echo ""

# Clean test artifacts
clean:
	rm -rf tests/test-output
	rm -f tests/test-output/*.png
	rm -rf test_screenshots

# Force add ignored performance history file and commit with current version, then regenerate chart
# Assumes you've already updated the version in wingman/main.py and ran make test-perf or make test-perf-preview
# otherwise the performance.json file won't be updated with the latest performance data and the chart won't reflect the latest changes
# and there will be no performance history to commit if you haven't generated the performance.json file with the latest data
# once you ran wrelease you can then run make p to push the commit with the new version and performance data to GitHub
wrelease:
	@echo "ADR 092 leak gate (release: insufficient data blocks too)…"
	@$(PYTHON_RUN) scripts/leak-check.py $(LEAK_ARGS); rc=$$?; \
	if [ $$rc -ne 0 ]; then \
		echo ""; \
		if [ $$rc -eq 2 ]; then \
			echo "❌ Refusing to release: memory behaviour has never been measured"; \
			echo "   on a qualifying session. Run one of at least 1h (ADR 092)."; \
		else \
			echo "❌ Refusing to release: the leak gate failed (ADR 092)."; \
		fi; \
		exit 1; \
	fi
	@count=$$(ls docs/performance/current/run_*.json 2>/dev/null | wc -l); \
	if [ "$$count" -lt 5 ]; then \
		echo "WARNING: only $$count session(s) in docs/performance/current/ — recommended minimum is 5."; \
		printf "Release anyway with thin baseline? [y/N] "; \
		read answer; \
		if [ "$$answer" != "y" ] && [ "$$answer" != "Y" ]; then \
			echo "Aborted. Run more sessions, then retry."; \
			exit 1; \
		fi; \
	fi
	git add wingman/main.py
	git add -f tests/test-output/performance.json
	mkdir -p docs/performance/release
	cp docs/performance/current/run_*.json docs/performance/release/ 2>/dev/null; true
	git add docs/performance/release/
	rm -f docs/performance/current/run_*.json
	version=$$(sed -n 's/^WINGMAN_VERSION = "\([^"]*\)"/\1/p' wingman/main.py); \
	details=$$(sed -n 's/^WINGMAN_VERSION_DETAILS = "\([^"]*\)"/\1/p' wingman/main.py); \
	test -n "$$version" || (echo "Could not parse WINGMAN_VERSION from wingman/main.py" && exit 1); \
	test -n "$$details" || (echo "Could not parse WINGMAN_VERSION_DETAILS from wingman/main.py" && exit 1); \
	git diff --cached --quiet && echo "No staged changes to commit" || git commit -m "v$${version}: $${details}"
	@$(MAKE) test-perf-chart
	@$(MAKE) runtime-perf-release
	@echo ""
	@echo "✅ Version committed and charts updated!"
	@echo ""

# Git helpers
s:
	git status

d:
	git diff

c:
	git add .
	git commit -am "clean up"

t:
	git add .
	git commit -am "temporary commit"

f:
	git add .
	git commit --fixup HEAD

n:
	git add .
	git commit -am "new feature"

# Capture extra words after 'p' as the commit message (e.g. make p "my message").
# .DEFAULT absorbs the extra goal at execution time, avoiding eval which parses
# the string as makefile syntax and breaks on words like "include" or "define".
ifeq ($(firstword $(MAKECMDGOALS)),p)
  _P_MSG := $(wordlist 2,$(words $(MAKECMDGOALS)),$(MAKECMDGOALS))
  ifneq ($(_P_MSG),)
.DEFAULT:
	@:
  endif
endif

p:
	git add .
	git commit -am "$(if $(_P_MSG),$(_P_MSG),wip)"
	git push

squash:
	git rebase -i --autosquash origin/main

q:
	git pull



# On Linux (Wayland): auto-launch MetalStorm and set up PipeWire capture before running.
# On Windows: just run Wingman (game is started separately).
UNAME_S := $(shell uname -s 2>/dev/null || echo Windows)

ifeq ($(UNAME_S),Linux)
GAME_LAUNCH_DEPS := launch-game wait-game
else
GAME_LAUNCH_DEPS :=
endif

# Launch MetalStorm without starting Wingman (Linux: launch-game + wait-game; Windows: no-op).
g: $(GAME_LAUNCH_DEPS)

r: $(GAME_LAUNCH_DEPS)
	$(WINGMAN_ENV) $(PYTHON_RUN) -m wingman.main

rd: $(GAME_LAUNCH_DEPS)
	$(WINGMAN_ENV) $(PYTHON_RUN) -m wingman.main --log-file wingman.log

# ---------------------------------------------------------------------------
# Per-account run targets (Research 005)
#
# One Wine prefix per account, one shared GAME_EXE. The game's session lives in
# the prefix registry (Software\\Starform\\Metalstorm holds auth_token,
# selectedAccountId and generatedDeviceIdentifier), so a prefix IS an account.
# launch-game kills any running instance first, so switching accounts is just
# running the other target.
#
# WINGMAN_ACCOUNT tags run_*.json / run_*_stats.json so accounts at different
# progression never silently share a performance baseline.
#
# ONE-TIME BOOTSTRAP per account, before its first `make rN`:
#   1. cp -a $(HOME)/Games/Heroic/Prefixes/Metalstorm \
#            $(HOME)/Games/Heroic/Prefixes/Metalstorm-acct1
#   2. make g1          # launches from that prefix — comes up LOGGED OUT
#   3. Log in as the intended account, quit cleanly.
#   4. make g1 again — the login persists.
#
# VERIFIED 2026-08-21 (Research 005 Q1). The copy carries the Proton first-run
# setup but NOT the session, so each prefix is an independent account rather
# than a clone of one login — no risk of two targets sharing a session. The
# duplicated generatedDeviceIdentifier did not prevent a fresh login.
ACCT1_PREFIX ?= $(HOME)/Games/Heroic/Prefixes/Metalstorm-acct1
ACCT2_PREFIX ?= $(HOME)/Games/Heroic/Prefixes/Metalstorm-acct2

# Proton's prefix update (wineboot) on first launch of a COPIED prefix resets
# Wine-owned registry keys while leaving app keys intact, so a cp -a prefix
# loses its virtual desktop and launches true-fullscreen — which breaks the
# capture region, game_window_offset, and every calibrated crop. Idempotent, so
# it is a dependency of every per-account launch rather than a manual step.
VIRTUAL_DESKTOP_SIZE ?= 1920x1200

# Copy settings + keybindings from the main prefix into a per-account prefix,
# WITHOUT copying identity (ADR 052 Open Question 3). Deliberately manual, not a
# launch dependency: it overwrites the target's settings, so running it on every
# launch would discard any per-account tweak.
#   make sync-settings-1     # main prefix -> acct1
#   make sync-settings-1 DRY=--dry-run
SETTINGS_SRC_PREFIX ?= $(HOME)/Games/Heroic/Prefixes/Metalstorm

sync-settings-1:
	@$(PYTHON_RUN) scripts/sync-metalstorm-settings.py \
	  "$(SETTINGS_SRC_PREFIX)" "$(ACCT1_PREFIX)" $(DRY)

sync-settings-2:
	@$(PYTHON_RUN) scripts/sync-metalstorm-settings.py \
	  "$(SETTINGS_SRC_PREFIX)" "$(ACCT2_PREFIX)" $(DRY)

ensure-virtual-desktop:
	@$(PYTHON_RUN) scripts/ensure-virtual-desktop.py \
	  "$(WINE_PREFIX)" "$(VIRTUAL_DESKTOP_SIZE)"

g1: WINE_PREFIX := $(ACCT1_PREFIX)
g1: ensure-virtual-desktop g
g2: WINE_PREFIX := $(ACCT2_PREFIX)
g2: ensure-virtual-desktop g

r1: WINE_PREFIX := $(ACCT1_PREFIX)
r1: WINGMAN_ENV += WINGMAN_ACCOUNT=acct1
r1: ensure-virtual-desktop rd
r2: WINE_PREFIX := $(ACCT2_PREFIX)
r2: WINGMAN_ENV += WINGMAN_ACCOUNT=acct2
r2: ensure-virtual-desktop rd

.PHONY: g1 g2 r1 r2 ensure-virtual-desktop sync-settings-1 sync-settings-2

# Launch MetalStorm via umu-run + GE-Proton (no Heroic UI click needed).
# Always kills any stale instance and relaunches fresh so the window comes to front.
# NOTE: pattern split via shell variable — literal "Metalstorm.exe" never appears in the
# recipe shell's cmdline, so pkill cannot match and kill the recipe shell itself.
PROTON_ROOT    ?= $(HOME)/.var/app/com.heroicgameslauncher.hgl/config/heroic/tools/proton/GE-Proton-latest
WINE_PREFIX    ?= $(HOME)/Games/Heroic/Prefixes/Metalstorm
GAME_EXE       ?= $(HOME)/Games/Heroic/Metalstorm/Metalstorm.exe
UMU_RUN        ?= $(HOME)/.local/bin/umu-run
# Extra Unity player args, e.g. GAME_ARGS=-force-d3d11 to work around a
# graphics-backend crash on some GPUs. Empty by default — no effect unless set.
GAME_ARGS      ?=
launch-game:
	@_p=Metalstorm; \
	 if pgrep -f "$${_p}.exe" > /dev/null 2>&1; then \
	   echo "MetalStorm running — stopping before fresh launch…"; \
	   pkill -f "$${_p}.exe" 2>/dev/null || true; \
	   sleep 5; \
	 fi
	@rm -f /tmp/wingman-game-prerunning
	@GAMEID=umu-0 PROTONPATH="$(PROTON_ROOT)" WINEPREFIX="$(WINE_PREFIX)" \
	  "$(UMU_RUN)" "$(GAME_EXE)" $(GAME_ARGS) > /tmp/wingman-game-launch.log 2>&1 & \
	echo "MetalStorm launching via umu-run (log: /tmp/wingman-game-launch.log)"

# Poll until MetalStorm.exe is alive, then wait for the lobby.
# 60 s covers slow loading; Wingman also retries detection continuously.
GAME_WAIT_TIMEOUT_S ?= 120
GAME_LOBBY_WAIT_S   ?= 20
wait-game:
	@echo "Waiting for Metalstorm.exe process (timeout $(GAME_WAIT_TIMEOUT_S) s)…"
	@timeout $(GAME_WAIT_TIMEOUT_S) bash -c \
	  'until pgrep -f Metalstorm.exe > /dev/null 2>&1; do sleep 2; done' \
	  || { echo "ERROR: Metalstorm.exe not found after $(GAME_WAIT_TIMEOUT_S) s"; exit 1; }
	@echo "Metalstorm.exe detected — waiting $(GAME_LOBBY_WAIT_S) s for game window to appear…"
	@sleep $(GAME_LOBBY_WAIT_S)
	@$(MAKE) undecorate-game-window

# Capture one native-resolution frame via PipeWire and save to /tmp/wingman_native.png.
# Run with the game on screen to verify the window capture region.
capture-frame:
	$(PYTHON_RUN) wingman/capture_frame_debug.py

# Capture one frame and save each configured crop as /tmp/wingman_crop_<NAME>.png.
# Also saves /tmp/wingman_full_annotated.png with all crop rectangles drawn.
# Run with MetalStorm at the lobby to verify crop alignment.
debug-crops:
	$(PYTHON_RUN) wingman/debug_crops.py

# Reposition the Wine virtual desktop window via X11 ConfigureWindow (no interactive
# drag). Do NOT move this window by dragging its title bar — that has caused full
# desktop freezes on GNOME Wayland requiring a hard power-cycle. See ADR 054.
# Usage: make move-game-window X=100 Y=100
move-game-window:
	$(PYTHON_RUN) -m wingman.move_game_window --x $(X) --y $(Y)

# Strip the Wine virtual desktop window's title bar so there is no drag handle to
# grab — eliminates the interactive-drag freeze vector at the source. Run
# automatically by wait-game on every `make r` / `make rd`. See ADR 054.
undecorate-game-window:
	@$(PYTHON_RUN) -m wingman.move_game_window --undecorate || true

# Capture a frame with MetalStorm on screen and overlay a coordinate grid.
# Open /tmp/wingman_grid.png to find the game window's top-left (x,y) offset,
# then set game_window_offset in wingman/config.yaml.
find-game:
	$(PYTHON_RUN) wingman/find_game_window.py

# rg is now an alias for r on Linux (kept for backwards compatibility).
rg: r

# One-time GNOME Wayland capture setup (PipeWire portal restore token).
# One-time setup: GNOME window picker appears; select MetalStorm and click Share.
# Saves a restore token so future runs skip the dialog.
# Delete ~/.config/wingman/pw_restore_token.json to re-show the picker.
setup-capture:
	@echo "=== Wingman capture setup ==="
	@echo "A GNOME window picker will appear — select MetalStorm and click Share."
	@echo "Token saved to ~/.config/wingman/pw_restore_token.json for future runs."
	@echo ""
	$(PYTHON_RUN) wingman/portal.py

# ADR37 replay integration smoke path (temporary until full screenshot catalog exists)
y:
	$(PYTEST_RUN) tests/test_replay_integration_make_y.py -q

# ADR037 real-OCR integration tests (PATH1 + PATH2).
# Requires real game screenshots in test_screenshots/integration_test/.
# All-black placeholder screenshots cause tests to skip automatically.
ocr:
	$(PYTEST_RUN) tests/test_replay_integration_path1_path2.py tests/test_telemetry_corpus.py tests/test_stall_crops_ocr.py -m slow -v

# Alias for ocr: run integration tests (shorter to type).
ti:
	$(PYTEST_RUN) tests/test_replay_integration_path1_path2.py tests/test_telemetry_corpus.py tests/test_stall_crops_ocr.py -m slow -v

# ADR044 phase 1 runtime lane: run real main loop with replayed PATH1 screenshots.
rr-path1:
	mkdir -p tests/test-output
	rm -f $(RR_PATH1_LOG) $(RR_PATH1_ASSERTIONS) $(RR_PATH1_INTENTS) $(RR_PATH1_REPORT) $(RR_PATH1_SUMMARY)
	$(WINGMAN_ENV) $(PYTHON_RUN) -m wingman.main \
		--config wingman/config.yaml \
		--replay-config $(RR_PATH1_CONFIG) \
		--replay-path $(RR_PATH1_NAME) \
		--replay-screenshot-dir test_screenshots/integration_test \
		--replay-exit-after 3.0 \
		--replay-report $(RR_PATH1_REPORT) \
		--replay-intents-output $(RR_PATH1_INTENTS) \
		--replay-assertions-output $(RR_PATH1_ASSERTIONS) \
		--log-file $(RR_PATH1_LOG)

# ADR044 phase 1 validator: machine-check replay artifacts and runtime log signatures.
rr-validate-path1:
	$(PYTHON_RUN) tests/runtime_replay_validate.py \
		--log-file $(RR_PATH1_LOG) \
		--assertions-file $(RR_PATH1_ASSERTIONS) \
		--intents-file $(RR_PATH1_INTENTS) \
		--summary-out $(RR_PATH1_SUMMARY)

# ADR044 phase 1 gate: execute runtime lane and fail fast on validator mismatch.
rr-path1-gate: rr-path1 rr-validate-path1

# ADR045 live lane: present timed screenshots on desktop while Wingman captures real monitor frames.
rr-live-path1:
	mkdir -p tests/test-output $(RR_LIVE_PATH1_CAPTURE_DIR)
	rm -f $(RR_LIVE_PATH1_LOG) $(RR_LIVE_PATH1_CAPTURE_SUMMARY) $(RR_LIVE_PATH1_VALIDATION_SUMMARY) $(RR_LIVE_PATH1_PRESENTER_LOG)
	$(PYTHON_RUN) tests/live_screen_presenter.py \
		--config wingman/config.yaml \
		--path-config $(RR_LIVE_PATH1_CAPTURE_CONFIG) \
		--path $(RR_LIVE_PATH1_NAME) \
		--screenshot-dir test_screenshots/integration_test \
		--grace-s $(RR_LIVE_PATH1_PRESENTER_GRACE_S) \
		> $(RR_LIVE_PATH1_PRESENTER_LOG) 2>&1 & \
	PRES_PID=$$!; \
	sleep 2; \
	if ! kill -0 $$PRES_PID 2>/dev/null; then \
		echo "Live presenter failed to start; see $(RR_LIVE_PATH1_PRESENTER_LOG)"; \
		wait $$PRES_PID || true; \
		exit 1; \
	fi; \
	$(WINGMAN_ENV) $(PYTHON_RUN) -m wingman.main \
		--config wingman/config.yaml \
		--capture-path-config $(RR_LIVE_PATH1_CAPTURE_CONFIG) \
		--capture-path $(RR_LIVE_PATH1_NAME) \
		--capture-screenshot-dir $(RR_LIVE_PATH1_CAPTURE_DIR) \
		--capture-overwrite \
		--capture-pin-region \
		--capture-timeout-s 30.0 \
		--capture-summary $(RR_LIVE_PATH1_CAPTURE_SUMMARY) \
		--log-file $(RR_LIVE_PATH1_LOG); \
	STATUS=$$?; \
	wait $$PRES_PID || true; \
	exit $$STATUS

# ADR045 live lane validator.
rr-live-validate-path1:
	$(PYTHON_RUN) tests/runtime_live_validate.py \
		--log-file $(RR_LIVE_PATH1_LOG) \
		--capture-summary $(RR_LIVE_PATH1_CAPTURE_SUMMARY) \
		--summary-out $(RR_LIVE_PATH1_VALIDATION_SUMMARY)

# ADR045 live lane gate — skips gracefully when python3-tk is not installed.
rr-live-path1-gate:
	@$(PYTHON_RUN) -c "import tkinter" 2>/dev/null \
	|| { echo "SKIP: rr-live-path1-gate — python3-tk not installed (sudo apt install python3-tk)"; exit 0; }; \
	$(MAKE) rr-live-path1 && $(MAKE) rr-live-validate-path1

# Live capture screenshots for ADR037 replay paths.
# On Linux this auto-launches MetalStorm first, same as `make rd` (see GAME_LAUNCH_DEPS above).
# Example:
#   make newpaths CAPTURE_PATH=PATH1
#   make newpaths CAPTURE_PATH=PATH2
#   make newpaths CAPTURE_PATH=PATH3
newpaths: $(GAME_LAUNCH_DEPS)
	$(WINGMAN_ENV) $(PYTHON_RUN) -m wingman.main \
		--config wingman/config.yaml \
		--capture-path-config tests/replay_paths/adr037_paths.yaml \
		--capture-path $(CAPTURE_PATH) \
		--capture-screenshot-dir test_screenshots/integration_test \
		--capture-overwrite \
		--capture-allow-inject \
		--capture-timeout-s $(CAPTURE_TIMEOUT_S) \
		--capture-summary tests/test-output/capture_summary_$(CAPTURE_PATH).json \
		--log-level INFO

# Shortcut: refresh PATH1 screenshots.
p1:
	$(MAKE) newpaths CAPTURE_PATH=PATH1

# Shortcut: refresh PATH2 screenshots.
p2:
	$(MAKE) newpaths CAPTURE_PATH=PATH2

# Shortcut: refresh PATH3 screenshots (full-coverage path, all integration_test screenshots).
p3:
	$(MAKE) newpaths CAPTURE_PATH=PATH3

# Two commands are available for calibrating crop regions:
#
#   make calibrate
#     Walks through every screenshot in tests/calibration_map.yaml and lets you
#     click two corners per crop. Config is written immediately after each crop
#     so a Q quit saves whatever you completed.
#
#   make calibrate-crop CROP=respawn
#     Recalibrates a single named crop — useful when one region shifts but the
#     rest are still good. Replace respawn with any name from the crops: section
#     (incoming, click_to, good_luck, PLAY, event_refresh, event_refresh_dismiss).
#
#   make add-crops
#     Scans test_screenshots/to_be_added for images, then calibrates one crop per
#     image. The crop name is exactly the filename stem (without extension).
#
#   Controls in the window:
#     Click top-left corner, then bottom-right corner — saves the crop
#     S — skip (keeps the existing value; disabled if the crop has never been set)
#     Q — quit and save progress so far
calibrate:
	uv run --active python tests/calibrate.py

# Standard recalibration flow: run `make p1` first to refresh the gate-corpus
# screenshots, then `make recalibrate` to walk through every crop.
recalibrate: calibrate

calibrate-crop:
	uv run --active python tests/calibrate.py --crop $(CROP)

add-crops:
	uv run --active python tests/calibrate.py --add-new-crops