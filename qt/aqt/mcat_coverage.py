# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""Desktop MCAT Coverage map — per-section / per-topic coverage of the AAMC
outline for the currently selected deck, with an abstain banner below the
give-up line. Mirrors the iOS CoverageView; all logic is the shared, tested
`mcat_models`. Computes off the UI thread."""

from __future__ import annotations

from aqt import mcat_models as m
from aqt import mcat_ui as ui


def show(mw) -> None:  # type: ignore[no-untyped-def]
    from aqt.operations import QueryOp
    from aqt.utils import showWarning

    deck = mw.col.decks.current()["name"]

    QueryOp(
        parent=mw,
        op=lambda col: m.gather_coverage(col, deck),
        success=lambda rep: ui.show_html(mw, "MCAT Coverage", _render(deck, rep)),
    ).with_progress("Mapping coverage…").failure(
        lambda e: showWarning(f"Could not load coverage: {e}", parent=mw)
    ).run_in_background()


def _color(fraction: float) -> str:
    if fraction >= m.SCORING_THRESHOLD:
        return ui.GOOD
    if fraction >= 0.30:
        return ui.WARN
    return ui.BAD


def _render(deck: str, cov: "m.CoverageReport") -> str:
    parts = [
        f"<h2>MCAT Coverage — {ui.esc(deck)}</h2>",
        f"<p style='color:{ui.MUTED}'>A topic counts as covered once it has at least one "
        "card. Coverage is one input to the scores — not a score itself.</p>",
    ]
    if not cov.meets_coverage_threshold:
        thr = int(m.COVERAGE_THRESHOLD * 100)
        parts.append(
            f"<p style='color:{ui.BAD}'><b>Not enough coverage to score yet — "
            f"{cov.percent_covered}% of MCAT topics studied.</b> "
            f"Scores stay hidden until at least {thr}%.</p>"
        )

    c = _color(cov.fraction_covered)
    parts.append(
        f"<h3>Overall: <span style='color:{c}'>{cov.percent_covered}%</span> "
        f"<span style='color:{ui.MUTED}'>({cov.covered_topics} of {cov.total_topics} topics)</span></h3>"
    )
    parts.append(ui.bar(cov.fraction_covered, c))

    for s in cov.sections:
        sc = _color(s.fraction_covered)
        parts.append(
            f"<h3>{ui.esc(s.section)} "
            f"<span style='color:{sc}'>{s.covered_count}/{s.total_count} &middot; {s.percent_covered}%</span></h3>"
        )
        full = m.FULL_NAMES.get(s.section)
        if full:
            parts.append(f"<p style='color:{ui.MUTED}'>{ui.esc(full)}</p>")
        parts.append(ui.bar(s.fraction_covered, sc))
        rows = []
        for t in s.topics:
            icon = "\u2713" if t.is_covered else "\u25cb"
            color = ui.GOOD if t.is_covered else ui.MUTED
            if t.is_covered:
                trailing = f"{t.card_count} card" + ("" if t.card_count == 1 else "s")
            else:
                trailing = "missing"
            rows.append(
                f"<tr><td style='color:{color}'>{icon} {ui.esc(t.name)}</td>"
                f"<td align='right' style='color:{ui.MUTED}'>{trailing}</td></tr>"
            )
        parts.append(f"<table width='100%' cellspacing='2'>{''.join(rows)}</table>")

    parts.append(
        f"<p style='color:{ui.MUTED}'>Topics are a representative subset of the AAMC "
        "content outline (~11–14 per section), not the full list. Percentages are of those topics.</p>"
    )
    return "".join(parts)
