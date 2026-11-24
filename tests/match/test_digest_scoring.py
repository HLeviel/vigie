from datetime import UTC, datetime

import pytest

from tests.match.fakes import FakeEmbedder
from tests.match.helpers import (
    NOW,
    PROFILE_EXAMPLE,
    counting_factory,
    make_offers,
    match_config,
    uids,
)
from tests.radar.factories import make_offer
from vigie.match.runner import MatchReport, key_for, run_match
from vigie.match.store import ScoreStore
from vigie.radar.config import DigestConfig
from vigie.radar.digest import (
    SCORING_UNAVAILABLE,
    DigestItem,
    SourceSummary,
    build_digest,
    render_html,
    render_markdown,
    render_text,
)
from vigie.radar.store import Store


def scored(min_score: float = 40.0, max_items: int = 50):  # type: ignore[no-untyped-def]
    store = Store.open(":memory:")
    for offer in make_offers():
        store.insert_offer(offer, NOW)
    config = match_config(
        PROFILE_EXAMPLE,
        f"  weights: {{ semantic: 0, lexical: 1 }}\n  digest: {{ min_score: {min_score} }}\n",
    )
    run_match(config, store, embedder_factory=counting_factory(FakeEmbedder()), now=NOW)
    key = key_for(config, "fake:test")
    return store, config, key, DigestConfig(max_items=max_items)


def test_offers_are_sorted_by_score_and_low_ones_are_only_counted() -> None:
    store, _, key, digest_config = scored(min_score=40)
    digest = build_digest(store, digest_config, NOW, key)
    assert [i.uid for i in digest.items] == uids("A")
    assert digest.scored and digest.total == 1
    assert digest.below_threshold == 3
    assert set(digest.below_uids) == set(uids("B", "C", "D"))
    assert "3 offres sous le seuil de pertinence (non listées)" in digest.subtitle
    assert "Triées par score décroissant" in digest.subtitle


def test_lower_threshold_lists_more_offers_by_decreasing_score() -> None:
    store, _, key, digest_config = scored(min_score=1)
    digest = build_digest(store, digest_config, NOW, key)
    assert [i.uid for i in digest.items] == uids("A", "B")  # C : score 0 ; D : exclue
    scores = [i.score for i in digest.items]
    assert scores == sorted(scores, reverse=True)
    assert digest.below_threshold == 2


def test_excluded_offers_are_never_listed_even_with_a_zero_threshold() -> None:
    store, _, key, digest_config = scored(min_score=0)
    digest = build_digest(store, digest_config, NOW, key)
    assert uids("D")[0] not in [i.uid for i in digest.items]
    assert uids("D")[0] in digest.below_uids


def test_max_items_applies_after_sorting() -> None:
    store, _, key, digest_config = scored(min_score=0, max_items=1)
    digest = build_digest(store, digest_config, NOW, key)
    assert [i.uid for i in digest.items] == uids("A")
    assert digest.total == 3  # A, B, C listables


def test_ties_are_broken_by_most_recent_publication() -> None:
    store = Store.open(":memory:")
    older = make_offer(
        source_id="old",
        title="Data",
        description="sql " * 80,
        published_at=datetime(2026, 11, 1, tzinfo=UTC),
    )
    newer = make_offer(
        source_id="new",
        title="Data",
        description="sql " * 80,
        published_at=datetime(2026, 11, 10, tzinfo=UTC),
    )
    store.insert_offer(older, NOW)
    store.insert_offer(newer, NOW)
    config = match_config(
        PROFILE_EXAMPLE, "  weights: { semantic: 0, lexical: 1 }\n  digest: { min_score: 0 }\n"
    )
    run_match(config, store, embedder_factory=counting_factory(FakeEmbedder()), now=NOW)
    digest = build_digest(store, DigestConfig(), NOW, key_for(config, "fake:test"))
    assert [i.uid for i in digest.items] == ["adzuna:new", "adzuna:old"]


def test_items_carry_score_explanation_and_confidence() -> None:
    store, _, key, digest_config = scored(min_score=1)
    a, b = build_digest(store, digest_config, NOW, key).items
    assert a.score is not None and a.score > b.score  # type: ignore[operator]
    assert len(a.found_skills) == 2 and a.missing_skills == ()
    assert b.found_skills == ("python",) and b.missing_skills == ("sql",)
    assert a.meta.startswith(f"score {a.score}/100")
    assert b.explanation() == "trouvées : python · manque : sql"
    assert not a.low_confidence


def test_low_confidence_is_marked() -> None:
    store = Store.open(":memory:")
    store.insert_offer(make_offer(source_id="short", title="Python sql", description="court"), NOW)
    config = match_config(PROFILE_EXAMPLE)
    run_match(config, store, embedder_factory=counting_factory(FakeEmbedder()), now=NOW)
    [item] = build_digest(store, DigestConfig(), NOW, key_for(config, "fake:test")).items
    assert item.low_confidence and "confiance faible" in item.meta


