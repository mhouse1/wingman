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
#   make r           -> run wingman (INFO console only)
#   make rd          -> run wingman with DEBUG log written to wingman.log
#   make y           -> run ADR37 replay integration smoke test (placeholder screenshots)
#   make ti          -> run integration tests (PATH1 + PATH2 real-OCR, alias for make ocr)
#   make newpaths    -> capture screenshots for PATH1 or PATH2 using live Wingman play
#   make p1          -> capture screenshots for PATH1 using live Wingman play
#   make p2          -> capture screenshots for PATH2 using live Wingman play

.PHONY: test test1 test2 test-perf tp tp-full test-perf-csv test-perf-chart runtime-perf-csv-release runtime-perf-csv-preview runtime-perf-release runtime-perf-preview clean wrelease s d c t f n p squash r rd y newpaths p1 p2 rr-path1 rr-validate-path1 rr-path1-gate rr-live-path1 rr-live-validate-path1 rr-live-path1-gate calibrate calibrate-crop add-crops ti

PYTHON ?= python
HAS_UV := $(shell if command -v uv >/dev/null 2>&1; then echo 1; else echo 0; fi)
PYTEST_RUN := $(if $(filter 1,$(HAS_UV)),uv run --active pytest,$(PYTHON) -m pytest)
PYTHON_RUN := $(if $(filter 1,$(HAS_UV)),uv run --active python,$(PYTHON))
CAPTURE_PATH ?= PATH1
CAPTURE_TIMEOUT_S ?= 120.0
RR_PATH1_LOG ?= wingman.log
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

# Generate HTML report for automated levels test
test:
	$(PYTEST_RUN) tests/test_automated_levels.py tests/test_main_game_end.py tests/test_analyzer.py --html=tests/test-output/report.html --self-contained-html

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
tp: test rr-path1-gate rr-live-path1-gate
	$(PYTHON_RUN) tests/performance_tracking.py --include-current --chart
	@$(MAKE) runtime-perf-preview
	@echo ""
	@echo "✅ Performance preview complete (test + ADR044/ADR045 runtime gates + runtime metrics)!"
	@echo "📊 View trends: tests/test-output/performance-trends.html"
	@echo "📈 CSV data: tests/test-output/performance-history.csv"
	@echo "📊 Runtime preview: docs/performance/runtime-performance-trends.preview.html"
	@echo "📈 Runtime CSV: docs/performance/current/runtime-performance-preview.csv"
	@echo ""
	@echo "⚠️  Chart includes UNCOMMITTED data - run 'make wrelease' to commit and finalize"
	@echo "🖥️  Live-screen ADR045 lane included in this preview run"
	@echo ""

# Full preview including ADR037 PATH1/PATH2 real-OCR integration tests.
tp-full: test rr-path1-gate rr-live-path1-gate ocr
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

# Extra words after 'p' are joined as the commit message: make p "my comment"
ifeq (p,$(firstword $(MAKECMDGOALS)))
  _P_MSG := $(wordlist 2,$(words $(MAKECMDGOALS)),$(MAKECMDGOALS))
  ifneq (,$(_P_MSG))
    $(eval $(_P_MSG):;@:)
  endif
endif

p:
	git add .
	git commit -am "$(if $(_P_MSG),$(_P_MSG),.)"
	git push

squash:
	git rebase -i --autosquash origin/main



r:
	wingman.bat

rd:
	wingman.bat --log-file wingman.log

# ADR37 replay integration smoke path (temporary until full screenshot catalog exists)
y:
	$(PYTEST_RUN) tests/test_replay_integration_make_y.py -q

# ADR037 real-OCR integration tests (PATH1 + PATH2).
# Requires real game screenshots in test_screenshots/integration_test/.
# All-black placeholder screenshots cause tests to skip automatically.
ocr:
	$(PYTEST_RUN) tests/test_replay_integration_path1_path2.py -m slow -v

# Alias for ocr: run integration tests (shorter to type).
ti:
	$(PYTEST_RUN) tests/test_replay_integration_path1_path2.py -m slow -v

# ADR044 phase 1 runtime lane: run real main loop with replayed PATH1 screenshots.
rr-path1:
	mkdir -p tests/test-output
	rm -f $(RR_PATH1_LOG) $(RR_PATH1_ASSERTIONS) $(RR_PATH1_INTENTS) $(RR_PATH1_REPORT) $(RR_PATH1_SUMMARY)
	$(PYTHON_RUN) -m wingman.main \
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
	$(PYTHON_RUN) -m wingman.main \
		--config wingman/config.yaml \
		--capture-path-config $(RR_LIVE_PATH1_CAPTURE_CONFIG) \
		--capture-path $(RR_LIVE_PATH1_NAME) \
		--capture-screenshot-dir $(RR_LIVE_PATH1_CAPTURE_DIR) \
		--capture-overwrite \
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

# ADR045 live lane gate.
rr-live-path1-gate: rr-live-path1 rr-live-validate-path1

# Live capture screenshots for ADR037 replay paths.
# Example:
#   make newpaths CAPTURE_PATH=PATH1
#   make newpaths CAPTURE_PATH=PATH2
newpaths:
	$(PYTHON_RUN) -m wingman.main \
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

calibrate-crop:
	uv run --active python tests/calibrate.py --crop $(CROP)

add-crops:
	uv run --active python tests/calibrate.py --add-new-crops