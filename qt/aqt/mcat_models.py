# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""MCAT coverage + three-score model for the desktop dashboards.

A faithful Python port of the iOS engine-bridge logic (AnkiKit's
`MCATOutline`, `BackendCoverage`, and `BackendReadiness`) so the desktop
readiness dashboard, coverage map, and weak-topics read-out compute the same
numbers as the phone. Pure logic + collection reads only; no Qt here, so the
scoring is unit-testable (run this file directly for a self-check).

Honesty rules, mirrored from iOS:
- Memory is the mean of the engine's own FSRS retrievability across studied
  cards (a 95% interval), never invented.
- Performance/Readiness are provisional (uncalibrated) and always a range.
- Below the give-up line (<200 graded reviews OR <50% coverage) NO scores are
  shown — only what's missing and the best next thing.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field

TAG_ROOT = "MCAT"
TOPIC_PREFIX = "MCAT::"

# The MCAT content-outline taxonomy: (section_token, display_name, full_name,
# [(topic_token, topic_display_name)]). A representative subset of the AAMC
# outline — 50 topics across the four sections — kept in exact sync with the iOS
# MCATOutline so both platforms measure the same thing.
OUTLINE: list[tuple[str, str, str, list[tuple[str, str]]]] = [
    (
        "ChemPhys", "Chem/Phys",
        "Chemical & Physical Foundations of Biological Systems",
        [
            ("Kinematics", "Kinematics & translational motion"),
            ("ForceAndEnergy", "Force, work & energy"),
            ("Fluids", "Fluids & hydrostatics"),
            ("Thermodynamics", "Thermodynamics"),
            ("ElectrostaticsAndCircuits", "Electrostatics & circuits"),
            ("Optics", "Light & optics"),
            ("SoundAndWaves", "Sound & waves"),
            ("AtomicAndNuclear", "Atomic & nuclear phenomena"),
            ("AcidsAndBases", "Acids & bases"),
            ("Solutions", "Solutions & solubility"),
            ("ReactionKinetics", "Reaction kinetics"),
            ("Electrochemistry", "Electrochemistry"),
        ],
    ),
    (
        "CARS", "CARS", "Critical Analysis & Reasoning Skills",
        [
            ("FoundationsOfComprehension", "Foundations of comprehension"),
            ("ReasoningWithinText", "Reasoning within the text"),
            ("ReasoningBeyondText", "Reasoning beyond the text"),
            ("Philosophy", "Humanities: philosophy"),
            ("Ethics", "Humanities: ethics"),
            ("Literature", "Humanities: literature"),
            ("ArtsAndCulture", "Humanities: arts & culture"),
            ("History", "Humanities: history"),
            ("Anthropology", "Social sciences: anthropology"),
            ("Economics", "Social sciences: economics"),
            ("PoliticalScience", "Social sciences: political science"),
        ],
    ),
    (
        "BioBiochem", "Bio/Biochem",
        "Biological & Biochemical Foundations of Living Systems",
        [
            ("AminoAcidsAndProteins", "Amino acids & proteins"),
            ("Enzymes", "Enzyme structure & kinetics"),
            ("Carbohydrates", "Carbohydrates"),
            ("LipidsAndMembranes", "Lipids & membranes"),
            ("NucleicAcids", "Nucleic acids"),
            ("DNAReplication", "DNA replication & repair"),
            ("GeneExpression", "Transcription & translation"),
            ("Genetics", "Genetics & inheritance"),
            ("Bioenergetics", "Bioenergetics"),
            ("CarbohydrateMetabolism", "Carbohydrate metabolism"),
            ("CellBiology", "Cell biology & organelles"),
            ("Microbiology", "Microbiology: prokaryotes & viruses"),
            ("NervousAndEndocrine", "Nervous & endocrine systems"),
            ("OrganSystems", "Organ systems physiology"),
        ],
    ),
    (
        "PsychSoc", "Psych/Soc",
        "Psychological, Social & Biological Foundations of Behavior",
        [
            ("SensationAndPerception", "Sensation & perception"),
            ("LearningAndConditioning", "Learning & conditioning"),
            ("MemoryAndCognition", "Memory & cognition"),
            ("Consciousness", "Consciousness & states"),
            ("MotivationAndEmotion", "Motivation & emotion"),
            ("Personality", "Personality"),
            ("PsychologicalDisorders", "Psychological disorders"),
            ("AttitudesAndBehaviorChange", "Attitudes & behavior change"),
            ("SocialInfluence", "Social influence & behavior"),
            ("SelfIdentity", "Self-identity"),
            ("SocialCognition", "Social cognition & attribution"),
            ("SocialStructure", "Social structure & institutions"),
            ("SocialStratification", "Social stratification & inequality"),
        ],
    ),
]