def test_missing_score_falls_back_to_date_order_with_a_note() -> None:
    store, config, key, digest_config = scored(min_score=1)
    store.insert_offer(
        make_offer(source_id="E", title="Nouvelle", description="x"), NOW
    )  # sans score
    digest = build_digest(store, digest_config, NOW, key)
    assert not digest.scored and digest.note == SCORING_UNAVAILABLE
    assert all(i.score is None for i in digest.items)
    dates = [i.date for i in digest.items]
    assert dates == sorted(dates, reverse=True) or len(set(dates)) < len(dates)
    assert digest.below_uids == ()  # rien n'est marqué notifié en secret


def test_corrupt_stored_details_fall_back_instead_of_crashing() -> None:
    store, _, key, digest_config = scored(min_score=1)
    ScoreStore(store).put("adzuna:A", key, 80.0, "pas du json", NOW)
    digest = build_digest(store, digest_config, NOW, key)
    assert not digest.scored and digest.note == SCORING_UNAVAILABLE


def test_a_note_given_by_the_caller_disables_scoring() -> None:
    store, _, key, digest_config = scored()
    digest = build_digest(store, digest_config, NOW, key, "Profil illisible.")
    assert not digest.scored and digest.note == "Profil illisible."


def test_without_scoring_the_digest_is_unchanged() -> None:
    store, _, _, digest_config = scored()
    digest = build_digest(store, digest_config, NOW)
    assert not digest.scored and digest.note == "" and digest.below_threshold == 0
    assert all(i.score is None for i in digest.items)
    assert len(digest.items) == 4


# --- rendu ----------------------------------------------------------------------------------------


def test_renderers_show_score_explanation_and_escape_everything() -> None:
    store, _, key, digest_config = scored(min_score=1)
    digest = build_digest(store, digest_config, NOW, key)
    html, md, text = render_html(digest), render_markdown(digest), render_text(digest)
    for rendered in (html, md, text):
        assert "score " in rendered and "/100" in rendered
        assert "trouvées" in rendered
        assert "sous le seuil" in rendered
    assert "<script" not in html


def test_hostile_skill_names_are_escaped_in_the_explanation() -> None:
    item = DigestItem.from_offer(make_offer())
    from vigie.match.scoring import ScoreDetails

    hostile = ScoreDetails(
        found_must_have=["<script>alert(1)</script>"], missing_must_have=["[x](http://evil)"]
    )
    scored_item = item.with_score(70.0, hostile)
    from vigie.radar.digest import Digest, md_escape

    digest = Digest(items=(scored_item,), total=1, generated_at=NOW, scored=True)
    assert "<script>" not in render_html(digest)
    assert "[x](http://evil)" not in render_markdown(digest)
    assert md_escape("[x](http://evil)") in render_markdown(digest)


def test_note_is_shown_in_the_footer_of_every_format() -> None:
    store, _, key, digest_config = scored()
    digest = build_digest(store, digest_config, NOW, None, "Scoring indisponible : test.")
    for rendered in (render_html(digest), render_markdown(digest), render_text(digest)):
        assert "Scoring indisponible : test." in rendered


def test_match_line_in_the_footer_uses_its_own_wording() -> None:
    line = SourceSummary("match", 4, 3, 1, 0, 0, False).text
    assert line == "match : à scorer 4, scorées 3, exclues par préférence 1"
    failed = SourceSummary("match", 0, 0, 0, 0, 1, True, "EmbedderError").text
    assert failed.endswith("(ÉCHEC : EmbedderError)")


def test_title_when_only_the_scoring_failed() -> None:
    store = Store.open(":memory:")
    run_id = "r1"
    store.start_run(run_id, NOW)
    store.add_run_source(
        run_id, MatchReport(failed=True, error_message="EmbedderError: x").as_source_report()
    )
    store.finish_run(run_id, NOW, "success")
    digest = build_digest(store, DigestConfig(), NOW)
    assert digest.should_send and digest.title == "Vigie : alerte, le scoring a échoué"


@pytest.mark.parametrize("source_failed", [True])
def test_title_when_a_source_failed_is_unchanged(source_failed: bool) -> None:
    from vigie.radar.models import SourceReport

    store = Store.open(":memory:")
    store.start_run("r", NOW)
    store.add_run_source("r", SourceReport(source="adzuna", errors=1, error_message="X: y"))
    store.finish_run("r", NOW, "failed")
    assert build_digest(store, DigestConfig(), NOW).title == "Vigie : alerte, une source a échoué"
