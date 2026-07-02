# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""Tests for the points-at-stake review ordering (PRD 7a).

These exercise the new ``get_points_at_stake_queue`` backend RPC end-to-end
through the Python bindings, and confirm the read-only ordering does not break
undo.
"""

from anki import cards_pb2
from anki.consts import CARD_TYPE_LRN, CARD_TYPE_NEW, CARD_TYPE_REV, QUEUE_TYPE_REV
from anki.utils import int_time
from tests.shared import getEmptyCol


def _add_due_review_card(col, tags, stability):
    """Add a review card due today with a controllable FSRS memory state.

    last_review_time is fixed at one day ago so retrievability (and therefore
    weakness) is driven purely by stability: lower stability -> lower
    retrievability -> higher weakness.
    """
    note = col.newNote()
    note["Front"] = f"{'-'.join(tags) or 'untagged'}-{stability}"
    note.tags = list(tags)
    col.addNote(note)

    card = note.cards()[0]
    card.type = CARD_TYPE_REV
    card.queue = QUEUE_TYPE_REV
    card.due = 0
    card.ivl = 10
    card.reps = 1
    card.memory_state = cards_pb2.FsrsMemoryState(stability=stability, difficulty=5.0)
    card.last_review_time = int_time() - 86_400
    card.flush()
    return card.id


def test_points_at_stake_orders_weak_topics_first():
    col = getEmptyCol()
    # Strong topic: high stability -> high retrievability -> low weakness.
    strong = _add_due_review_card(col, ["MCAT::Biochemistry"], 10_000.0)
    # Weak topic: low stability -> low retrievability -> high weakness.
    weak = _add_due_review_card(col, ["MCAT::Physics"], 1.0)

    out = col._backend.get_points_at_stake_queue(
        topic_tag_prefix="MCAT::", weight_by_topic_size=False
    )

    order = [c.card_id for c in out.cards]
    assert order == [weak, strong], "the weaker topic must be surfaced first"

    by_id = {c.card_id: c for c in out.cards}
    assert by_id[weak].topic == "Physics"
    assert by_id[strong].topic == "Biochemistry"
    assert by_id[weak].points_at_stake > by_id[strong].points_at_stake
    assert by_id[weak].weakness > by_id[strong].weakness

    assert {t.topic for t in out.topics} == {"Physics", "Biochemistry"}


def test_points_at_stake_buckets_untagged_cards():
    col = getEmptyCol()
    tagged = _add_due_review_card(col, ["MCAT::Anatomy"], 2.0)
    # A tag that exists but is not under the configured prefix.
    untagged = _add_due_review_card(col, ["misc::foo"], 2.0)

    out = col._backend.get_points_at_stake_queue(
        topic_tag_prefix="MCAT::", weight_by_topic_size=False
    )

    by_id = {c.card_id: c for c in out.cards}
    assert by_id[tagged].topic == "Anatomy"
    assert by_id[untagged].topic == "untagged"


def test_points_at_stake_does_not_break_undo():
    col = getEmptyCol()
    note = col.newNote()
    note["Front"] = "undo-me"
    col.addNote(note)
    cid = note.cards()[0].id
    assert col.get_card(cid).type == CARD_TYPE_NEW

    # Answer the new card (Again) -> learning, which creates an undo point.
    card = col.sched.getCard()
    col.sched.answerCard(card, 1)
    assert col.get_card(cid).type == CARD_TYPE_LRN

    # A read-only points-at-stake call between answering and undo must not
    # corrupt data or block the undo.
    col._backend.get_points_at_stake_queue(
        topic_tag_prefix="MCAT::", weight_by_topic_size=False
    )

    col.undo()
    assert col.get_card(cid).type == CARD_TYPE_NEW, (
        "undo must restore the card after a points-at-stake call"
    )
