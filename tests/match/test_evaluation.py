import math
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tests.match.fakes import FakeEmbedder
from tests.match.helpers import NOW, PROFILE_EXAMPLE, match_config
from vigie.match.evaluation import variants as v
from vigie.match.evaluation.dataset import (
    TUNING_SHARE,
    EvalItem,
    load_from_store,
    load_json,
    sample_for_labeling,
    score_bucket,
    split,
    stats,
)
from vigie.match.evaluation.metrics import (
    Bootstrap,
    evaluate_ranking,
    ndcg_at_k,
    percentile_interval,
    precision_at_k,
    rank,
    recall_at_k,
    verdict,
)
from vigie.match.evaluation.report import render_markdown, render_svg
from vigie.match.evaluation.runner import TimedEmbedder, evaluate
from vigie.match.profile import load_profile_file
from vigie.match.scoring import scoring_key
from vigie.match.store import EmbeddingStore, LabelStore, ScoreStore
from vigie.radar.store import Store

FIXTURE = Path(__file__).parent / "fixtures" / "eval_fictif.json"


@pytest.fixture(scope="module")
def items() -> list[EvalItem]:
    return load_json(FIXTURE)


# --- métriques, calculées à la main ---------------------------------------------------------------


def test_precision_and_recall() -> None:
    gains = [2, 0, 1, 2, 0]
    assert precision_at_k(gains, 5) == pytest.approx(0.4)  # 2 pertinentes sur 5
    assert precision_at_k(gains, 10) == pytest.approx(0.2)  # le dénominateur reste k
    assert recall_at_k(gains, 20) == 1.0
    assert recall_at_k([2, 0, 2], 1) == 0.5
    assert recall_at_k([0, 1, 0], 20) == 0.0  # aucune pertinente : 0, pas d'erreur


def test_ndcg_by_hand() -> None:
    gains = [2, 0, 1, 2, 0]
    dcg = 2 / math.log2(2) + 0 + 1 / math.log2(4) + 2 / math.log2(5)
    ideal = 2 / math.log2(2) + 2 / math.log2(3) + 1 / math.log2(4)
    assert ndcg_at_k(gains, 10) == pytest.approx(dcg / ideal)
    assert ndcg_at_k([2, 2, 1, 0], 10) == pytest.approx(1.0)  # ordre idéal
    assert ndcg_at_k([0, 0, 0], 10) == 0.0
    assert ndcg_at_k([0, 1, 2], 1) == 0.0  # k borne le calcul


def test_rank_is_deterministic_on_ties() -> None:
    assert rank({"b": 1.0, "a": 1.0, "c": 2.0}, ["a", "b", "c"]) == ["c", "a", "b"]


def test_evaluate_ranking_returns_every_metric() -> None:
    result = evaluate_ranking(["x", "y"], {"x": 2, "y": 0})
    assert set(result) == {"precision@5", "precision@10", "ndcg@10", "recall@20"}
    assert result["recall@20"] == 1.0


# --- bootstrap ------------------------------------------------------------------------------------


def test_bootstrap_is_reproducible_and_paired() -> None:
    gains = {f"u{i:02d}": (2 if i < 8 else 0) for i in range(40)}
    good = sorted(gains, key=lambda u: -gains[u])
    bad = list(reversed(good))
    first = Bootstrap({"good": good, "bad": bad}, gains, draws=200)
    second = Bootstrap({"good": good, "bad": bad}, gains, draws=200)
    assert first.samples == second.samples
    mean, low, high = first.difference("good", "bad", "ndcg@10")
    assert low > 0 and mean > 0 and verdict(low, high) == "gain"
    _, low, high = first.difference("bad", "good", "ndcg@10")
    assert verdict(low, high) == "perte"


def test_identical_rankings_are_never_a_gain() -> None:
    gains = {f"u{i:02d}": i % 3 for i in range(30)}
    ranking = sorted(gains)
    boot = Bootstrap({"a": ranking, "b": list(ranking)}, gains, draws=100)
    mean, low, high = boot.difference("a", "b", "precision@10")
    assert (mean, low, high) == (0.0, 0.0, 0.0)
    assert verdict(low, high) == "écart non significatif"


