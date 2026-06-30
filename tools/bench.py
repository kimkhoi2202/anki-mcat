#!/usr/bin/env python3
# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""One-command engine benchmark over a large, deterministic deck (PRD 7h).

Generates (or loads from cache) a ~50k-card collection with a fixed seed, then
times the key engine actions and prints p50 / p95 / worst (ms) for each:

  * deck tree with counts        col.sched.deck_due_tree()
  * get queued cards             col.sched.get_queued_cards()
  * render card                  card.render_output()
  * answer card                  col.sched.answerCard()
  * points-at-stake query        backend.get_points_at_stake_queue()  (PRD 7a)
  * find_cards / search          col.find_cards(...)

Usage (from the repo root, via the desktop env)::

    PYTHONPATH=out/pylib out/pyenv/bin/python tools/bench.py
    PYTHONPATH=out/pylib out/pyenv/bin/python tools/bench.py --cards 50000 --iters 200

Or simply ``make bench``. The deck is deterministic given ``--cards``/``--seed``
and cached under ``out/bench/``, so re-runs are fast and reproducible.
"""

from __future__ import annotations

import argparse
import math
import os
import sys
import time

# Allow running as a plain script (``python tools/bench.py``).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _bench_common as common  # noqa: E402


def percentile(values: list[float], pct: float) -> float:
    """Nearest-rank percentile in ms."""
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = max(0, math.ceil(pct / 100.0 * len(ordered)) - 1)
    return ordered[rank]


def measure(fn, iterations: int, warmup: int = 2) -> list[float]:
    """Time ``fn`` ``iterations`` times (after ``warmup`` untimed calls)."""
    for _ in range(warmup):
        fn()
    return [common.timed(fn) for _ in range(iterations)]


class Results:
    def __init__(self) -> None:
        self.rows: list[tuple[str, list[float]]] = []

    def add(self, name: str, samples: list[float]) -> None:
        self.rows.append((name, samples))

    def print_table(self) -> None:
        header = ("action", "n", "p50 ms", "p95 ms", "worst ms", "mean ms")
        widths = [40, 6, 10, 10, 10, 10]

        def fmt_row(cells: tuple[str, ...]) -> str:
            return "  ".join(
                str(c).ljust(w) if i == 0 else str(c).rjust(w)
                for i, (c, w) in enumerate(zip(cells, widths))
            )

        print(fmt_row(header))
        print("  ".join("-" * w for w in widths))
        for name, samples in self.rows:
            if not samples:
                print(fmt_row((name, "0", "-", "-", "-", "-")))
                continue
            p50 = percentile(samples, 50)
            p95 = percentile(samples, 95)
            worst = max(samples)
            mean = sum(samples) / len(samples)
            print(
                fmt_row(
                    (
                        name,
                        len(samples),
                        f"{p50:.3f}",
                        f"{p95:.3f}",
                        f"{worst:.3f}",
                        f"{mean:.3f}",
                    )
                )
            )


def run_benchmarks(bench: common.BenchCollection, iters: int, heavy_iters: int) -> Results:
    col = bench.col
    sched = col.sched
    backend = col._backend
    results = Results()

    # Warm up the queue once so the first timed call isn't paying for the
    # initial queue build.
    queued = sched.get_queued_cards(fetch_limit=1)
    review_count = queued.review_count
    print(
        f"deck={common.DECK_NAME} cards={bench.num_cards} seed={bench.seed} "
        f"live_review_queue={review_count}\n"
    )

    # 1. Deck tree with counts (whole-collection).
    results.add("deck tree with counts", measure(sched.deck_due_tree, heavy_iters))

    # 2. Get queued cards (next card + remaining counts).
    results.add(
        "get queued cards",
        measure(lambda: sched.get_queued_cards(fetch_limit=1), iters),
    )

    # 3. Points-at-stake query (PRD 7a) over the whole due review queue.
    results.add(
        "points-at-stake query",
        measure(
            lambda: backend.get_points_at_stake_queue(
                topic_tag_prefix=common.TOPIC_TAG_PREFIX, weight_by_topic_size=False
            ),
            heavy_iters,
        ),
    )

    # 4. find_cards / search variants.
    all_ids = col.find_cards(f"deck:{common.DECK_NAME}")
    results.add(
        f"find_cards: deck:{common.DECK_NAME} ({len(all_ids)})",
        measure(lambda: col.find_cards(f"deck:{common.DECK_NAME}"), iters),
    )
    results.add(
        "find_cards: tag:MCAT::Physics::*",
        measure(lambda: col.find_cards("tag:MCAT::Physics::*"), iters),
    )
    results.add(
        "find_cards: full-text 'concept #12345'",
        measure(lambda: col.find_cards("concept #12345"), iters),
    )
    results.add(
        "find_cards: deck + is:due",
        measure(lambda: col.find_cards(f"deck:{common.DECK_NAME} is:due"), iters),
    )

    # 5. Render card (template render incl. note fetch), on distinct cards.
    render_ids = list(all_ids[: iters + 2])
    render_cards = [col.get_card(cid) for cid in render_ids]
    render_samples: list[float] = []
    for idx, card in enumerate(render_cards):
        sample = common.timed(lambda c=card: c.render_output(reload=True))
        if idx >= 2:  # drop 2 warmup renders
            render_samples.append(sample)
    results.add("render card", render_samples)

    # 6. Answer card (MUTATING — runs last). The v3 scheduler only answers the
    # card at the top of the queue, so we fetch the next card (untimed) and time
    # only the answer itself. Answering reschedules the card out of today's
    # queue, so each iteration answers a distinct card.
    answer_samples: list[float] = []
    warmups = 2
    answered = 0
    while answered < iters + warmups:
        card = sched.getCard()
        if card is None:
            break
        sample = common.timed(lambda c=card: sched.answerCard(c, 3))
        if answered >= warmups:
            answer_samples.append(sample)
        answered += 1
    results.add("answer card", answer_samples)

    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cards", type=int, default=50_000, help="deck size")
    parser.add_argument("--seed", type=int, default=12_345, help="generation seed")
    parser.add_argument(
        "--iters",
        type=int,
        default=200,
        help="samples for light/per-card actions",
    )
    parser.add_argument(
        "--heavy-iters",
        type=int,
        default=50,
        help="samples for whole-collection actions",
    )
    parser.add_argument(
        "--rebuild",
        action="store_true",
        help="regenerate the cached collection from scratch",
    )
    parser.add_argument(
        "--keep",
        action="store_true",
        help="keep the throwaway working collection after running",
    )
    args = parser.parse_args()

    print("=" * 78)
    print("Anki engine benchmark (PRD 7h)")
    print("=" * 78)

    gen_start = time.perf_counter()
    bench = common.load_collection(args.cards, args.seed, rebuild=args.rebuild)
    load_secs = time.perf_counter() - gen_start
    status = "generated" if bench.generated else "loaded from cache"
    print(
        f"collection {status} in {common.human_time(load_secs)} "
        f"({bench.path})\n"
    )

    try:
        results = run_benchmarks(bench, args.iters, args.heavy_iters)
    finally:
        bench.col.close(downgrade=False)
        if not args.keep and os.path.exists(bench.path):
            os.unlink(bench.path)

    print()
    results.print_table()
    print()
    print(
        "Notes: times are wall-clock per call on a warm queue. Re-runnable and "
        "deterministic given --cards/--seed."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
