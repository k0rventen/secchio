# Verification entrypoint for secchio.
#
#   make verify     run every check (do this before handing work back)
#   make lint       ruff lint + format check
#   make typecheck  mypy
#   make pyright    pyright
#   make shellcheck shellcheck the restic-pod entrypoint
#   make test       pytest
#   make format     auto-fix lint + formatting
#
# Everything runs through `uv` against the deps declared in pyproject.toml.

UV ?= uv
SHELLCHECK ?= shellcheck
SRC := src
TESTS := tests
RESTIC_POD := restic-pod

.PHONY: verify lint typecheck pyright shellcheck test format

verify: lint typecheck pyright shellcheck test

lint:
	$(UV) run ruff check $(SRC) $(TESTS)
	$(UV) run ruff format --check $(SRC) $(TESTS)

format:
	$(UV) run ruff check --fix $(SRC) $(TESTS)
	$(UV) run ruff format $(SRC) $(TESTS)

typecheck:
	$(UV) run mypy $(SRC)

pyright:
	$(UV) run pyright

shellcheck:
	$(SHELLCHECK) $(RESTIC_POD)/entrypoint.sh

test:
	$(UV) run pytest -q
