"""Évaluation des variantes `hybrid+llm:<modèle>` (au moins deux modèles OpenRouter)."""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from tests.match.cli_helpers import invoke
from tests.match.fakes import FakeEmbedder
from tests.match.helpers import PROFILE_EXAMPLE, match_config
from tests.match.llm_helpers import MODEL, OTHER_MODEL, FakeJudge, assessment
from vigie.match.evaluation.dataset import EvalItem, load_json
from vigie.match.evaluation.report import render_markdown
from vigie.match.evaluation.runner import evaluate
from vigie.match.judge.base import JudgeUnavailable
from vigie.match.profile import load_profile_file
from vigie.match.store import EmbeddingStore, LabelStore, LlmCallStore
from vigie.radar.store import Store

FIXTURE = Path(__file__).parent / "fixtures" / "eval_fictif.json"
NOW = datetime(2026, 11, 27, 6, 0, tzinfo=UTC)
PROFILE = load_profile_file(PROFILE_EXAMPLE)


@pytest.fixture(scope="module")
def items() -> list[EvalItem]:
    return load_json(FIXTURE)


class KeywordJudge(FakeJudge):
    """Juge « malin » : note haut les descriptions qui parlent de Python (les offres pertinentes
    du jeu fictif)."""

    def assess(self, profile, offer):  # type: ignore[no-untyped-def]
        self.default = assessment(95 if "python" in offer.description.lower() else 5)
        return super().assess(profile, offer)


def run(items: list[EvalItem], judges, store: Store | None = None):  # type: ignore[no-untyped-def]
    db = store or Store.open(":memory:")
    return db, evaluate(
        PROFILE,
        match_config(PROFILE_EXAMPLE),
        items,
        [FakeEmbedder(name="local:modele-a")],
        EmbeddingStore(db),
        draws=40,
        now=NOW,
        judges=judges,
        db=db,
    )


@pytest.fixture(scope="module")
def two_models(items: list[EvalItem]):  # type: ignore[no-untyped-def]
    return run(
        items,
        [KeywordJudge(name=f"openrouter:{MODEL}"), KeywordJudge(name=f"openrouter:{OTHER_MODEL}")],
    )


def test_two_openrouter_models_give_two_variants_compared_with_hybrid(two_models) -> None:  # type: ignore[no-untyped-def]
    db, result = two_models
    names = [r.name for r in result.variants]
    assert f"hybrid+llm:{MODEL}" in names and f"hybrid+llm:{OTHER_MODEL}" in names
    assert names[-2:] == [f"hybrid+llm:{MODEL}", f"hybrid+llm:{OTHER_MODEL}"]
    hybrid = next(n for n in names if n.startswith("hybrid:"))
    # chaque étage doit prouver un gain sur le précédent : LLM contre hybride, sur les 2 métriques
    llm_vs_hybrid = [c for c in result.targeted if c.a.startswith("hybrid+llm:") and c.b == hybrid]
    assert len(llm_vs_hybrid) == 4  # 2 modèles × (precision@10, nDCG@10)
    assert all(c.verdict in {"gain", "perte", "écart non significatif"} for c in llm_vs_hybrid)
    assert set(result.vs_date) >= {f"hybrid+llm:{MODEL}", f"hybrid+llm:{OTHER_MODEL}"}
    by_name = {r.name: r for r in result.variants}
    variant = by_name[f"hybrid+llm:{MODEL}"]
    assert 0 < variant.llm_candidates <= 10 and variant.llm_assessed == variant.llm_candidates
    assert variant.llm_calls == variant.llm_candidates  # un appel par offre, jeu valide
    assert variant.prompt_tokens == 500 * variant.llm_calls
    assert variant.seconds_per_call is not None and variant.metrics["ndcg@10"] >= 0
    assert LlmCallStore(db).totals("eval-20261127")[0] == variant.llm_calls * 2


