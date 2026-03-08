# Makefile for wingman project
# Usage:
#   make test        -> run all tests
#   make test1       -> run region 33 continue-text OCR test
#   make test2       -> run region 9 INCO-text OCR test
#   make test-perf   -> run tests + generate CSV + chart
#   make test-perf-csv   -> generate performance CSV from git history
#   make test-perf-chart -> generate performance visualization chart
#   make report      -> run tests and generate HTML report
#   make clean       -> remove test output and screenshots
#   make wrelease    -> force add performance.json and commit with current version
#   make status      -> git status
#   make diff        -> git diff
#   make commit      -> commit all changes with a default message
#   make push        -> push current branch

.PHONY: test test1 test2 test-perf test-perf-csv test-perf-chart clean wrelease s d c t f n p squash run

# Generate HTML report for automated levels test
test:
	uv run pytest tests/test_automated_levels.py --html=tests/test-output/report.html --self-contained-html

# Run region 33 OCR check for "lick to C" on continue screenshots
test1:
	uv run pytest tests/test_automated_levels.py -k level4_region33_contains_lick_to_c -q

# Run region 9 OCR check for "INCO" on INCOMING screenshots
test2:
	uv run pytest tests/test_automated_levels.py -k level4_region9_contains_inco -q

# Generate CSV with performance trends from git history
test-perf-csv:
	uv run python tests/performance_tracking.py --csv

# Generate HTML visualization of performance trends
test-perf-chart:
	uv run python tests/performance_tracking.py --chart

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

# Clean test artifacts
clean:
	rm -rf tests/test-output
	rm -f tests/test-output/*.png
	rm -rf test_screenshots

# Force add ignored performance history file and commit with current version
wrelease:
	git add -f tests/test-output/performance.json
	version=$$(sed -n 's/^WINGMAN_VERSION = "\([^"]*\)"/\1/p' wingman/main.py); \
	test -n "$$version" || (echo "Could not parse WINGMAN_VERSION from wingman/main.py" && exit 1); \
	git diff --cached --quiet -- tests/test-output/performance.json && echo "No staged changes for tests/test-output/performance.json" || git commit -m "v$${version}: update performance baseline" -- tests/test-output/performance.json

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

p:
	git push

squash:
	git rebase -i --autosquash origin/main



run:
	wingman.bat