from pathlib import Path

import pytest

from tests.match.fakes import FakeEmbedder
from tests.match.helpers import NOW, PROFILE_EXAMPLE, counting_factory, make_offers, match_config
from vigie.match.embedders.base import EmbedderError
from vigie.match.profile import ProfileError
from vigie.match.runner import MATCH_SOURCE, MatchReport, key_for, run_match
from vigie.match.scoring import scoring_signature
from vigie.match.store import EmbeddingStore, ScoreStore
from vigie.radar.store import Store


def filled_store() -> Store:
    store = Store.open(":memory:")
    for offer in make_offers():
        store.insert_offer(offer, NOW)
    return store


def test_scores_pending_offers_and_stores_details() -> None:
    store, fake = filled_store(), FakeEmbedder()
    config = match_config(PROFILE_EXAMPLE)
    report = run_match(config, store, embedder_factory=counting_factory(fake), now=NOW)
    assert (report.pending, report.scored, report.excluded, report.failed) == (4, 4, 1, False)
    key = key_for(config, fake.name)
    stored = ScoreStore(store).get_many([o.uid for o in store.pending_offers()], key)
    assert len(stored) == 4
    assert stored["adzuna:D"][0] == 0.0  # CDD : exclue par les préférences
    assert stored["adzuna:A"][0] > stored["adzuna:B"][0] > stored["adzuna:C"][0]
    assert '"excluded":true' in stored["adzuna:D"][1]


def test_excluded_offers_are_not_embedded() -> None:
    store, fake = filled_store(), FakeEmbedder()
    run_match(
        match_config(PROFILE_EXAMPLE), store, embedder_factory=counting_factory(fake), now=NOW
    )
    offer_calls = [texts for texts, kind in fake.calls if kind == "document"]
    assert sum(len(t) for t in offer_calls) == 3  # A, B, C ; pas D
    assert not any("Data python" in text for texts in offer_calls for text in texts)


def test_second_run_neither_rescores_nor_recomputes() -> None:
    store, fake = filled_store(), FakeEmbedder()
    config = match_config(PROFILE_EXAMPLE)
    run_match(config, store, embedder_factory=counting_factory(fake), now=NOW)
    calls = len(fake.calls)
    again = run_match(config, store, embedder_factory=counting_factory(fake), now=NOW)
    assert (again.pending, again.scored) == (0, 0)
    assert len(fake.calls) == calls


def test_a_new_offer_is_scored_alone_and_its_embedding_is_cached() -> None:
    store, fake = filled_store(), FakeEmbedder()
    config = match_config(PROFILE_EXAMPLE)
    run_match(config, store, embedder_factory=counting_factory(fake), now=NOW)
    from tests.radar.factories import make_offer

    store.insert_offer(make_offer(source_id="E", title="Data", description="sql " * 80), NOW)
    before = fake.texts_embedded
    report = run_match(config, store, embedder_factory=counting_factory(fake), now=NOW)
    assert (report.pending, report.scored) == (1, 1)
    assert fake.texts_embedded == before + 1


def test_profile_change_rescoring_but_offer_embeddings_are_kept(tmp_path: Path) -> None:
    store, fake = filled_store(), FakeEmbedder()
    run_match(
        match_config(PROFILE_EXAMPLE), store, embedder_factory=counting_factory(fake), now=NOW
    )
    offers_embedded = sum(len(t) for t, kind in fake.calls if kind == "document")

    changed = tmp_path / "profile.yaml"
    changed.write_text(PROFILE_EXAMPLE.read_text().replace("ML engineer", "NLP engineer"))
    report = run_match(
        match_config(changed), store, embedder_factory=counting_factory(fake), now=NOW
    )
    assert report.scored == 4  # clé différente : tout est rescoré
    assert sum(len(t) for t, kind in fake.calls if kind == "document") == offers_embedded