# section token (as points-at-stake reports it) -> short display name
SECTION_NAMES = {token: name for token, name, _full, _topics in OUTLINE}
FULL_NAMES = {name: full for _token, name, full, _topics in OUTLINE}


@dataclass
class CoverageTopic:
    section: str  # short display label, e.g. "Chem/Phys"
    name: str
    tag: str
    card_count: int

    @property
    def is_covered(self) -> bool:
        return self.card_count > 0


@dataclass
class SectionCoverage:
    section: str
    topics: list[CoverageTopic]

    @property
    def covered_count(self) -> int:
        return sum(1 for t in self.topics if t.is_covered)

    @property
    def total_count(self) -> int:
        return len(self.topics)

    @property
    def fraction_covered(self) -> float:
        return self.covered_count / self.total_count if self.total_count else 0.0

    @property
    def percent_covered(self) -> int:
        return round(self.fraction_covered * 100)


SCORING_THRESHOLD = 0.5


@dataclass
class CoverageReport:
    sections: list[SectionCoverage]

    @property
    def covered_topics(self) -> int:
        return sum(s.covered_count for s in self.sections)

    @property
    def total_topics(self) -> int:
        return sum(s.total_count for s in self.sections)

    @property
    def fraction_covered(self) -> float:
        return self.covered_topics / self.total_topics if self.total_topics else 0.0

    @property
    def percent_covered(self) -> int:
        return round(self.fraction_covered * 100)

    @property
    def meets_coverage_threshold(self) -> bool:
        return self.fraction_covered >= SCORING_THRESHOLD

    @property
    def most_impactful_missing_topic(self) -> CoverageTopic | None:
        candidates = [s for s in self.sections if s.covered_count < s.total_count]
        if not candidates:
            return None
        # least-covered section; ties -> most missing topics.
        target = min(
            candidates,
            key=lambda s: (s.fraction_covered, -(s.total_count - s.covered_count)),
        )
        return next((t for t in target.topics if not t.is_covered), None)


# --- Score model (pure math, mirrors iOS ScoreModel) ------------------------

GRADED_REVIEW_THRESHOLD = 200
COVERAGE_THRESHOLD = SCORING_THRESHOLD
GIVE_UP_RULE = (
    f"No score until \u2265{GRADED_REVIEW_THRESHOLD} graded reviews AND "
    f"\u2265{int(COVERAGE_THRESHOLD * 100)}% topic coverage."
)
PROVISIONAL_LABEL = (
    "Provisional \u2014 not yet calibrated against held-out exam-style questions."
)
_MEMORY_CI_Z = 1.96
_GUESS_BASELINE = 0.25
_TRANSFER_LOW, _TRANSFER_MID, _TRANSFER_HIGH = 0.50, 0.675, 0.85
SCALE_MIN, SCALE_MAX = 472, 528


def _clamp01(v: float) -> float:
    return min(1.0, max(0.0, v))


