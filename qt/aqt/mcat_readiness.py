# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""Desktop MCAT Readiness dashboard — the three honest scores.

Shows Memory / Performance / Readiness for the currently selected deck, each as
a range with the full honesty read-out; or, below the give-up line, NO scores —
just what's missing and the best next thing. Mirrors the iOS
ReadinessDashboardView. All scoring math lives in the shared, unit-tested
`mcat_models`; this file only gathers (off the UI thread) and renders.
"""

from __future__ import annotations

from aqt import mcat_models as m
from aqt import mcat_ui as ui


def show(mw) -> None:  # type: ignore[no-untyped-def]
    from aqt.operations import QueryOp
    from aqt.utils import showWarning

    deck = mw.col.decks.current()["name"]

    QueryOp(
        parent=mw,
        op=lambda col: m.assess(col, deck),
        success=lambda a: ui.show_html(mw, "MCAT Readiness", _render(deck, a)),
    ).with_progress("Scoring…").failure(
        lambda e: showWarning(f"Could not compute readiness: {e}", parent=mw)
    ).run_in_background()


def _render(deck: str, a: "m.ReadinessAssessment") -> str:
    head = (
        f"<h2>Readiness — {ui.esc(deck)}</h2>"
        f"<p style='color:{ui.MUTED}'>Three separate scores, each a range. {ui.esc(m.GIVE_UP_RULE)}</p>"
    )
    body = _scored(a) if a.is_scored else _abstain(a)
    return head + body + _coverage_summary(a.coverage) + _best_and_readout(a)


def _scored(a: "m.ReadinessAssessment") -> str:
    mem, perf, rdy = a.memory, a.performance, a.readiness
    assert mem and perf and rdy
    return (
        "<h3>Your three scores</h3>"
        f"<p><b style='color:{ui.GOOD}'>Memory {mem.percent_range_text}</b> "
        f"(≈{mem.percent_point}%) &middot; real FSRS recall &middot; confidence: {a.memory_confidence}"
        f"{ui.bar(mem.point, ui.GOOD)}</p>"
        f"<p><b style='color:{ui.WARN}'>Performance {perf.percent_range_text}</b> "
        f"(≈{perf.percent_point}%) &middot; provisional"
        f"{ui.bar(perf.point, ui.WARN)}</p>"
        f"<p><b style='color:{ui.ACCENT}'>Readiness {rdy.range_text}</b> "
        f"(≈{rdy.point}) &middot; MCAT 472–528 &middot; provisional"
        f"{ui.bar((rdy.point - m.SCALE_MIN) / (m.SCALE_MAX - m.SCALE_MIN), ui.ACCENT)}</p>"
        f"<p style='color:{ui.MUTED}'>{ui.esc(m.PROVISIONAL_LABEL)}</p>"
    )


def _abstain(a: "m.ReadinessAssessment") -> str:
    reviews_frac = a.graded_reviews / m.GRADED_REVIEW_THRESHOLD
    cov_frac = a.coverage.fraction_covered / m.COVERAGE_THRESHOLD
    rev_color = ui.GOOD if a.meets_graded_review_threshold else ui.BAD
    cov_color = ui.GOOD if a.meets_coverage_threshold else ui.BAD
    return (
        "<h3>No scores yet — not enough data</h3>"
        "<h4>Progress to scoring</h4>"
        f"<p>Graded reviews: <b>{a.graded_reviews}</b> / {m.GRADED_REVIEW_THRESHOLD}"
        f"{ui.bar(reviews_frac, rev_color)}</p>"
        f"<p>Topic coverage: <b>{a.coverage.percent_covered}%</b> / {int(m.COVERAGE_THRESHOLD * 100)}%"
        f"{ui.bar(cov_frac, cov_color)}</p>"
    )


def _best_and_readout(a: "m.ReadinessAssessment") -> str:
    reasons = "".join(f"<li>{ui.esc(r)}</li>" for r in a.reasons)
    missing = "".join(f"<li>{ui.esc(mi)}</li>" for mi in a.missing_data)
    return (
        f"<h3>Best next thing to study</h3><p><b>{ui.esc(a.best_next_thing)}</b></p>"
        + (f"<h3>Why these numbers</h3><ul>{reasons}</ul>" if a.is_scored and reasons else "")
        + f"<h3>What’s missing</h3><ul>{missing}</ul>"
        + "<h3>Evidence</h3>"
        + f"<p style='color:{ui.MUTED}'>"
        + f"% of exam covered: {a.coverage.percent_covered}%<br>"
        + f"Graded reviews: {a.graded_reviews}<br>"
        + f"Studied cards: {a.studied_card_count} "
        + f"({a.cards_with_memory_state} with FSRS memory state)<br>"
        + f"Confidence: {a.memory_confidence}<br>"
        + f"Updated: {ui.esc(a.updated)}</p>"
    )


def _coverage_summary(cov: "m.CoverageReport") -> str:
    color = ui.GOOD if cov.fraction_covered >= m.SCORING_THRESHOLD else (
        ui.WARN if cov.fraction_covered >= 0.30 else ui.BAD
    )
    rows = "".join(
        f"<tr><td style='color:{ui.MUTED}'>{ui.esc(s.section)}</td>"
        f"<td align='right'>{s.covered_count}/{s.total_count} &middot; {s.percent_covered}%</td></tr>"
        for s in cov.sections
    )
    return (
        f"<h3>Exam coverage: <span style='color:{color}'>{cov.percent_covered}%</span></h3>"
        f"{ui.bar(cov.fraction_covered, color)}"
        f"<table width='60%' cellspacing='2'>{rows}</table>"
    )
