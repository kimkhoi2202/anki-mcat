// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

//! Points-at-stake review ordering (PRD 7a).
//!
//! Produces a *read-only* reordering of the due review queue, surfacing the
//! topics a student is weakest at first. A card's score is
//!
//! ```text
//! points_at_stake = topic_weight x student_weakness
//! ```
//!
//! where:
//! - **topic** is derived from a card's note tags: the first tag under a
//!   configurable prefix (default `MCAT::`), reduced to its top component (the
//!   level immediately under the prefix). Notes without such a tag bucket into
//!   the [`UNTAGGED_TOPIC`] topic.
//! - **student_weakness** for a topic is `1 - mean(FSRS retrievability)` across
//!   that topic's due review cards. Lower retrievability (weaker memory) yields
//!   higher weakness. Cards without FSRS memory state are excluded from the
//!   mean; a topic with no memory-state cards has weakness `0`.
//! - **topic_weight** is `1.0` for every topic by default (so the ordering is
//!   driven purely by weakness and the weakest topics surface first), or, when
//!   `weight_by_topic_size` is set, the topic's share of the due review queue
//!   (`card_count / total_due`).
//!
//! This never touches FSRS intervals, scheduling or any stored card data: it
//! only reads existing memory state, so undo and the scheduler are unaffected.

use std::collections::HashMap;

use fsrs::FSRS5_DEFAULT_DECAY;
use fsrs::FSRS;

use crate::prelude::*;

pub const DEFAULT_TOPIC_TAG_PREFIX: &str = "MCAT::";
pub const UNTAGGED_TOPIC: &str = "untagged";

#[derive(Debug, Clone)]
pub struct PointsAtStakeRequest {
    /// Tag prefix identifying topic tags (e.g. `MCAT::`).
    pub topic_tag_prefix: String,
    /// When false, all topics weigh equally (`topic_weight == 1.0`). When true,
    /// each topic is weighted by its share of the due review queue.
    pub weight_by_topic_size: bool,
}

impl Default for PointsAtStakeRequest {
    fn default() -> Self {
        Self {
            topic_tag_prefix: DEFAULT_TOPIC_TAG_PREFIX.to_string(),
            weight_by_topic_size: false,
        }
    }
}

#[derive(Debug, Clone, PartialEq)]
pub struct ScoredCard {
    pub card_id: CardId,
    pub topic: String,
    /// `topic_weight * weakness` — the value used for the descending sort.
    pub points_at_stake: f32,
    pub topic_weight: f32,
    pub weakness: f32,
    /// This card's own FSRS retrievability, if it has memory state.
    pub retrievability: Option<f32>,
}

/// Per-topic aggregate, surfaced for transparency/demo.
#[derive(Debug, Clone, PartialEq)]
pub struct TopicSummary {
    pub topic: String,
    pub card_count: u32,
    pub weakness: f32,
    pub topic_weight: f32,
    /// Mean FSRS retrievability across the topic's cards that have memory state.
    pub mean_retrievability: Option<f32>,
}

#[derive(Debug, Clone, Default, PartialEq)]
pub struct PointsAtStakeQueue {
    /// Due review cards ordered by descending points-at-stake (ties break by
    /// ascending card id).
    pub cards: Vec<ScoredCard>,
    /// Per-topic aggregates, in first-seen queue order.
    pub topics: Vec<TopicSummary>,
}

struct CardTopic {
    card_id: CardId,
    topic: String,
    retrievability: Option<f32>,
}