@dataclass
class ScoreRange:
    low: float
    point: float
    high: float

    @staticmethod
    def make(low: float, point: float, high: float) -> "ScoreRange":
        p = _clamp01(point)
        return ScoreRange(low=min(p, _clamp01(low)), point=p, high=max(p, _clamp01(high)))

    @property
    def percent_point(self) -> int:
        return round(self.point * 100)

    @property
    def percent_low(self) -> int:
        return round(self.low * 100)

    @property
    def percent_high(self) -> int:
        return round(self.high * 100)

    @property
    def percent_range_text(self) -> str:
        return f"{self.percent_low}\u2013{self.percent_high}%"


@dataclass
class ReadinessProjection:
    low: int
    point: int
    high: int

    @staticmethod
    def make(low: int, point: int, high: int) -> "ReadinessProjection":
        def clamp(v: int) -> int:
            return min(SCALE_MAX, max(SCALE_MIN, v))
        p = clamp(point)
        return ReadinessProjection(low=min(p, clamp(low)), point=p, high=max(p, clamp(high)))

    @property
    def range_text(self) -> str:
        return f"{self.low}\u2013{self.high}"


def memory_score(retrievabilities: list[float]) -> ScoreRange | None:
    if not retrievabilities:
        return None
    n = len(retrievabilities)
    mean = sum(retrievabilities) / n
    if n < 2:
        return ScoreRange.make(mean, mean, mean)
    variance = sum((r - mean) ** 2 for r in retrievabilities) / (n - 1)
    standard_error = math.sqrt(variance / n)
    margin = _MEMORY_CI_Z * standard_error
    return ScoreRange.make(mean - margin, mean, mean + margin)


def performance_score(memory: ScoreRange, coverage_fraction: float) -> ScoreRange:
    c = _clamp01(coverage_fraction)

    def blend(mem: float, transfer: float) -> float:
        return c * (mem * transfer) + (1 - c) * _GUESS_BASELINE

    return ScoreRange.make(
        blend(memory.low, _TRANSFER_LOW),
        blend(memory.point, _TRANSFER_MID),
        blend(memory.high, _TRANSFER_HIGH),
    )


def _scaled_score(p: float) -> int:
    return round(SCALE_MIN + _clamp01(p) * (SCALE_MAX - SCALE_MIN))


def readiness_projection(performance: ScoreRange) -> ReadinessProjection:
    return ReadinessProjection.make(
        _scaled_score(performance.low),
        _scaled_score(performance.point),
        _scaled_score(performance.high),
    )


def memory_confidence(card_count: int) -> str:
    if card_count >= 80:
        return "high"
    if card_count >= 25:
        return "moderate"
    return "low"


@dataclass
class ReadinessAssessment:
    coverage: CoverageReport
    graded_reviews: int
    studied_card_count: int
    cards_with_memory_state: int
    updated: str
    memory: ScoreRange | None
    performance: ScoreRange | None
    readiness: ReadinessProjection | None
    memory_confidence: str
    reasons: list[str] = field(default_factory=list)
    missing_data: list[str] = field(default_factory=list)
    best_next_thing: str = ""

    @property
    def meets_graded_review_threshold(self) -> bool:
        return self.graded_reviews >= GRADED_REVIEW_THRESHOLD

    @property
    def meets_coverage_threshold(self) -> bool:
        return self.coverage.meets_coverage_threshold

    @property
    def is_scored(self) -> bool:
        return (
            self.meets_graded_review_threshold
            and self.meets_coverage_threshold
            and self.memory is not None
        )


