# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""Desktop MCAT readiness panel (Wednesday: honest memory score).

Shows a *memory* estimate derived from FSRS retrievability (via the shared Rust
engine's points-at-stake query), presented honestly: a point estimate with a
range, topic coverage, the evidence behind it, and a hard "give-up" rule that
refuses to show a score when there isn't enough data. This is deliberately only
the memory bridge - performance and readiness scores come later.

The scoring/give-up logic lives in `compute_readiness`, which is pure (no Qt, no
backend) so it can be unit-tested; run this file directly to execute its
self-check:

    out/pyenv/bin/python qt/aqt/mcat_readiness.py
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field

TOPIC_PREFIX = "MCAT::"

# The exam outline we measure coverage against (top-level MCAT subjects, keyed
# off the first component under the MCAT:: tag prefix). Coverage = how many of
# these the deck actually has cards for.
MCAT_SUBJECTS = [
    "Biochemistry",
    "Biology",
    "GeneralChemistry",
    "OrganicChemistry",
    "Physics",
    "Psychology",
    "Sociology",
    "CARS",
]

# Give-up rule (stated, per the honesty requirement): no score until the student
# has at least this many graded reviews AND this fraction of topic coverage.
MIN_GRADED_REVIEWS = 200
MIN_COVERAGE = 0.50


@dataclass
class TopicStat:
    """Per-topic aggregate, mirrored from the points-at-stake RPC."""

    topic: str
    card_count: int
    weakness: float  # 0..1  (1 - mean retrievability)
    mean_retrievability: float | None  # 0..1, or None if no memory state


@dataclass
class ReadinessResult:
    abstained: bool
    abstain_reason: str
    memory_score: float | None  # percent 0..100
    low: float | None
    high: float | None
    confidence: str
    coverage_pct: float  # 0..100
    covered: list[str]
    missing: list[str]
    graded_reviews: int
    min_reviews: int
    min_coverage_pct: float
    reasons: list[tuple[str, float]] = field(default_factory=list)  # (topic, weakness%)
    updated: str = ""


def compute_readiness(
    covered_subjects: set[str],
    topics: list[TopicStat],
    graded_reviews: int,
    *,
    outline: list[str] = MCAT_SUBJECTS,
    min_reviews: int = MIN_GRADED_REVIEWS,
    min_coverage: float = MIN_COVERAGE,
) -> ReadinessResult:
    """Pure scoring + give-up logic. No Qt, no backend."""
    covered = [s for s in outline if s in covered_subjects]
    missing = [s for s in outline if s not in covered_subjects]
    coverage = len(covered) / len(outline) if outline else 0.0

    # Weighted mean retrievability over topics that actually have memory state.
    weighted_sum = 0.0
    counted = 0
    weak: list[tuple[str, float]] = []
    for t in topics:
        if t.mean_retrievability is not None and t.card_count > 0:
            weighted_sum += t.mean_retrievability * t.card_count
            counted += t.card_count
        weak.append((t.topic, t.weakness * 100.0))
    reasons = sorted(weak, key=lambda x: x[1], reverse=True)[:3]
    updated = time.strftime("%Y-%m-%d %H:%M")

    # The give-up rule: refuse a score when data is insufficient.
    missing_data: list[str] = []
    if graded_reviews < min_reviews:
        missing_data.append(
            f"need at least {min_reviews} graded reviews (have {graded_reviews})"
        )
    if coverage < min_coverage:
        missing_data.append(
            f"need at least {int(min_coverage * 100)}% topic coverage "
            f"(have {coverage * 100:.0f}%)"
        )
    if missing_data:
        return ReadinessResult(
            abstained=True,
            abstain_reason="; ".join(missing_data),
            memory_score=None,
            low=None,
            high=None,
            confidence="none",
            coverage_pct=coverage * 100.0,
            covered=covered,
            missing=missing,
            graded_reviews=graded_reviews,
            min_reviews=min_reviews,
            min_coverage_pct=min_coverage * 100.0,
            reasons=reasons,
            updated=updated,
        )

    p = weighted_sum / counted if counted else 0.0  # 0..1
    # Honest range: normal-approx interval on the mean recall, so more data
    # (larger n) narrows the range.
    margin = 1.96 * math.sqrt(max(p * (1.0 - p), 1e-9) / counted) if counted else 0.0
    memory = p * 100.0
    low = max(0.0, (p - margin) * 100.0)
    high = min(100.0, (p + margin) * 100.0)

    if coverage >= 0.8 and counted >= 1000:
        confidence = "high"
    elif coverage >= 0.6 and counted >= 500:
        confidence = "medium"
    else:
        confidence = "low"

    return ReadinessResult(
        abstained=False,
        abstain_reason="",
        memory_score=memory,
        low=low,
        high=high,
        confidence=confidence,
        coverage_pct=coverage * 100.0,
        covered=covered,
        missing=missing,
        graded_reviews=graded_reviews,
        min_reviews=min_reviews,
        min_coverage_pct=min_coverage * 100.0,
        reasons=reasons,
        updated=updated,
    )


def gather(col) -> ReadinessResult:  # type: ignore[no-untyped-def]
    """Collect inputs from a live collection and compute the result."""
    queue = col._backend.get_points_at_stake_queue(
        topic_tag_prefix=TOPIC_PREFIX, weight_by_topic_size=False
    )
    topics = [
        TopicStat(
            topic=t.topic,
            card_count=int(t.card_count),
            weakness=float(t.weakness),
            mean_retrievability=(
                float(t.mean_retrievability)
                if t.HasField("mean_retrievability")
                else None
            ),
        )
        for t in queue.topics
    ]

    covered: set[str] = set()
    for tag in col.tags.all():
        if tag.startswith(TOPIC_PREFIX):
            parts = tag[len(TOPIC_PREFIX) :].split("::")
            if parts and parts[0]:
                covered.add(parts[0])

    graded_reviews = col.db.scalar("select count() from revlog") or 0
    return compute_readiness(covered, topics, int(graded_reviews))


def _render_html(r: ReadinessResult) -> str:
    rule = (
        f"Give-up rule: no score until at least {r.min_reviews} graded reviews "
        f"and {int(r.min_coverage_pct)}% topic coverage."
    )
    cov = (
        f"Topic coverage: <b>{r.coverage_pct:.0f}%</b> "
        f"({len(r.covered)} of {len(r.covered) + len(r.missing)} MCAT subjects)"
    )
    if r.missing:
        cov += f"<br><span style='color:#888'>Missing: {', '.join(r.missing)}</span>"

    if r.abstained:
        body = (
            "<h2>Not enough data yet</h2>"
            f"<p>The app is withholding a score because it {r.abstain_reason}.</p>"
            f"<p>{cov}</p>"
            f"<p>Graded reviews so far: <b>{r.graded_reviews}</b></p>"
            f"<p style='color:#888'>{rule}</p>"
        )
        return body

    reasons = "".join(f"<li>{t} ({w:.0f}% weak)</li>" for t, w in r.reasons)
    return (
        "<h2>Memory estimate</h2>"
        f"<p style='font-size:15px'>Memory: <b>{r.memory_score:.0f}%</b> "
        f"(likely {r.low:.0f}-{r.high:.0f}%)</p>"
        f"<p>Confidence: <b>{r.confidence}</b></p>"
        f"<p>{cov}</p>"
        f"<p>Based on <b>{r.graded_reviews}</b> graded reviews.</p>"
        f"<p>Weakest topics (study these first):<ul>{reasons}</ul></p>"
        f"<p style='color:#888'>Updated {r.updated}. This is a memory estimate "
        "from FSRS retrievability, not a predicted exam score - performance and "
        "readiness come later.</p>"
        f"<p style='color:#888'>{rule}</p>"
    )


def show(mw) -> None:  # type: ignore[no-untyped-def]
    """Open the readiness dialog for the given main window."""
    from aqt.qt import (
        QDialog,
        QDialogButtonBox,
        QLabel,
        Qt,
        QVBoxLayout,
        qconnect,
    )
    from aqt.utils import disable_help_button

    try:
        result = gather(mw.col)
    except Exception as err:  # pragma: no cover - surface backend errors
        from aqt.utils import showWarning

        showWarning(f"Could not compute readiness: {err}", parent=mw)
        return

    dialog = QDialog(mw)
    dialog.setWindowTitle("MCAT Readiness")
    disable_help_button(dialog)
    mw.garbage_collect_on_dialog_finish(dialog)

    layout = QVBoxLayout(dialog)
    label = QLabel(_render_html(result))
    label.setTextFormat(Qt.TextFormat.RichText)
    label.setWordWrap(True)
    layout.addWidget(label)

    buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
    qconnect(buttons.rejected, dialog.reject)
    qconnect(buttons.accepted, dialog.accept)
    layout.addWidget(buttons)

    dialog.resize(460, 420)
    dialog.exec()


def _self_test() -> None:
    """Runnable check for the pure scoring + give-up logic."""
    strong = TopicStat("Biochemistry", 100, 0.10, 0.90)
    weak = TopicStat("Physics", 100, 0.40, 0.60)

    # Below both thresholds -> abstain.
    r = compute_readiness({"Biochemistry", "Physics"}, [strong, weak], graded_reviews=10)
    assert r.abstained, "should abstain with too few reviews"
    assert "graded reviews" in r.abstain_reason

    # Enough reviews but coverage < 50% -> abstain.
    r = compute_readiness({"Biochemistry"}, [strong], graded_reviews=500)
    assert r.abstained, "should abstain with low coverage"
    assert "coverage" in r.abstain_reason

    # Enough reviews AND coverage -> real score with a range.
    full = {s for s in MCAT_SUBJECTS}
    r = compute_readiness(full, [strong, weak], graded_reviews=500)
    assert not r.abstained, f"should show a score, got {r.abstain_reason}"
    assert r.memory_score is not None and 70.0 < r.memory_score < 80.0, r.memory_score
    assert r.low < r.memory_score < r.high, (r.low, r.memory_score, r.high)
    assert r.coverage_pct == 100.0
    assert r.reasons and r.reasons[0][0] == "Physics"  # weakest first
    print("mcat_readiness self-test: OK")
    print(
        f"  sample -> memory {r.memory_score:.0f}% "
        f"({r.low:.0f}-{r.high:.0f}%), coverage {r.coverage_pct:.0f}%, "
        f"confidence {r.confidence}"
    )


if __name__ == "__main__":
    _self_test()
