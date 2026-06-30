# Engine benchmarks & crash-resilience tests

Two graded, re-runnable tests that exercise the **real engine** (the Rust core
via the desktop build's `pylib`):

| Command           | What it does                                              | PRD |
| ----------------- | -------------------------------------------------------- | --- |
| `make bench`      | Times key engine actions on a large, deterministic deck  | 7h  |
| `make crash-test` | Repeatedly kills the engine mid-write and checks for corruption | 7g |

Both are thin `Makefile` targets over scripts in `tools/`:

- `tools/bench.py` — the benchmark
- `tools/crash_test.py` — the crash-resilience / no-corruption test
- `tools/_bench_common.py` — shared deterministic deck generation

## Prerequisites

These run against the **already-built desktop env**, not a fresh checkout. You
need `out/pyenv/` (the Python interpreter) and `out/pylib/` (the built pylib).
If they're missing, build them first, e.g.:

```bash
just build        # or: ./ninja pylib
```

The `Makefile` sets `PYTHONPATH=out/pylib`, puts `~/.cargo/bin` and
`/opt/homebrew/bin` on `PATH`, and invokes `out/pyenv/bin/python`. It fails
early with a clear message if the build env isn't present.

---

## `make bench` (PRD 7h)

### What it measures

A deterministic deck of ~50k review cards is generated once (and cached under
`out/bench/`), then each of these engine actions is timed and reported as
**p50 / p95 / worst (ms)**:

| Action                  | Call                                            |
| ----------------------- | ----------------------------------------------- |
| deck tree with counts   | `col.sched.deck_due_tree()`                     |
| get queued cards        | `col.sched.get_queued_cards()`                  |
| render card             | `card.render_output()`                          |
| answer card             | `col.sched.answerCard()`                        |
| points-at-stake query   | `backend.get_points_at_stake_queue()` (PRD 7a)  |
| find_cards / search     | `col.find_cards(...)` (several query shapes)     |

### Determinism & re-runnability

- A fixed seed (`--seed`, default `12345`) drives note content, topic tags and
  per-card FSRS memory state, so the same `--cards`/`--seed` always produces a
  byte-identical deck.
- The generated master is cached at `out/bench/bench_<cards>_<seed>.anki2`;
  each run copies it to a throwaway working file so the cache is never mutated.
  First run generates (~11s for 50k); later runs load from cache (~20ms).
- Daily review/new limits are lifted for the run so the **entire** 50k-card
  queue is live (the default 200/day cap would otherwise hide most of it).
- `answer card` is measured last because it mutates the queue; each timed
  answer is on a distinct card at the top of the queue (the only card the v3
  scheduler will answer).

### Usage

```bash
make bench                              # 50k cards, 200 iters (50 for heavy ops)
make bench BENCH_CARDS=10000            # smaller deck
make bench BENCH_ITERS=500 BENCH_HEAVY_ITERS=100
make bench-rebuild                      # regenerate the cached deck
make bench-clean                        # delete out/bench
```

Or directly:

```bash
PYTHONPATH=out/pylib out/pyenv/bin/python tools/bench.py --cards 50000 --seed 12345
```

### Reading the output

Times are wall-clock per call on a **warm** queue (each action gets a couple of
untimed warmup calls first). Whole-collection actions (deck tree, points-at-stake)
use fewer samples (`--heavy-iters`) than the light per-card actions (`--iters`).

The points-at-stake query is by far the heaviest action: it walks the entire due
review queue and computes FSRS retrievability for every card on each call, so it
scales with deck size (hundreds of ms at 50k) while the others stay in the
single-digit-ms range.

---

## `make crash-test` (PRD 7g, no-corruption part)

### What it does

For each iteration (default 25, minimum 20 enforced):

1. **open** a persistent collection,
2. spawn a **child process** that performs real review writes — answering due
   cards and inserting notes in a tight loop, committing as it goes,
3. **`SIGKILL`** that child mid-write (no clean close, no `atexit`, no flush) at
   a randomized offset once writes are confirmed in flight,
4. **reopen** the same collection and run a DB integrity + sanity check.

Across all iterations it asserts **zero corruption**. The same file is crashed
and reopened every iteration, so any latent corruption would accumulate and be
caught.

### Why this is a real test

Anki stores collections in SQLite **WAL** mode with `synchronous=FULL`. A hard
kill mid-transaction must roll back atomically on reopen. The test proves this
empirically against the real engine rather than assuming it.

The integrity check is strict — an iteration only passes if **all** of:

- the collection reopens without error,
- `pragma integrity_check` returns `ok` (physical integrity),
- `pragma foreign_key_check` returns no rows,
- Anki's own `col.fix_integrity()` reports **zero** problems (logical sanity).

### Usage

```bash
make crash-test                         # 25 iterations
make crash-test CRASH_ITERS=50          # more iterations
make crash-test CRASH_CARDS=5000        # larger crash deck
```

Or directly:

```bash
PYTHONPATH=out/pylib out/pyenv/bin/python tools/crash_test.py --iterations 25
```

### Output

Each iteration prints `PASS`/`FAIL`, how many writes the worker had committed
when it was killed, and the reopened card count, followed by a final
`N/N iterations with ZERO corruption` summary. A non-zero exit code means
corruption was detected.

---

## Notes

- Everything lives under `out/bench/` (git-ignored) and is safe to delete with
  `make bench-clean`.
- These tests only use the public `anki` pylib API, plus one batched SQL write
  to stamp FSRS memory state onto cards during generation (the same
  `{"s","d","lrt"}` JSON the public `Card` API produces).
