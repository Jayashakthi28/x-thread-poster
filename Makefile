PYTHON := .venv/bin/python
RUFF := .venv/bin/ruff
MYPY := .venv/bin/mypy
PYTEST := .venv/bin/pytest

# Chrome is heavy: six concurrent browsers is the sweet spot on a typical
# 18-core dev machine. Override without editing this file:
#   XPOSTER_TEST_WORKERS=4 make check
TEST_WORKERS ?= $(if $(strip $(XPOSTER_TEST_WORKERS)),$(strip $(XPOSTER_TEST_WORKERS)),6)

.PHONY: help dev-install lint format typecheck test check check-fast test-browser test-file

help:
	@echo "x-thread-poster development targets"
	@echo ""
	@echo "  make dev-install          install the package in editable mode with dev extras"
	@echo "  make lint                 ruff check"
	@echo "  make format               ruff format (writes)"
	@echo "  make typecheck            mypy --strict on src/"
	@echo "  make test                 full pytest run (serial)"
	@echo "  make check                ruff + format-check + mypy + full pytest under xdist"
	@echo "                            ($(TEST_WORKERS) workers, --dist load; XPOSTER_TEST_WORKERS overrides)"
	@echo "  make check-fast           ruff + format-check + mypy + non-browser/non-live tests"
	@echo "                            (the fast inner loop; target < 30 s)"
	@echo "  make test-browser         only the real-Chrome tests, under xdist"
	@echo "  make test-file FILE=...   one test file, no xdist"
	@echo ""

dev-install:
	uv pip install --python $(PYTHON) -e ".[dev]"

lint:
	$(RUFF) check .

format:
	$(RUFF) format .

typecheck:
	$(MYPY)

test:
	$(PYTEST)

check:
	$(RUFF) check .
	$(RUFF) format --check .
	$(MYPY)
	$(PYTEST) -n $(TEST_WORKERS) --dist load

check-fast:
	$(RUFF) check .
	$(RUFF) format --check .
	$(MYPY)
	$(PYTEST) -m "not browser and not live" -n auto

test-browser:
	$(PYTEST) -m browser -n $(TEST_WORKERS) --dist load

test-file:
	@test -n "$(FILE)" || (echo "usage: make test-file FILE=tests/test_x.py" >&2; exit 2)
	$(PYTEST) "$(FILE)"