def make_assessment(
    coverage: CoverageReport,
    retrievabilities: list[float],
    graded_reviews: int,
    studied_card_count: int,
    weakest_studied_topic: str | None = None,
) -> ReadinessAssessment:
    cards_with_memory = len(retrievabilities)
    meets_reviews = graded_reviews >= GRADED_REVIEW_THRESHOLD
    meets_coverage = coverage.meets_coverage_threshold
    computed_memory = memory_score(retrievabilities)
    gated = meets_reviews and meets_coverage and computed_memory is not None

    memory = performance = readiness = None
    if gated and computed_memory is not None:
        perf = performance_score(computed_memory, coverage.fraction_covered)
        memory, performance, readiness = computed_memory, perf, readiness_projection(perf)

    reasons: list[str] = []
    if memory is not None:
        noun = "card" if cards_with_memory == 1 else "cards"
        reasons.append(
            f"Memory is the mean real FSRS retrievability of {cards_with_memory} studied "
            f"{noun} ({memory.percent_point}%, 95% interval {memory.percent_range_text})."
        )
        reasons.append(
            "Performance discounts Memory by the recall\u2192application gap and the "
            "un-covered share; Readiness maps that onto the 472\u2013528 scale."
        )
    reasons.append(
        f"Coverage is {coverage.percent_covered}% of {coverage.total_topics} outline topics "
        f"({coverage.covered_topics} with at least one card)."
    )
    reasons.append(
        f"{graded_reviews} graded {'review' if graded_reviews == 1 else 'reviews'} across "
        f"{studied_card_count} studied {'card' if studied_card_count == 1 else 'cards'}."
    )

    missing: list[str] = []
    if not meets_reviews:
        missing.append(
            f"Need \u2265{GRADED_REVIEW_THRESHOLD} graded reviews \u2014 have {graded_reviews}."
        )
    if not meets_coverage:
        missing.append(
            f"Need \u2265{int(COVERAGE_THRESHOLD * 100)}% topic coverage \u2014 "
            f"have {coverage.percent_covered}%."
        )
    if memory is None and (meets_reviews or meets_coverage):
        missing.append("No studied card carries FSRS memory state yet, so Memory can't be computed.")
    without_memory = studied_card_count - cards_with_memory
    if without_memory > 0:
        verb = "card has" if without_memory == 1 else "cards have"
        missing.append(f"{without_memory} studied {verb} no FSRS memory state and are excluded from Memory.")
    missing.append("Performance is not yet calibrated against held-out exam-style questions.")
    missing.append("Past-prediction accuracy: not enough history yet.")

    best_next = _best_next_thing(
        coverage, gated, meets_reviews, meets_coverage, graded_reviews, weakest_studied_topic
    )

    return ReadinessAssessment(
        coverage=coverage,
        graded_reviews=graded_reviews,
        studied_card_count=studied_card_count,
        cards_with_memory_state=cards_with_memory,
        updated=time.strftime("%Y-%m-%d %H:%M"),
        memory=memory,
        performance=performance,
        readiness=readiness,
        memory_confidence=memory_confidence(cards_with_memory),
        reasons=reasons,
        missing_data=missing,
        best_next_thing=best_next,
    )


def _best_next_thing(
    coverage: CoverageReport, gated: bool, meets_reviews: bool, meets_coverage: bool,
    graded_reviews: int, weakest_studied_topic: str | None,
) -> str:
    if not meets_coverage:
        missing = coverage.most_impactful_missing_topic
        if missing is not None:
            return f"Add cards for \u201c{missing.name}\u201d ({missing.section}) \u2014 the biggest coverage gap."
    if not meets_reviews:
        remaining = max(0, GRADED_REVIEW_THRESHOLD - graded_reviews)
        if weakest_studied_topic:
            return f"Review {remaining} more cards \u2014 start with your weakest area, {weakest_studied_topic}."
        return f"Review {remaining} more cards to reach the {GRADED_REVIEW_THRESHOLD}-review line."
    if weakest_studied_topic:
        return f"Review your weakest area: {weakest_studied_topic}."
    missing = coverage.most_impactful_missing_topic
    if missing is not None:
        return f"Add cards for \u201c{missing.name}\u201d ({missing.section}) to raise coverage."
    return "Keep your reviews current to hold your recall."


# --- Collection gathering (desktop engine reads) ----------------------------

def _quote(value: str) -> str:
    return '"' + value.replace('"', '\\"') + '"'


