# Anki engine benchmarks & crash-resilience tests (PRD 7g / 7h).
#
# These are graded, re-runnable tests that exercise the real engine through the
# desktop build's pylib:
#
#   make bench        one-command benchmark over a large deterministic deck
#   make crash-test   repeated abrupt-kill / no-corruption test
#
# They run against the already-built desktop env (out/pyenv + out/pylib). If
# that env is missing, build it first (e.g. `just build` or `./ninja pylib`).
# See docs/benchmarks.md for details.

SHELL := /bin/bash

# Desktop build's Python interpreter and the pylib it wraps.
PYTHON ?= out/pyenv/bin/python
export PYTHONPATH := out/pylib
# Engine tooling expects cargo + homebrew binaries on PATH.
export PATH := $(HOME)/.cargo/bin:/opt/homebrew/bin:$(PATH)

# --- Benchmark knobs (override on the CLI, e.g. `make bench BENCH_CARDS=10000`).
BENCH_CARDS ?= 50000
BENCH_SEED ?= 12345
BENCH_ITERS ?= 200
BENCH_HEAVY_ITERS ?= 50

# --- Crash-test knobs.
CRASH_ITERS ?= 25
CRASH_CARDS ?= 3000
CRASH_SEED ?= 777

.PHONY: bench bench-rebuild crash-test bench-clean check-env help

help:
	@echo "Anki engine test targets:"
	@echo "  make bench         - benchmark engine actions on a ~50k-card deck (PRD 7h)"
	@echo "  make bench-rebuild - bench, regenerating the cached deck from scratch"
	@echo "  make crash-test    - abrupt-kill / no-corruption test, >=20 iters (PRD 7g)"
	@echo "  make bench-clean   - delete cached benchmark collections (out/bench)"
	@echo ""
	@echo "Override knobs, e.g.: make bench BENCH_CARDS=10000 BENCH_ITERS=100"

# Fail early with a helpful message if the desktop build hasn't been produced.
check-env:
	@test -x "$(PYTHON)" || { \
	  echo "error: $(PYTHON) not found."; \
	  echo "Build the desktop env first (e.g. 'just build' or './ninja pylib')."; \
	  exit 1; }
	@test -d "out/pylib/anki" || { \
	  echo "error: out/pylib/anki not found - build pylib first."; \
	  exit 1; }

# One-command engine benchmark over a large, deterministic deck (PRD 7h).
bench: check-env
	$(PYTHON) tools/bench.py \
	  --cards $(BENCH_CARDS) --seed $(BENCH_SEED) \
	  --iters $(BENCH_ITERS) --heavy-iters $(BENCH_HEAVY_ITERS)

# Same as bench, but force-regenerate the cached deck.
bench-rebuild: check-env
	$(PYTHON) tools/bench.py \
	  --cards $(BENCH_CARDS) --seed $(BENCH_SEED) \
	  --iters $(BENCH_ITERS) --heavy-iters $(BENCH_HEAVY_ITERS) --rebuild

# Repeated open -> review writes -> SIGKILL mid-write -> reopen -> integrity
# check, asserting zero corruption across all iterations (PRD 7g).
crash-test: check-env
	$(PYTHON) tools/crash_test.py \
	  --iterations $(CRASH_ITERS) --cards $(CRASH_CARDS) --seed $(CRASH_SEED)

# Remove generated/cached benchmark collections.
bench-clean:
	rm -rf out/bench
	@echo "removed out/bench"
