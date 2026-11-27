"""Étage LLM dans `run_match` et dans le digest."""

import json
from pathlib import Path

import pytest

from tests.match.fakes import FakeEmbedder
from tests.match.helpers import (
    LEXICAL_ONLY,
    NOW,
    PROFILE_EXAMPLE,
    counting_factory,
    make_offers,
    match_config,
)
from tests.match.llm_helpers import MODEL, FakeJudge, assessment
from vigie.match.judge.base import JudgeUnavailable
from vigie.match.llm_stage import LlmReport, llm_key
from vigie.match.profile import load_profile_file
from vigie.match.runner import LLM_SOURCE, MatchReport, key_for, run_match
from vigie.match.store import LlmCallStore, ScoreStore
from vigie.radar.config import DigestConfig
from vigie.radar.digest import (
    SourceSummary,
    build_digest,
    render_html,
    render_markdown,
    render_text,
)
from vigie.radar.models import SourceReport
from vigie.radar.store import Store

LLM = f"  llm: {{ enabled: true, model: {MODEL}, top_n: 10 }}\n"
EMB = counting_factory(FakeEmbedder(name="local:fictif"))


def filled() -> Store:
    store = Store.open(":memory:")
    for offer in make_offers():
        store.insert_offer(offer, NOW)
    return store


def config(extra: str = ""):  # type: ignore[no-untyped-def]
    return match_config(PROFILE_EXAMPLE, LEXICAL_ONLY + extra)


def run(store: Store, judge: FakeJudge | None, extra: str = LLM, **kw):  # type: ignore[no-untyped-def]
    return run_match(
        config(extra),
        store,
        embedder_factory=EMB,
        judge_factory=(lambda _cfg: judge) if judge is not None else None,
        run_id="run-1",
        now=NOW,
        **kw,
    )


def test_llm_stage_runs_after_the_hybrid_stage_on_the_best_offers() -> None:
    store = filled()
    judge = FakeJudge(script=[assessment(30, "passer"), assessment(95, "postuler"), assessment(10)])
    report = run(store, judge)
    assert not report.failed and not report.llm_failed and not report.any_failure
    assert (report.scored, report.excluded) == (4, 1)  # l'étage hybride est intact
    llm = report.llm
    assert llm is not None and (llm.candidates, llm.assessed, llm.calls) == (3, 3, 3)  # D : exclue
    assert report.llm_calls == 3
    assert [v.title for v in judge.seen] == ["Ingénieur IA", "Développeur", "Comptable"]  # A, B, C
    assert LlmCallStore(store).totals("run-1") == (3, 1500, 300)


def test_disabled_llm_stage_does_nothing() -> None:
    report = run(filled(), None, extra="")
    assert report.llm is None and report.llm_calls == 0 and report.llm_source_report() is None
    assert key_for(config()).llm is None


def test_llm_stage_is_not_attempted_when_the_hybrid_stage_fails() -> None:
    def broken(provider: str, model: str):  # type: ignore[no-untyped-def]
        raise RuntimeError("panne")

    judge = FakeJudge()
    report = run_match(
        config(LLM), filled(), embedder_factory=broken, judge_factory=lambda _c: judge
    )
    assert report.failed and report.llm is None and judge.seen == []


def test_llm_failure_keeps_every_hybrid_score() -> None:
    store = filled()
    judge = FakeJudge(
        script=[JudgeUnavailable("OpenRouter a refusé la clé OPENROUTER_API_KEY (HTTP 401)")]
    )
    report = run(store, judge)
    assert not report.failed and report.llm_failed and report.any_failure
    assert report.llm is not None
    assert report.llm.error_message == (
        "JudgeUnavailable: OpenRouter a refusé la clé OPENROUTER_API_KEY (HTTP 401)"
    )
    key = key_for(config(LLM), "local:fictif")
    assert len(ScoreStore(store).get_many([o.uid for o in store.pending_offers()], key)) == 4


def test_factory_failure_is_an_llm_failure_not_a_hybrid_failure() -> None:
    def no_key(_cfg):  # type: ignore[no-untyped-def]
        raise JudgeUnavailable("variable d'environnement manquante : OPENROUTER_API_KEY")

    report = run_match(config(LLM), filled(), embedder_factory=EMB, judge_factory=no_key, now=NOW)
    assert not report.failed and report.llm_failed
    assert "OPENROUTER_API_KEY" in (report.llm.error_message or "")  # type: ignore[union-attr]


def test_unexpected_judge_bug_is_reported_by_type_only() -> None:
    class Buggy(FakeJudge):
        def assess(self, profile, offer):  # type: ignore[no-untyped-def]
            raise RuntimeError("texte-confidentiel de l'offre")

    report = run(filled(), Buggy())
    assert report.llm_failed
    from vigie.radar.cli import failure_reason

    # un bogue n'est pas un JudgeError : seul son type est affiché
    assert "RuntimeError" in (report.llm.error_message or "")  # type: ignore[union-attr]
    assert failure_reason(report.llm.error_message) == " (RuntimeError)"  # type: ignore[union-attr]


def test_default_factory_reads_the_key_from_settings_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.chdir(tmp_path)  # pas de .env
    report = run(filled(), None)
    assert report.llm_failed and "OPENROUTER_API_KEY" in (report.llm.error_message or "")  # type: ignore[union-attr]


def test_second_run_reuses_the_cached_assessments() -> None:
    store = filled()
    run(store, FakeJudge())
    judge = FakeJudge()
    report = run(store, judge)
    assert judge.seen == [] and report.llm is not None and report.llm.cached == 3
    assert report.llm.assessed == 0 and report.llm.calls == 0