impl Collection {
    /// Return the due review queue reordered by points at stake. Read-only: this
    /// does not change FSRS intervals, scheduling or any stored card data.
    pub fn points_at_stake_queue(
        &mut self,
        req: PointsAtStakeRequest,
    ) -> Result<PointsAtStakeQueue> {
        let prefix = if req.topic_tag_prefix.trim().is_empty() {
            DEFAULT_TOPIC_TAG_PREFIX
        } else {
            req.topic_tag_prefix.as_str()
        };

        let timing = self.timing_today()?;
        let review_ids = self.due_review_card_ids()?;
        // FSRS::new(None) only computes retrievability; it never mutates state.
        let fsrs = FSRS::new(None)?;

        let mut per_card: Vec<CardTopic> = Vec::with_capacity(review_ids.len());
        for cid in review_ids {
            let card = self.storage.get_card(cid)?.or_not_found(cid)?;
            let note = self
                .storage
                .get_note(card.note_id)?
                .or_not_found(card.note_id)?;
            let topic = derive_topic(&note.tags, prefix);
            let retrievability = card.memory_state.map(|state| {
                let seconds = card.seconds_since_last_review(&timing).unwrap_or(0);
                let decay = card.decay.unwrap_or(FSRS5_DEFAULT_DECAY);
                fsrs.current_retrievability_seconds(state.into(), seconds, decay)
            });
            per_card.push(CardTopic {
                card_id: cid,
                topic,
                retrievability,
            });
        }

        let aggregates = TopicAggregates::from_cards(&per_card);
        let total = per_card.len() as f32;

        let mut cards: Vec<ScoredCard> = per_card
            .iter()
            .map(|c| {
                let weakness = aggregates.weakness(&c.topic);
                let topic_weight = aggregates.weight(&c.topic, req.weight_by_topic_size, total);
                ScoredCard {
                    card_id: c.card_id,
                    topic: c.topic.clone(),
                    points_at_stake: topic_weight * weakness,
                    topic_weight,
                    weakness,
                    retrievability: c.retrievability,
                }
            })
            .collect();

        // Descending by score; ascending card id breaks ties so the output is
        // deterministic regardless of the queue builder's internal order.
        cards.sort_by(|a, b| {
            b.points_at_stake
                .partial_cmp(&a.points_at_stake)
                .unwrap_or(std::cmp::Ordering::Equal)
                .then(a.card_id.0.cmp(&b.card_id.0))
        });

        let topics: Vec<TopicSummary> = aggregates
            .order
            .iter()
            .map(|topic| TopicSummary {
                topic: topic.clone(),
                card_count: aggregates.count(topic),
                weakness: aggregates.weakness(topic),
                topic_weight: aggregates.weight(topic, req.weight_by_topic_size, total),
                mean_retrievability: aggregates.mean_retrievability(topic),
            })
            .collect();

        Ok(PointsAtStakeQueue { cards, topics })
    }
}

/// Per-topic counts and retrievability sums, preserving first-seen order.
struct TopicAggregates {
    order: Vec<String>,
    counts: HashMap<String, u32>,
    retr_sum: HashMap<String, f32>,
    retr_n: HashMap<String, u32>,
}

impl TopicAggregates {
    fn from_cards(cards: &[CardTopic]) -> Self {
        let mut agg = TopicAggregates {
            order: Vec::new(),
            counts: HashMap::new(),
            retr_sum: HashMap::new(),
            retr_n: HashMap::new(),
        };
        for c in cards {
            if !agg.counts.contains_key(&c.topic) {
                agg.order.push(c.topic.clone());
            }
            *agg.counts.entry(c.topic.clone()).or_default() += 1;
            if let Some(r) = c.retrievability {
                *agg.retr_sum.entry(c.topic.clone()).or_default() += r;
                *agg.retr_n.entry(c.topic.clone()).or_default() += 1;
            }
        }
        agg
    }

    fn count(&self, topic: &str) -> u32 {
        self.counts.get(topic).copied().unwrap_or(0)
    }

    fn mean_retrievability(&self, topic: &str) -> Option<f32> {
        match self.retr_n.get(topic).copied().unwrap_or(0) {
            0 => None,
            n => Some(self.retr_sum.get(topic).copied().unwrap_or(0.0) / n as f32),
        }
    }

    /// `1 - mean(retrievability)`, or `0` when the topic has no memory-state
    /// cards to assess.
    fn weakness(&self, topic: &str) -> f32 {
        self.mean_retrievability(topic)
            .map(|mean| 1.0 - mean)
            .unwrap_or(0.0)
    }

    fn weight(&self, topic: &str, by_size: bool, total: f32) -> f32 {
        if by_size {
            if total > 0.0 {
                self.count(topic) as f32 / total
            } else {
                0.0
            }
        } else {
            1.0
        }
    }
}