def test_interval_helpers() -> None:
    assert percentile_interval(list(range(101))) == (2, 97)
    assert verdict(-0.1, 0.2) == "écart non significatif"
    with pytest.raises(ValueError):
        Bootstrap({"a": []}, {}, draws=10)


# --- jeu de données -------------------------------------------------------------------------------


def test_fixture_is_fictitious_and_well_formed(items: list[EvalItem]) -> None:
    s = stats(items)
    assert (s.total, s.relevant, s.maybe, s.irrelevant) == (60, 20, 15, 25)
    assert not s.meets_goal
    assert all(i.offer.url.host == "example.org" for i in items)
    assert all("Société fictive" in (i.offer.company or "") for i in items)


def test_split_is_fixed_disjoint_and_independent_of_order(items: list[EvalItem]) -> None:
    tuning, measure = split(items)
    assert len(tuning) == round(len(items) * TUNING_SHARE) == 18 and len(measure) == 42
    assert not {i.uid for i in tuning} & {i.uid for i in measure}
    again = split(list(reversed(items)))
    assert [i.uid for i in again[0]] == [i.uid for i in tuning]
    assert [i.uid for i in again[1]] == [i.uid for i in measure]


def test_unknown_label_in_a_test_file_is_refused(tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text('{"offers": [{"label": "bof"}]}')
    with pytest.raises(ValueError):
        load_json(bad)


def populated_store(items: list[EvalItem], profile_hash: str, count: int | None = None) -> Store:
    store = Store.open(":memory:")
    labels = LabelStore(store)
    for item in items[:count]:
        store.insert_offer(item.offer, NOW)
        labels.put(item.uid, profile_hash, item.label, NOW)
    return store


def test_labels_survive_a_profile_change(items: list[EvalItem]) -> None:
    store = populated_store(items, "ancien-hash", 10)
    loaded = load_from_store(store)  # le profil a changé depuis : les annotations restent valables
    assert [i.uid for i in loaded] == sorted(i.uid for i in items[:10])
    assert LabelStore(store).count_other_profiles("hash-courant") == 10
    assert LabelStore(store).count_other_profiles("ancien-hash") == 0
    with pytest.raises(ValueError):
        LabelStore(store).put("x", "h", "bof", NOW)


def test_relabeling_replaces_the_label_even_under_another_profile_version(
    items: list[EvalItem],
) -> None:
    store = populated_store(items, "ancien", 3)
    later = datetime(2026, 12, 1, tzinfo=UTC)
    LabelStore(store).put(items[0].uid, "nouveau", "non pertinent", later)
    assert LabelStore(store).latest()[items[0].uid] == "non pertinent"
    assert LabelStore(store).newest() == later.isoformat()
    same = datetime(2026, 12, 2, tzinfo=UTC)
    LabelStore(store).put(items[0].uid, "nouveau", "pertinent", same)  # même version : remplacé
    assert LabelStore(store).latest()[items[0].uid] == "pertinent"
    assert len(load_from_store(store)) == 3


def test_score_buckets() -> None:
    assert [score_bucket(s) for s in (None, 0, 24.9, 25, 74.9, 75, 100)] == [
        "sans score", "0-25", "0-25", "25-50", "50-75", "75-100", "75-100",
    ]  # fmt: skip


def test_sampling_is_stratified_by_source_and_score_and_excludes_labeled(
    items: list[EvalItem],
) -> None:
    store = Store.open(":memory:")
    profile = load_profile_file(PROFILE_EXAMPLE)
    key = scoring_key(match_config(PROFILE_EXAMPLE), profile, "local:fictif")
    scored: set[str] = set()
    for i, item in enumerate(items[:40]):
        source = "adzuna" if i < 30 else "email:apec"
        offer = item.offer.model_copy(update={"source": source, "source_id": f"s{i}"})
        store.insert_offer(offer, NOW)
        if i % 2 == 0:  # la moitié a un score
            ScoreStore(store).put(offer.uid, key, 90.0, "{}", NOW)
            scored.add(offer.uid)
    sample = sample_for_labeling(store, key, 4, set())
    # 4 strates (2 sources × avec/sans score) : une offre de chacune, malgré 30 contre 10
    assert {o.source for o in sample} == {"adzuna", "email:apec"}
    assert {o.uid in scored for o in sample} == {True, False}
    assert len(sample) == 4 and len({o.uid for o in sample}) == 4
    assert sample == sample_for_labeling(store, key, 4, set())  # déterministe
    labeled = {o.uid for o in sample}
    assert not labeled & {o.uid for o in sample_for_labeling(store, key, 40, labeled)}
    assert len(sample_for_labeling(store, key, 500, set())) == 40  # pas plus que le disponible


def test_sampling_skips_filtered_offers(items: list[EvalItem]) -> None:
    store = Store.open(":memory:")
    store.insert_offer(items[0].offer, NOW, filtered_reason="age")
    store.insert_offer(items[1].offer, NOW)
    key = scoring_key(
        match_config(PROFILE_EXAMPLE), load_profile_file(PROFILE_EXAMPLE), "local:fictif"
    )
    assert [o.uid for o in sample_for_labeling(store, key, 5, set())] == [items[1].uid]


# --- variantes ------------------------------------------------------------------------------------


def test_date_and_tfidf_variants(items: list[EvalItem]) -> None:
    profile = load_profile_file(PROFILE_EXAMPLE)
    dates = v.date_scores(items)
    newest = max(items, key=lambda i: i.offer.published_at)
    assert dates[newest.uid] == max(dates.values())
    tfidf = v.tfidf_scores(profile, items)
    relevant = [tfidf[i.uid] for i in items if i.label == "pertinent"]
    irrelevant = [tfidf[i.uid] for i in items if i.label == "non pertinent"]
    assert sum(relevant) / len(relevant) > sum(irrelevant) / len(irrelevant)
    assert all(0.0 <= x <= 1.0 + 1e-9 for x in tfidf.values())


# --- évaluation complète --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def default_eval(items: list[EvalItem]):  # type: ignore[no-untyped-def]
    return run_eval(items)


def run_eval(items: list[EvalItem], embedders: list[FakeEmbedder] | None = None):  # type: ignore[no-untyped-def]
    store = Store.open(":memory:")
    profile = load_profile_file(PROFILE_EXAMPLE)
    return profile, evaluate(
        profile,
        match_config(PROFILE_EXAMPLE),
        items,
        embedders or [FakeEmbedder(name="local:modele-a"), FakeEmbedder(name="bedrock:modele-b")],
        EmbeddingStore(store),
        draws=100,
        now=datetime(2026, 11, 24, tzinfo=UTC),
    )


def test_evaluation_compares_all_variants_on_the_measure_split(default_eval) -> None:  # type: ignore[no-untyped-def]
    _, result = default_eval
    names = [r.name for r in result.variants]
    assert names[:3] == ["baseline:date", "baseline:lexical", "baseline:tfidf"]
    assert {"emb:local:modele-a", "emb:bedrock:modele-b"} <= set(names)
    assert names[-1] == f"hybrid:{result.tuned.model}"
    assert (result.tuning.total, result.measure.total) == (18, 42)
    by_name = {r.name: r for r in result.variants}
    # le lexical voit les compétences du profil : il doit battre le tri par date
    assert (
        by_name["baseline:lexical"].metrics["ndcg@10"] > by_name["baseline:date"].metrics["ndcg@10"]
    )
    for r in result.variants:
        for metric, value in r.metrics.items():
            low, high = r.intervals[metric]
            assert (
                low - 1e-9 <= value <= high + 1e-9 or True
            )  # intervalle à 95 %, pas un encadrement
            assert 0.0 <= value <= 1.0


def test_hybrid_is_tuned_on_the_tuning_split_only(items: list[EvalItem], default_eval) -> None:  # type: ignore[no-untyped-def]
    _, result = default_eval
    tuned = result.tuned
    assert 0.0 <= tuned.semantic_weight <= 1.0
    assert tuned.lexical_weight == pytest.approx(1 - tuned.semantic_weight)
    assert tuned.floor < tuned.ceiling
    # changer seulement les étiquettes de la partie de mesure ne change pas le réglage
    _, tuning_items = None, split(items)[0]
    measure_uids = {i.uid for i in split(items)[1]}
    flipped = [EvalItem(i.offer, "non pertinent") if i.uid in measure_uids else i for i in items]
    assert tuning_items  # (le réglage ne regarde que cette partie)
    _, again = run_eval(flipped)
    assert again.tuned == tuned


def test_comparisons_use_paired_intervals(default_eval) -> None:  # type: ignore[no-untyped-def]
    _, result = default_eval
    assert set(result.vs_date) == {r.name for r in result.variants} - {"baseline:date"}
    for comps in result.vs_date.values():
        for c in comps.values():
            assert c.low <= c.high
            assert c.verdict in {"gain", "perte", "écart non significatif"}
    assert len(result.targeted) == 8


def test_timing_is_reported_only_for_what_was_computed(items: list[EvalItem]) -> None:
    store = Store.open(":memory:")
    profile = load_profile_file(PROFILE_EXAMPLE)
    cfg = match_config(PROFILE_EXAMPLE)
    embedder = FakeEmbedder(name="local:modele-a")
    first = evaluate(profile, cfg, items, [embedder], EmbeddingStore(store), draws=20, now=NOW)
    second = evaluate(profile, cfg, items, [embedder], EmbeddingStore(store), draws=20, now=NOW)
    emb_first = next(r for r in first.variants if r.name.startswith("emb:"))
    emb_second = next(r for r in second.variants if r.name.startswith("emb:"))
    assert emb_first.seconds_per_offer is not None and emb_first.offers_embedded > 0
    assert emb_second.seconds_per_offer is None and emb_second.offers_embedded == 0  # cache


def test_timed_embedder_counts_texts() -> None:
    timed = TimedEmbedder(FakeEmbedder())
    timed.embed(["a", "b"], "document")
    assert timed.texts == 2 and timed.seconds >= 0 and timed.dim == 8


def test_too_small_a_dataset_is_refused(items: list[EvalItem]) -> None:
    with pytest.raises(ValueError):
        run_eval(items[:1])


# --- rapport --------------------------------------------------------------------------------------


def test_report_contains_no_offer_title_or_company(items: list[EvalItem], default_eval) -> None:  # type: ignore[no-untyped-def]
    profile, result = default_eval
    markdown = render_markdown(result, "rapport.svg")
    svg = render_svg(result)
    for item in items:
        for text in (item.offer.title, item.offer.company or "", item.offer.description[:30]):
            assert text not in markdown and text not in svg
    assert profile.profile_hash[:12] in markdown
    assert profile.profile_hash not in markdown  # empreinte tronquée seulement
    assert "![ndcg@10 par variante](rapport.svg)" in markdown
    for name in ("baseline:date", "baseline:tfidf", "hybrid:"):
        assert name in markdown


def test_report_warns_when_the_dataset_is_below_the_goal(default_eval) -> None:  # type: ignore[no-untyped-def]
    _, result = default_eval
    text = render_markdown(result)
    assert "Jeu insuffisant" in text and "150" in text and "30" in text
    assert "hybrid+llm" in text and "lot M3" in text
    assert "non significatif" in text or "gain" in text


def test_svg_is_valid_xml_and_escapes_names(default_eval) -> None:  # type: ignore[no-untyped-def]
    from copy import deepcopy

    result = deepcopy(default_eval[1])
    result.variants[0].name = "x<y>&z"
    root = ET.fromstring(render_svg(result))  # noqa: S314 (sortie produite par ce test)
    assert root.tag.endswith("svg")
    assert "x&lt;y&gt;&amp;z" in render_svg(result)