def test_run_sources_row_for_the_llm_stage() -> None:
    report = MatchReport(llm=LlmReport(candidates=5, cached=2, assessed=2, invalid=1))
    row = report.llm_source_report()
    assert row is not None
    assert (row.source, row.fetched, row.new, row.duplicates, row.filtered, row.errors) == (
        LLM_SOURCE,
        5,
        2,
        2,
        1,
        0,
    )
    failed = MatchReport(llm=LlmReport(failed=True, error_message="JudgeUnavailable: x"))
    row = failed.llm_source_report()
    assert row is not None and row.errors == 1 and row.error_message == "JudgeUnavailable: x"


# --- digest : score final ------------------------------------------------------------------------


def digest(store: Store, extra: str = LLM, **kw):  # type: ignore[no-untyped-def]
    cfg = config(extra)
    return build_digest(store, DigestConfig(), NOW, key_for(cfg, "local:fictif"), **kw)


def test_final_score_is_the_llm_score_and_sorting_and_threshold_use_it() -> None:
    store = filled()
    # hybride : A 90, B 20, C 0. LLM : A 30 (sous le seuil 40), B 95, C 10.
    run(
        store,
        FakeJudge(script=[assessment(30, "passer"), assessment(95, "postuler"), assessment(10)]),
    )
    result = digest(store)
    assert [i.title for i in result.items] == ["Développeur"]  # B passe devant, A tombe
    [item] = result.items
    assert item.score == 95 and item.recommendation == "postuler"
    assert result.below_threshold == 3 and result.scored
    assert "recommandation : postuler" in item.meta
    for rendered in (render_html(result), render_markdown(result), render_text(result)):
        assert "recommandation : postuler" in rendered


def test_without_llm_scores_the_digest_is_the_hybrid_ranking() -> None:
    store = filled()
    run(store, None, extra="")
    result = digest(store, extra=LLM)  # clé LLM présente, mais aucune évaluation stockée
    assert [i.title for i in result.items] == ["Ingénieur IA"]
    assert result.items[0].recommendation == ""


def test_an_offer_keeps_its_hybrid_score_when_its_llm_details_are_unreadable() -> None:
    store = filled()
    run(store, FakeJudge(default=assessment(5, "passer")))
    llm = llm_key(config(LLM).llm, load_profile_file(PROFILE_EXAMPLE))
    uid = "adzuna:A"
    ScoreStore(store).put(uid, llm, 5.0, "pas du json", NOW)
    result = digest(store)
    assert [i.title for i in result.items] == ["Ingénieur IA"]  # score hybride 90 conservé
    assert result.items[0].recommendation == ""


def test_an_unknown_recommendation_is_ignored() -> None:
    store = filled()
    run(store, None, extra="")
    llm = llm_key(config(LLM).llm, load_profile_file(PROFILE_EXAMPLE))
    bad = json.dumps({"assessment": {"recommendation": "<script>alert(1)</script>"}})
    ScoreStore(store).put("adzuna:A", llm, 99.0, bad, NOW)
    result = digest(store)
    assert result.items[0].recommendation == "" and result.items[0].score == 90


def test_excluded_offers_never_get_an_llm_verdict() -> None:
    store = filled()
    run(store, None, extra="")
    llm = llm_key(config(LLM).llm, load_profile_file(PROFILE_EXAMPLE))
    good = json.dumps({"assessment": {"recommendation": "postuler"}})
    ScoreStore(store).put("adzuna:D", llm, 100.0, good, NOW)  # D : exclue (contrat CDD)
    assert "adzuna:D" in digest(store).below_uids


def test_llm_note_is_shown_while_staying_sorted_by_hybrid_score() -> None:
    store = filled()
    run(store, FakeJudge(script=[JudgeUnavailable("x")]))
    result = digest(store, llm_note="Étage LLM ignoré pour cette exécution.")
    assert result.scored and result.note == "Étage LLM ignoré pour cette exécution."
    assert [i.title for i in result.items] == ["Ingénieur IA"]
    assert "Étage LLM ignoré" in render_html(result) and "Étage LLM ignoré" in render_text(result)


def test_footer_line_of_the_llm_stage_has_counters_and_tokens_only() -> None:
    store = filled()
    run_id = "run-1"
    store.start_run(run_id, NOW)
    report = run(store, FakeJudge())
    store.add_run_source(run_id, report.llm_source_report())  # type: ignore[arg-type]
    store.finish_run(run_id, NOW, "success")
    lines = [line.text for line in digest(store).run_lines]
    assert (
        "match:llm : candidates 3, évaluées 3, en cache 0, invalides 0, appels 3, "
        "jetons 1500 en entrée et 300 en sortie"
    ) in lines


def test_footer_marks_a_failed_llm_stage() -> None:
    line = SourceSummary("match:llm", 0, 0, 0, 0, 1, True, "JudgeUnavailable", (1, 0, 0)).text
    assert line.startswith("match:llm : candidates 0") and line.endswith(
        "(ÉCHEC : JudgeUnavailable)"
    )
    assert "JudgeUnavailable" in render_html(build_digest_with_failed_llm())


def build_digest_with_failed_llm():  # type: ignore[no-untyped-def]
    store = Store.open(":memory:")
    store.start_run("r", NOW)
    store.add_run_source(
        "r", SourceReport(source=LLM_SOURCE, errors=1, error_message="JudgeUnavailable: x")
    )
    store.finish_run("r", NOW, "success")
    return build_digest(store, DigestConfig(), NOW)


def test_title_when_only_the_llm_stage_failed_names_the_scoring() -> None:
    assert build_digest_with_failed_llm().title == "Vigie : alerte, le scoring a échoué"