def test_llm_variant_scores_only_the_top_n_and_keeps_hybrid_for_the_rest(
    items: list[EvalItem],
) -> None:
    judge = FakeJudge(default=assessment(1, "passer"))
    _, result = run(items, [judge])
    variant = next(r for r in result.variants if r.name.startswith("hybrid+llm:"))
    assert len(judge.seen) == variant.llm_calls <= 10  # top_n = 10 par défaut


def test_a_smart_judge_does_not_degrade_the_top_of_the_ranking(two_models) -> None:  # type: ignore[no-untyped-def]
    _, result = two_models
    by_name = {r.name: r for r in result.variants}
    llm = by_name[f"hybrid+llm:{MODEL}"].metrics["precision@10"]
    hybrid = next(r for n, r in by_name.items() if n.startswith("hybrid:")).metrics["precision@10"]
    assert llm >= hybrid


def test_second_evaluation_uses_the_cache_and_reports_nothing_measured(
    items: list[EvalItem],
) -> None:
    db, first = run(items, [FakeJudge()])
    again_judge = FakeJudge()
    _, second = run(items, [again_judge], store=db)
    assert again_judge.seen == []  # évaluations en cache : aucun appel payant
    variant = next(r for r in second.variants if r.name.startswith("hybrid+llm:"))
    assert variant.llm_calls == 0 and variant.seconds_per_call is None
    assert variant.llm_assessed == variant.llm_candidates > 0
    assert "non mesuré (évaluations déjà en cache)" in render_markdown(second)
    assert first.variants[-1].llm_calls > 0


def test_an_unavailable_model_is_noted_and_the_other_one_is_still_evaluated(
    items: list[EvalItem],
) -> None:
    bad = FakeJudge(
        name=f"openrouter:{OTHER_MODEL}",
        script=[JudgeUnavailable("OpenRouter n'a pas pu traiter la requête (HTTP 404)")],
    )
    _, result = run(items, [FakeJudge(), bad])
    names = [r.name for r in result.variants]
    assert f"hybrid+llm:{MODEL}" in names and f"hybrid+llm:{OTHER_MODEL}" not in names
    assert any(OTHER_MODEL in note and "ignorée" in note for note in result.notes)
    assert "ignorée" in render_markdown(result)


def test_budget_reached_is_noted(items: list[EvalItem]) -> None:
    db = Store.open(":memory:")
    config = match_config(PROFILE_EXAMPLE, "  llm: { max_calls_per_run: 2 }\n")
    result = evaluate(
        PROFILE, config, items, [FakeEmbedder(name="local:modele-a")], EmbeddingStore(db),
        draws=20, now=NOW, judges=[FakeJudge()], db=db,
    )  # fmt: skip
    assert any("budget atteint" in note for note in result.notes)


def test_judges_need_the_database(items: list[EvalItem]) -> None:
    with pytest.raises(ValueError):
        evaluate(
            PROFILE,
            match_config(PROFILE_EXAMPLE),
            items,
            [FakeEmbedder(name="local:modele-a")],
            EmbeddingStore(Store.open(":memory:")),
            judges=[FakeJudge()],
        )


def test_without_judges_the_report_is_unchanged_and_mentions_the_requirement(
    items: list[EvalItem],
) -> None:
    _, result = run(items, [])
    assert not any(r.name.startswith("hybrid+llm:") for r in result.variants)
    text = render_markdown(result)
    assert "Étage LLM (jetons et temps)" not in text and "OpenRouter" in text


def test_report_has_the_llm_section_without_offer_titles(items: list[EvalItem], two_models) -> None:  # type: ignore[no-untyped-def]
    _, result = two_models
    text = render_markdown(result)
    assert "## Étage LLM (jetons et temps)" in text
    assert "500 / 100" in text  # jetons moyens par appel (entrée / sortie)
    assert "dépasse `hybrid` au-delà de l'intervalle de confiance" in text
    assert "Coût par exécution" in text and "tarif" in text  # jamais de prix inventé
    for item in items:
        assert item.offer.title not in text and (item.offer.company or "") not in text


# --- commande ------------------------------------------------------------------------------------