/// Derive a topic name from a note's tags. Returns the top component of the
/// first tag found under `prefix`, or [`UNTAGGED_TOPIC`] if none match.
fn derive_topic(tags: &[String], prefix: &str) -> String {
    for tag in tags {
        if let Some(rest) = strip_prefix_ci(tag, prefix) {
            if let Some(top) = rest.split("::").find(|component| !component.is_empty()) {
                return top.to_string();
            }
        }
    }
    UNTAGGED_TOPIC.to_string()
}

/// ASCII case-insensitive prefix strip that respects char boundaries. Returns
/// the remainder after `prefix`, or `None` if `tag` doesn't start with it.
fn strip_prefix_ci<'a>(tag: &'a str, prefix: &str) -> Option<&'a str> {
    let head = tag.get(..prefix.len())?;
    if head.eq_ignore_ascii_case(prefix) {
        tag.get(prefix.len()..)
    } else {
        None
    }
}

impl From<anki_proto::scheduler::GetPointsAtStakeQueueRequest> for PointsAtStakeRequest {
    fn from(req: anki_proto::scheduler::GetPointsAtStakeQueueRequest) -> Self {
        let topic_tag_prefix = if req.topic_tag_prefix.trim().is_empty() {
            DEFAULT_TOPIC_TAG_PREFIX.to_string()
        } else {
            req.topic_tag_prefix
        };
        PointsAtStakeRequest {
            topic_tag_prefix,
            weight_by_topic_size: req.weight_by_topic_size,
        }
    }
}

impl From<ScoredCard> for anki_proto::scheduler::points_at_stake_queue::ScoredCard {
    fn from(card: ScoredCard) -> Self {
        Self {
            card_id: card.card_id.0,
            topic: card.topic,
            points_at_stake: card.points_at_stake,
            topic_weight: card.topic_weight,
            weakness: card.weakness,
            retrievability: card.retrievability,
        }
    }
}

impl From<TopicSummary> for anki_proto::scheduler::points_at_stake_queue::TopicSummary {
    fn from(summary: TopicSummary) -> Self {
        Self {
            topic: summary.topic,
            card_count: summary.card_count,
            weakness: summary.weakness,
            topic_weight: summary.topic_weight,
            mean_retrievability: summary.mean_retrievability,
        }
    }
}

