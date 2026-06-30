# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""Shared helpers for the engine benchmark (``tools/bench.py``) and the
crash-resilience test (``tools/crash_test.py``).

The central job here is to *deterministically* build a large Anki collection so
the benchmarks are re-runnable: given the same ``--cards``/``--seed`` you always
get byte-identical content (same notes, tags and FSRS memory state). Generated
collections are cached under ``out/bench/`` so repeated runs are fast.

Run via the desktop Python env, e.g.::

    PYTHONPATH=out/pylib out/pyenv/bin/python tools/bench.py

This module only uses the public ``anki`` pylib API plus a single batched SQL
write to stamp FSRS memory state onto cards (the same JSON shape the public
``Card`` API produces, just written 50k rows at a time instead of one).
"""

from __future__ import annotations

import json
import os
import random
import shutil
import time
from dataclasses import dataclass

from anki import deck_config_pb2
from anki.collection import Collection
from anki.decks import DeckId
from anki.utils import int_time

# Where generated/cached collections live (out/ is git-ignored & auto-managed).
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE_DIR = os.path.join(REPO_ROOT, "out", "bench")

DECK_NAME = "MCATBench"
TOPIC_TAG_PREFIX = "MCAT::"

# MCAT-style sections become the "topic" the points-at-stake query groups by
# (it keys on the first component under the prefix). Each section is given a
# distinct base memory stability so topics have visibly different weakness,
# which makes the points-at-stake ordering meaningful on the generated deck.
SECTIONS: list[tuple[str, float]] = [
    ("Biochemistry", 80.0),
    ("Biology", 40.0),
    ("GeneralChemistry", 20.0),
    ("OrganicChemistry", 8.0),
    ("Physics", 4.0),
    ("Psychology", 120.0),
    ("Sociology", 60.0),
    ("CARS", 2.0),
]

SUBTOPICS = ["Core", "Applied", "HighYield", "Review", "Practice"]

# A day in seconds; cards are stamped as "reviewed one day ago" so their FSRS
# retrievability (and therefore weakness) is driven purely by stability.
ONE_DAY = 86_400


@dataclass
class BenchCollection:
    """A loaded, review-ready benchmark collection."""

    col: Collection
    deck_id: DeckId
    path: str
    num_cards: int
    seed: int
    generated: bool  # True if freshly generated this call (vs. loaded from cache)


def _cache_path(num_cards: int, seed: int) -> str:
    return os.path.join(CACHE_DIR, f"bench_{num_cards}_{seed}.anki2")


def _stability_for(rng: random.Random, base: float) -> float:
    """A per-card stability jittered around its section base (rounded so the
    cached collection is byte-stable across runs)."""
    factor = rng.uniform(0.5, 1.5)
    return round(base * factor, 3)


def generate_collection(path: str, num_cards: int, seed: int) -> None:
    """Build a fresh collection of ``num_cards`` due review cards at ``path``.

    Every card is a review card due today with deterministic FSRS memory state
    and an ``MCAT::<Section>::<Subtopic>`` tag. Deterministic given ``seed``.
    """
    if os.path.exists(path):
        os.unlink(path)

    rng = random.Random(seed)
    col = Collection(path)
    try:
        notetype = col.models.by_name("Basic")
        assert notetype is not None, "Basic notetype missing"
        deck_id = col.decks.id(DECK_NAME)
        assert deck_id is not None

        # Stamp FSRS state with a fixed "now" so the cache is reproducible and
        # all cards share a one-day-ago last review.
        now = int_time()
        last_review = now - ONE_DAY

        nid_to_stability: dict[int, float] = {}
        for i in range(num_cards):
            section, base = SECTIONS[i % len(SECTIONS)]
            subtopic = SUBTOPICS[(i // len(SECTIONS)) % len(SUBTOPICS)]
            note = col.new_note(notetype)
            note["Front"] = f"{section} card {i}: what is concept #{i}?"
            note["Back"] = f"The answer to concept #{i} in {section}/{subtopic}."
            note.tags = [f"{TOPIC_TAG_PREFIX}{section}::{subtopic}"]
            col.add_note(note, deck_id)
            nid_to_stability[note.id] = _stability_for(rng, base)

        # Promote every card to a due review card with FSRS memory state in one
        # batched write. This is the same {"s","d","lrt"} JSON the public Card
        # API produces (verified against col.update_card), just written in bulk.
        card_rows = col.db.all("select id, nid from cards")
        updates = [
            (
                json.dumps(
                    {"s": nid_to_stability[nid], "d": 5.0, "lrt": last_review}
                ),
                cid,
            )
            for cid, nid in card_rows
        ]
        col.db.executemany(
            "update cards set type=2, queue=2, due=0, ivl=10, reps=1, "
            "data=? where id=?",
            updates,
        )
    finally:
        # close() flushes the backend's pending writes to disk.
        col.close(downgrade=False)


def prepare_for_review(col: Collection, deck_id: DeckId) -> None:
    """Select the bench deck and lift daily limits so the *whole* due queue is
    available (the default 200/day cap would otherwise hide most of the deck),
    and enable FSRS so the answer path and points-at-stake stay consistent.

    Does not reschedule (``fsrs_reschedule`` is left false), so cards keep their
    due-today dates.
    """
    col.decks.set_current(deck_id)

    update = col.decks.get_deck_configs_for_update(deck_id)
    config = update.all_config[0].config
    config.config.reviews_per_day = 1_000_000
    config.config.new_per_day = 1_000_000
    # Per-deck "today" override is what actually lifts the live queue cap.
    limits = deck_config_pb2.DeckConfigsForUpdate.CurrentDeck.Limits(
        review=1_000_000, new=1_000_000
    )
    request = deck_config_pb2.UpdateDeckConfigsRequest(
        target_deck_id=deck_id,
        configs=[config],
        mode=deck_config_pb2.UPDATE_DECK_CONFIGS_MODE_NORMAL,
        fsrs=True,
        apply_all_parent_limits=update.apply_all_parent_limits,
        new_cards_ignore_review_limit=update.new_cards_ignore_review_limit,
        limits=limits,
    )
    col.decks.update_deck_configs(request)


def load_collection(
    num_cards: int,
    seed: int,
    *,
    rebuild: bool = False,
    work_path: str | None = None,
    prepared: bool = True,
) -> BenchCollection:
    """Return a review-ready benchmark collection.

    A deterministic master collection is generated once and cached under
    ``out/bench/``. Each call copies the cache to ``work_path`` (a throwaway
    working file) and opens that, so benchmarks/crash runs never mutate the
    cached master.
    """
    os.makedirs(CACHE_DIR, exist_ok=True)
    master = _cache_path(num_cards, seed)

    generated = False
    if rebuild or not os.path.exists(master):
        generate_collection(master, num_cards, seed)
        generated = True

    if work_path is None:
        work_path = os.path.join(CACHE_DIR, f"work_{num_cards}_{seed}.anki2")
    if os.path.exists(work_path):
        os.unlink(work_path)
    shutil.copy(master, work_path)

    col = Collection(work_path)
    deck_id = col.decks.id(DECK_NAME)
    assert deck_id is not None, f"deck {DECK_NAME!r} missing from cached collection"
    if prepared:
        prepare_for_review(col, deck_id)
    return BenchCollection(
        col=col,
        deck_id=deck_id,
        path=work_path,
        num_cards=num_cards,
        seed=seed,
        generated=generated,
    )


def human_time(seconds: float) -> str:
    if seconds < 1:
        return f"{seconds * 1000:.0f}ms"
    return f"{seconds:.1f}s"


def timed(fn) -> float:
    """Run ``fn`` once and return elapsed milliseconds."""
    start = time.perf_counter()
    fn()
    return (time.perf_counter() - start) * 1000.0