def gather_coverage(col, deck: str | None) -> CoverageReport:  # type: ignore[no-untyped-def]
    """Per-topic coverage via the same card search the browser uses."""
    sections: list[SectionCoverage] = []
    for token, name, _full, topics in OUTLINE:
        topic_cov: list[CoverageTopic] = []
        for topic_token, topic_name in topics:
            tag = f"{TAG_ROOT}::{token}::{topic_token}"
            clause = f"(tag:{tag} OR tag:{tag}::*)"
            query = f"deck:{_quote(deck)} AND {clause}" if deck else clause
            count = len(col.find_cards(query))
            topic_cov.append(CoverageTopic(section=name, name=topic_name, tag=tag, card_count=count))
        sections.append(SectionCoverage(section=name, topics=topic_cov))
    return CoverageReport(sections=sections)


def gather_evidence(col, deck: str) -> tuple[list[float], int, int]:  # type: ignore[no-untyped-def]
    """Real FSRS retrievabilities + graded-review total across studied cards."""
    ids = col.find_cards(f"deck:{_quote(deck)} -is:new")
    retrievabilities: list[float] = []
    graded = 0
    for cid in ids:
        stats = col.card_stats_data(cid)
        graded += int(stats.reviews)
        if stats.HasField("fsrs_retrievability"):
            retrievabilities.append(float(stats.fsrs_retrievability))
    return retrievabilities, graded, len(ids)


def weakest_studied_topic(col, deck: str) -> str | None:  # type: ignore[no-untyped-def]
    """The weakest MCAT section (by points-at-stake) among the deck's due cards."""
    try:
        did = col.decks.id(deck)
        if did is not None:
            col.decks.set_current(did)
        queue = col._backend.get_points_at_stake_queue(
            topic_tag_prefix=TOPIC_PREFIX, weight_by_topic_size=False
        )
    except Exception:
        return None
    best, best_w = None, -1.0
    for t in queue.topics:
        if t.HasField("mean_retrievability") and t.weakness > best_w:
            best_w, best = float(t.weakness), t.topic
    if best is None:
        return None
    return SECTION_NAMES.get(best, best)


def assess(col, deck: str) -> ReadinessAssessment:  # type: ignore[no-untyped-def]
    """Full deck-scoped assessment: coverage + evidence + best-next-thing."""
    coverage = gather_coverage(col, deck)
    retrievabilities, graded, studied = gather_evidence(col, deck)
    weakest = weakest_studied_topic(col, deck)
    return make_assessment(coverage, retrievabilities, graded, studied, weakest)


def _self_test() -> None:
    assert len(OUTLINE) == 4
    assert sum(len(t) for _a, _b, _c, t in OUTLINE) == 50, "expected 50 outline topics"

    # Abstain: no data.
    empty = CoverageReport(sections=[SectionCoverage(s, [CoverageTopic(s, "x", "MCAT::x", 0)]) for s in ["a"]])
    a = make_assessment(empty, [], 0, 0)
    assert not a.is_scored and a.memory is None

    # Scored: full coverage + reviews + strong recall.
    covered = CoverageReport(sections=[
        SectionCoverage(name, [CoverageTopic(name, tn, f"MCAT::{tok}::{tt}", 1) for tt, tn in topics])
        for tok, name, _full, topics in OUTLINE
    ])
    retr = [0.9] * 100
    scored = make_assessment(covered, retr, 300, 100, weakest_studied_topic="Physics")
    assert scored.is_scored, scored.missing_data
    assert scored.memory is not None and scored.performance is not None and scored.readiness is not None
    assert 85 <= scored.memory.percent_point <= 95
    assert SCALE_MIN <= scored.readiness.point <= SCALE_MAX
    assert scored.memory_confidence == "high"
    print("mcat_models self-test: OK")
    print(
        f"  coverage {covered.percent_covered}% ({covered.covered_topics}/{covered.total_topics}), "
        f"memory {scored.memory.percent_range_text}, "
        f"performance {scored.performance.percent_range_text}, "
        f"readiness {scored.readiness.range_text}, confidence {scored.memory_confidence}"
    )


if __name__ == "__main__":
    _self_test()