def test_scoring_setting_change_rescoring() -> None:
    store, fake = filled_store(), FakeEmbedder()
    run_match(
        match_config(PROFILE_EXAMPLE), store, embedder_factory=counting_factory(fake), now=NOW
    )
    other = match_config(PROFILE_EXAMPLE, "  weights: { semantic: 0.5, lexical: 0.5 }\n")
    assert scoring_signature(other) != scoring_signature(match_config(PROFILE_EXAMPLE))
    assert run_match(other, store, embedder_factory=counting_factory(fake), now=NOW).scored == 4


class Failing(FakeEmbedder):
    def embed(self, texts: list[str], kind: str) -> list[list[float]]:  # type: ignore[override]
        raise EmbedderError("fournisseur indisponible")


def test_provider_failure_is_reported_and_nothing_is_scored() -> None:
    store = filled_store()
    report = run_match(
        match_config(PROFILE_EXAMPLE), store, embedder_factory=counting_factory(Failing()), now=NOW
    )
    assert report.failed and report.scored == 0
    assert report.error_message == "EmbedderError: fournisseur indisponible"
    assert ScoreStore(store).count() == 0
    assert EmbeddingStore(store).count() == 0


def test_provider_construction_failure_is_reported() -> None:
    def broken(provider: str, model: str) -> FakeEmbedder:
        raise EmbedderError("fournisseur indisponible")

    report = run_match(match_config(PROFILE_EXAMPLE), filled_store(), embedder_factory=broken)
    assert report.failed


def test_invalid_profile_is_reported_without_values(tmp_path: Path) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text("summary: valeur-secrete\n")
    report = run_match(
        match_config(bad), filled_store(), embedder_factory=counting_factory(FakeEmbedder())
    )
    assert report.failed
    assert (report.error_message or "").startswith("ProfileError")
    assert "valeur-secrete" not in (report.error_message or "")


def test_missing_profile_file_is_reported(tmp_path: Path) -> None:
    report = run_match(match_config(tmp_path / "absent.yaml"), filled_store())
    assert report.failed and "ProfileError" in (report.error_message or "")
    with pytest.raises(ProfileError):
        key_for(match_config(tmp_path / "absent.yaml"))


def test_unexpected_bug_is_reported_by_type_only() -> None:
    class Buggy(FakeEmbedder):
        def embed(self, texts: list[str], kind: str) -> list[list[float]]:  # type: ignore[override]
            raise RuntimeError("texte-confidentiel de l'offre")

    report = run_match(
        match_config(PROFILE_EXAMPLE), filled_store(), embedder_factory=counting_factory(Buggy())
    )
    assert report.failed
    from vigie.radar.cli import failure_reason

    assert failure_reason(report.error_message) == " (RuntimeError)"


def test_report_maps_to_a_run_sources_row() -> None:
    row = MatchReport(pending=4, scored=3, excluded=1).as_source_report(12)
    assert (row.source, row.fetched, row.new, row.filtered, row.errors) == (
        MATCH_SOURCE,
        4,
        3,
        1,
        0,
    )
    failed = MatchReport(failed=True, error_message="EmbedderError: x").as_source_report()
    assert failed.errors == 1 and failed.error_message == "EmbedderError: x"


def test_no_pending_offer_builds_nothing() -> None:
    store, fake = Store.open(":memory:"), FakeEmbedder()
    report = run_match(
        match_config(PROFILE_EXAMPLE), store, embedder_factory=counting_factory(fake), now=NOW
    )
    assert (report.pending, report.failed) == (0, False)
    assert fake.calls == []  # pas même le profil


def test_default_key_name_matches_the_real_embedders_naming() -> None:
    """Le digest retrouve les scores sans fournisseur : le nom est « <fournisseur>:<modèle> »."""
    from vigie.match.embedders.bedrock import BedrockEmbedder
    from vigie.match.embedders.local import LocalEmbedder

    assert key_for(match_config(PROFILE_EXAMPLE)).model == "local:fictif"
    assert (
        LocalEmbedder(
            "fictif", lambda _m: type("M", (), {"get_sentence_embedding_dimension": lambda s: 3})()
        ).name
        == "local:fictif"
    )
    assert (
        BedrockEmbedder("amazon.titan-embed-text-v2:0", object()).name
        == "bedrock:amazon.titan-embed-text-v2:0"
    )