impl From<PointsAtStakeQueue> for anki_proto::scheduler::PointsAtStakeQueue {
    fn from(queue: PointsAtStakeQueue) -> Self {
        Self {
            cards: queue.cards.into_iter().map(Into::into).collect(),
            topics: queue.topics.into_iter().map(Into::into).collect(),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::card::CardQueue;
    use crate::card::CardType;
    use crate::card::FsrsMemoryState;
    use crate::tests::NoteAdder;

    /// Adds a review card due today, tagged with `tags`, with an FSRS memory
    /// state whose stability we control. last_review_time is fixed at one day
    /// ago, so retrievability (and therefore weakness) is driven purely by
    /// stability: lower stability -> lower retrievability -> higher weakness.
    fn add_due_review_card(col: &mut Collection, tags: &[&str], stability: f32) -> CardId {
        let nt = col.get_notetype_by_name("Basic").unwrap().unwrap();
        let mut note = nt.new_note();
        note.tags = tags.iter().map(|t| t.to_string()).collect();
        col.add_note(&mut note, DeckId(1)).unwrap();

        let cid = col.storage.card_ids_of_notes(&[note.id]).unwrap()[0];
        let mut card = col.storage.get_card(cid).unwrap().unwrap();
        card.ctype = CardType::Review;
        card.queue = CardQueue::Review;
        card.due = 0;
        card.interval = 10;
        card.reps = 1;
        card.memory_state = Some(FsrsMemoryState {
            stability,
            difficulty: 5.0,
        });
        card.decay = Some(FSRS5_DEFAULT_DECAY);
        card.last_review_time = Some(TimestampSecs::now().adding_secs(-86_400));
        col.storage.update_card(&card).unwrap();
        col.clear_study_queues();
        cid
    }

    #[test]
    fn weak_topics_are_ordered_first() {
        let mut col = Collection::new();
        // Strong topic: high stability -> high retrievability -> low weakness.
        let strong = add_due_review_card(&mut col, &["MCAT::Biochemistry"], 10_000.0);
        // Weak topic: low stability -> low retrievability -> high weakness.
        let weak = add_due_review_card(&mut col, &["MCAT::Physics"], 1.0);

        let out = col
            .points_at_stake_queue(PointsAtStakeRequest::default())
            .unwrap();

        let order: Vec<CardId> = out.cards.iter().map(|c| c.card_id).collect();
        assert_eq!(
            order,
            vec![weak, strong],
            "the weaker topic must be surfaced first"
        );

        let weak_card = out.cards.iter().find(|c| c.card_id == weak).unwrap();
        let strong_card = out.cards.iter().find(|c| c.card_id == strong).unwrap();
        assert_eq!(weak_card.topic, "Physics");
        assert_eq!(strong_card.topic, "Biochemistry");
        assert!(
            weak_card.points_at_stake > strong_card.points_at_stake,
            "weak topic score {} should exceed strong topic score {}",
            weak_card.points_at_stake,
            strong_card.points_at_stake
        );
        assert!(weak_card.weakness > strong_card.weakness);
    }

    #[test]
    fn empty_queue_returns_no_cards() {
        let mut col = Collection::new();
        // A brand-new card is not a due *review* card, so the review queue is
        // empty.
        NoteAdder::basic(&mut col).add(&mut col);

        let out = col
            .points_at_stake_queue(PointsAtStakeRequest::default())
            .unwrap();

        assert!(
            out.cards.is_empty(),
            "no due review cards should yield an empty ordering"
        );
        assert!(out.topics.is_empty());
    }

    #[test]
    fn ties_break_deterministically_by_card_id() {
        let mut col = Collection::new();
        // Two cards in the same topic with identical stability -> identical
        // score. Output must be deterministic (ascending card id).
        let first = add_due_review_card(&mut col, &["MCAT::Bio"], 50.0);
        let second = add_due_review_card(&mut col, &["MCAT::Bio"], 50.0);

        let out = col
            .points_at_stake_queue(PointsAtStakeRequest::default())
            .unwrap();

        let order: Vec<CardId> = out.cards.iter().map(|c| c.card_id).collect();
        assert_eq!(
            order,
            vec![first, second],
            "equal scores must order by ascending card id"
        );
        assert_eq!(
            out.cards[0].points_at_stake, out.cards[1].points_at_stake,
            "the two tied cards must have identical scores"
        );
        assert_eq!(out.topics.len(), 1, "both cards share one topic");
        assert_eq!(out.topics[0].card_count, 2);
    }

    #[test]
    fn untagged_cards_bucket_into_untagged_topic() {
        let mut col = Collection::new();
        let tagged = add_due_review_card(&mut col, &["MCAT::Physiology"], 1.0);
        // A tag that exists but isn't under the prefix.
        let other_prefix = add_due_review_card(&mut col, &["other::tag"], 1.0);
        let no_tags = add_due_review_card(&mut col, &[], 1.0);

        let out = col
            .points_at_stake_queue(PointsAtStakeRequest::default())
            .unwrap();

        let topic_of = |cid: CardId| {
            out.cards
                .iter()
                .find(|c| c.card_id == cid)
                .unwrap()
                .topic
                .clone()
        };
        assert_eq!(topic_of(tagged), "Physiology");
        assert_eq!(topic_of(other_prefix), UNTAGGED_TOPIC);
        assert_eq!(topic_of(no_tags), UNTAGGED_TOPIC);

        let untagged = out
            .topics
            .iter()
            .find(|t| t.topic == UNTAGGED_TOPIC)
            .expect("an untagged topic summary should exist");
        assert_eq!(
            untagged.card_count, 2,
            "both non-MCAT cards share the untagged bucket"
        );
    }

    #[test]
    fn case_insensitive_prefix_and_nested_topic() {
        let mut col = Collection::new();
        // Mixed-case prefix and a deeply nested tag: topic is the top component.
        let cid = add_due_review_card(&mut col, &["mcat::Biochem::Enzymes::Kinetics"], 5.0);

        let out = col
            .points_at_stake_queue(PointsAtStakeRequest::default())
            .unwrap();

        let scored = out.cards.iter().find(|c| c.card_id == cid).unwrap();
        assert_eq!(
            scored.topic, "Biochem",
            "topic should be the top component below the (case-insensitive) prefix"
        );
    }

    #[test]
    fn does_not_change_scheduling_or_card_data() {
        let mut col = Collection::new();
        let cid = add_due_review_card(&mut col, &["MCAT::Bio"], 5.0);
        let before = col.storage.get_card(cid).unwrap().unwrap();

        let _ = col
            .points_at_stake_queue(PointsAtStakeRequest::default())
            .unwrap();

        let after = col.storage.get_card(cid).unwrap().unwrap();
        assert_eq!(before.interval, after.interval, "interval must be unchanged");
        assert_eq!(before.due, after.due, "due must be unchanged");
        assert_eq!(before.ctype, after.ctype);
        assert_eq!(before.queue, after.queue);
        assert_eq!(before.ease_factor, after.ease_factor);
        assert_eq!(before.reps, after.reps);
        assert_eq!(before.lapses, after.lapses);
        assert_eq!(before.remaining_steps, after.remaining_steps);
        assert_eq!(
            before.memory_state, after.memory_state,
            "FSRS memory state must be unchanged"
        );
        assert_eq!(before, after, "the card must be completely unchanged");
    }

    #[test]
    fn does_not_break_undo() {
        let mut col = Collection::new();
        let note = NoteAdder::basic(&mut col).add(&mut col);
        let cid = col.storage.card_ids_of_notes(&[note.id]).unwrap()[0];
        assert_eq!(
            col.storage.get_card(cid).unwrap().unwrap().ctype,
            CardType::New
        );

        // Answer the new card: this creates an undo entry and moves it to the
        // learning queue.
        col.answer_again();
        assert_eq!(
            col.storage.get_card(cid).unwrap().unwrap().ctype,
            CardType::Learn,
            "precondition: answering moved the card to learning"
        );

        // A read-only points-at-stake call between answering and undo must not
        // interfere with the undo queue or corrupt any data.
        let _ = col
            .points_at_stake_queue(PointsAtStakeRequest::default())
            .unwrap();

        col.undo().unwrap();
        let restored = col.storage.get_card(cid).unwrap().unwrap();
        assert_eq!(
            restored.ctype,
            CardType::New,
            "undo must restore the card after a points-at-stake call"
        );
        assert_eq!(restored.queue, CardQueue::New);
        assert_eq!(restored.reps, 0, "the answer's rep increment was undone");
    }

    #[test]
    fn weight_by_topic_size_favours_larger_topics() {
        let mut col = Collection::new();
        // Two topics with the same per-card weakness (same stability), but the
        // "big" topic has more cards. With size weighting it should rank first.
        let small = add_due_review_card(&mut col, &["MCAT::Small"], 2.0);
        let big_a = add_due_review_card(&mut col, &["MCAT::Big"], 2.0);
        let big_b = add_due_review_card(&mut col, &["MCAT::Big"], 2.0);
        let big_c = add_due_review_card(&mut col, &["MCAT::Big"], 2.0);

        let out = col
            .points_at_stake_queue(PointsAtStakeRequest {
                topic_tag_prefix: DEFAULT_TOPIC_TAG_PREFIX.to_string(),
                weight_by_topic_size: true,
            })
            .unwrap();

        // The three "Big" cards (higher topic_weight) must precede the lone
        // "Small" card.
        let order: Vec<CardId> = out.cards.iter().map(|c| c.card_id).collect();
        assert_eq!(out.cards.last().unwrap().card_id, small);
        assert!(order[..3].contains(&big_a));
        assert!(order[..3].contains(&big_b));
        assert!(order[..3].contains(&big_c));

        let big = out.topics.iter().find(|t| t.topic == "Big").unwrap();
        let small_topic = out.topics.iter().find(|t| t.topic == "Small").unwrap();
        assert!(
            big.topic_weight > small_topic.topic_weight,
            "the larger topic should carry more weight"
        );
    }
}