def labelled(setup: dict[str, Path], items: list[EvalItem]) -> None:
    with Store.open(setup["db"]) as store:
        for item in items:
            store.insert_offer(item.offer, NOW)
            LabelStore(store).put(item.uid, PROFILE.profile_hash, item.label, NOW)


def llm_config(setup: dict[str, Path], extra_models: str = "") -> None:
    text = (
        setup["config"]
        .read_text()
        .replace(
            "  evaluation:\n    candidates:",
            f"  evaluation:\n    llm_models: [{extra_models}]\n    candidates:",
        )
    )
    setup["config"].write_text(text + f"  llm: {{ enabled: true, model: {MODEL} }}\n")


@pytest.fixture
def fake_openrouter(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Remplace le client OpenRouter par un faux juge ; renvoie la liste des modèles construits."""
    import vigie.match.judge  # noqa: F401
    from vigie.match.judge import base

    built: list[str] = []

    def factory(config, settings):  # type: ignore[no-untyped-def]
        built.append(config.model)
        return FakeJudge(name=f"openrouter:{config.model}")

    monkeypatch.setitem(base.REGISTRY, "openrouter", factory)
    return built


def test_eval_command_builds_one_judge_per_configured_model_without_duplicates(
    setup: dict[str, Path],
    items: list[EvalItem],
    fake_openrouter: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-valeur-secrete")
    labelled(setup, items)
    llm_config(setup, f'"{OTHER_MODEL}", "{MODEL}"')  # MODEL est déjà `llm.model` : dédoublonné
    out = setup["tmp"] / "rapport.md"
    result = invoke(setup, "eval", "--output", str(out))
    assert result.exit_code == 0, result.output
    assert fake_openrouter == [MODEL, OTHER_MODEL]
    report = out.read_text()
    assert f"`hybrid+llm:{MODEL}`" in report and f"`hybrid+llm:{OTHER_MODEL}`" in report
    assert "sk-or-valeur-secrete" not in report + result.output


def test_eval_command_no_llm_flag_skips_every_paid_call(
    setup: dict[str, Path],
    items: list[EvalItem],
    fake_openrouter: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-valeur-secrete")
    labelled(setup, items)
    llm_config(setup)
    result = invoke(setup, "eval", "--no-llm", "--output", str(setup["tmp"] / "r.md"))
    assert result.exit_code == 0 and fake_openrouter == []
    assert "hybrid+llm" not in (setup["tmp"] / "r.md").read_text().split("## Limites")[0]


def test_eval_command_without_a_key_skips_the_llm_variants_with_a_message(
    setup: dict[str, Path],
    items: list[EvalItem],
    fake_openrouter: list[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.chdir(tmp_path)
    labelled(setup, items)
    llm_config(setup)
    result = invoke(setup, "eval", "--output", str(setup["tmp"] / "r.md"))
    assert result.exit_code == 0, result.output
    assert "variantes hybrid+llm ignorées : OPENROUTER_API_KEY absente" in result.output
    assert fake_openrouter == []


def test_eval_command_without_any_llm_model_does_not_mention_the_key(
    setup: dict[str, Path], items: list[EvalItem], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    labelled(setup, items)
    result = invoke(setup, "eval", "--output", str(setup["tmp"] / "r.md"))
    assert result.exit_code == 0 and "OPENROUTER" not in result.output


def test_eval_command_reports_a_judge_construction_error(
    setup: dict[str, Path],
    items: list[EvalItem],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import vigie.match.judge  # noqa: F401
    from vigie.match.judge import base

    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-valeur-secrete")

    def broken(config, settings):  # type: ignore[no-untyped-def]
        raise JudgeUnavailable("fournisseur LLM indisponible")

    monkeypatch.setitem(base.REGISTRY, "openrouter", broken)
    labelled(setup, items)
    llm_config(setup)
    result = invoke(setup, "eval")
    assert result.exit_code == 1 and "étage LLM : fournisseur LLM indisponible" in result.output
