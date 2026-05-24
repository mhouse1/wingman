# Makefile for wingman project
# Usage:
#   make test        -> run all tests
#   make test1       -> run region 33 continue-text OCR test
#   make test2       -> run region 9 INCO-text OCR test
#   make test-perf   -> run tests + generate CSV + chart
#   make tp              -> run tests + preview chart with uncommitted data
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

.PHONY: test test1 test2 test-perf tp test-perf-csv test-perf-chart runtime-perf-csv-release runtime-perf-csv-preview runtime-perf-release runtime-perf-preview clean wrelease s d c t f n p squash r rd calibrate calibrate-crop add-crops

PYTHON ?= python
HAS_UV := $(shell if command -v uv >/dev/null 2>&1; then echo 1; else echo 0; fi)
PYTEST_RUN := $(if $(filter 1,$(HAS_UV)),uv run pytest,$(PYTHON) -m pytest)
PYTHON_RUN := $(if $(filter 1,$(HAS_UV)),uv run python,$(PYTHON))

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
tp: test
	$(PYTHON_RUN) tests/performance_tracking.py --include-current --chart
	@$(MAKE) runtime-perf-preview
	@echo ""
	@echo "✅ Performance preview complete (test + runtime)!"
	@echo "📊 View trends: tests/test-output/performance-trends.html"
	@echo "📈 CSV data: tests/test-output/performance-history.csv"
	@echo "📊 Runtime preview: docs/performance/runtime-performance-trends.preview.html"
	@echo "📈 Runtime CSV: docs/performance/current/runtime-performance-preview.csv"
	@echo ""
	@echo "⚠️  Chart includes UNCOMMITTED data - run 'make wrelease' to commit and finalize"
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
	uv run python tests/calibrate.py

calibrate-crop:
	uv run python tests/calibrate.py --crop $(CROP)

add-crops:
	uv run python tests/calibrate.py --add-new-crops