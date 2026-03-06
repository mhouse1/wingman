# Makefile for wingman project
# Usage:
#   make test        -> run all tests
#   make report      -> run tests and generate HTML report
#   make clean       -> remove test output and screenshots
#   make status      -> git status
#   make diff        -> git diff
#   make commit      -> commit all changes with a default message
#   make push        -> push current branch

.PHONY: test clean s d c t f n p squash run

# Generate HTML report for automated levels test
test:
	uv run pytest tests/test_automated_levels.py --html=tests/test-output/report.html --self-contained-html

# Clean test artifacts
clean:
	rm -rf tests/test-output
	rm -f tests/test-output/*.png
	rm -rf test_screenshots

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