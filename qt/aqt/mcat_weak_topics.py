# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""Desktop "Focus Weak Topics" read-out — the user-facing surface for the Rust
points-at-stake engine change. Ranks the currently selected deck's due review
topics weakest-first (weakness = 1 − average FSRS recall) using the shared
engine RPC `SchedulerService.GetPointsAtStakeQueue`. Read-only; computes off the
UI thread. Mirrors the iOS WeakTopicsView read-out."""

from __future__ import annotations

from aqt import mcat_ui as ui

TOPIC_PREFIX = "MCAT::"


def show(mw) -> None:  # type: ignore[no-untyped-def]
    from aqt.operations import QueryOp
    from aqt.utils import showWarning

    deck = mw.col.decks.current()["name"]

    def op(col):  # type: ignore[no-untyped-def]
        did = col.decks.id(deck)
        if did is not None:
            col.decks.set_current(did)
        return col._backend.get_points_at_stake_queue(
            topic_tag_prefix=TOPIC_PREFIX, weight_by_topic_size=False
        )

    QueryOp(
        parent=mw,
        op=op,
        success=lambda queue: ui.show_html(mw, "Focus Weak Topics", _render(deck, queue)),
    ).with_progress("Ranking…").failure(
        lambda e: showWarning(f"Could not rank weak topics: {e}", parent=mw)
    ).run_in_background()


def _weakness_color(weakness: float) -> str:
    if weakness >= 0.40:
        return ui.BAD
    if weakness >= 0.15:
        return ui.WARN
    return ui.GOOD


def _render(deck: str, queue) -> str:  # type: ignore[no-untyped-def]
    head = f"<h2>Focus Weak Topics — {ui.esc(deck)}</h2>"
    topics = sorted(queue.topics, key=lambda t: t.weakness, reverse=True)
    if not topics:
        return (
            head
            + "<p>No due, MCAT-tagged review cards in this deck yet. Points-at-stake "
            "ranks topics by FSRS recall on cards that are due for review — once this "
            "deck has due, <code>MCAT::</code>-tagged review cards, their weakest "
            "topics rank here.</p>"
        )

    total = sum(t.card_count for t in topics)
    parts = [
        head,
        f"<p style='color:{ui.MUTED}'>Your due review cards, ranked weakest-topic-first "
        "by the engine’s points-at-stake score. Weakness = 1 − average FSRS recall, "
        f"computed on-device. {total} due cards across {len(topics)} topics.</p>",
    ]
    for i, t in enumerate(topics, 1):
        weakness = float(t.weakness)
        color = _weakness_color(weakness)
        if t.HasField("mean_retrievability"):
            retr = f"{t.mean_retrievability * 100:.0f}% avg recall"
        else:
            retr = "no memory state"
        noun = "card" if t.card_count == 1 else "cards"
        parts.append(
            f"<h3>{i}. {ui.esc(t.topic)} "
            f"<span style='color:{color}'>{weakness * 100:.0f}% weak</span></h3>"
        )
        parts.append(ui.bar(weakness, color))
        parts.append(
            f"<p style='color:{ui.MUTED}'>{t.card_count} due {noun} &middot; {retr}</p>"
        )
    parts.append(
        f"<p style='color:{ui.MUTED}'>Study these top-down: select the deck and review "
        "as normal — the engine surfaces the weakest topics first.</p>"
    )
    return "".join(parts)
