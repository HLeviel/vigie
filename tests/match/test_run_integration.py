"""``vigie radar run`` et ``vigie radar digest`` avec le module match activé."""

import sqlite3
from pathlib import Path
from typing import Any

import pytest

from tests.match.fakes import FakeEmbedder
from tests.match.helpers import PROFILE_EXAMPLE, make_offers
from tests.radar.factories import FakeSource
from tests.radar.test_cli_m2 import CONFIG, Env, env  # noqa: F401  (fixture)
from vigie.match.embedders import base
from vigie.match.embedders.base import EmbedderError
from vigie.radar import cli
from vigie.radar.http import SourceHttpError

MATCH_BLOCK = """
match:
  enabled: true
  profile: {profile}
  embeddings: {{ provider: local, model: fictif }}
  weights: {{ semantic: 0, lexical: 1 }}
  digest: {{ min_score: 40 }}
"""


@pytest.fixture
def menv(env: Env, monkeypatch: pytest.MonkeyPatch) -> Env:  # noqa: F811
    """``env`` avec match activé, un profil fictif et un faux fournisseur d'embeddings."""
    profile = env.tmp / "profile.yaml"
    profile.write_text(PROFILE_EXAMPLE.read_text())
    env.config.write_text(
        CONFIG.replace("require_any: [data]", "require_any: []")
        + MATCH_BLOCK.format(profile=profile)
    )
    env.profile = profile  # type: ignore[attr-defined]
    env.embedder = FakeEmbedder(name="local:fictif")  # type: ignore[attr-defined]
    monkeypatch.setitem(base.REGISTRY, "local", lambda model: env.embedder)  # type: ignore[attr-defined]
    return env


def source(monkeypatch: pytest.MonkeyPatch, e: Env) -> None:
    e.use_source(monkeypatch, FakeSource("adzuna", make_offers()))


def scored_rows(e: Env) -> int:
    with sqlite3.connect(e.db) as conn:
        return int(conn.execute("SELECT COUNT(*) FROM scores").fetchone()[0])


# --- cas nominal ----------------------------------------------------------------------------------


def test_digest_is_scored_sorted_and_below_threshold_offers_are_counted(
    menv: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    source(monkeypatch, menv)
    result = menv.run()
    assert result.exit_code == 0, result.output
    assert "match : à scorer=4 scorées=4 exclues=1 appels LLM=0" in result.output
    [digest] = menv.sender.digests
    assert [i.uid for i in digest.items] == ["adzuna:A"]
    assert digest.items[0].score is not None and digest.items[0].explanation()
    assert digest.below_threshold == 3
    assert (
        "offres à notifier : 0" in result.output
    )  # les offres sous le seuil sont comptées, soldées


def test_second_run_makes_no_new_embedding_call_and_sends_nothing(
    menv: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    source(monkeypatch, menv)
    menv.run()
    calls = len(menv.embedder.calls)  # type: ignore[attr-defined]
    second = menv.run()
    assert second.exit_code == 0
    assert "scorées=0" in second.output
    assert len(menv.embedder.calls) == calls  # type: ignore[attr-defined]
    assert len(menv.sender.digests) == 1


def test_run_sources_gets_a_match_line_that_does_not_change_the_run_status(
    menv: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    source(monkeypatch, menv)
    menv.run()
    with sqlite3.connect(menv.db) as conn:
        row = conn.execute(
            "SELECT fetched, new, filtered, errors FROM run_sources WHERE source = 'match'"
        ).fetchone()
        status = conn.execute("SELECT status FROM runs").fetchone()[0]
    assert row == (4, 4, 1, 0)
    assert status == "success"


def test_output_shows_counters_only(menv: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    source(monkeypatch, menv)
    out = menv.run().output
    for forbidden in ("Ingénieur IA", "Comptable", "python", "AI engineer", "Exemple SAS"):
        assert forbidden not in out


def test_match_disabled_leaves_the_flow_unchanged(
    env: Env,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env.config.write_text(CONFIG.replace("require_any: [data]", "require_any: []"))
    env.use_source(monkeypatch, FakeSource("adzuna", make_offers()))
    result = env.run()
    assert "match :" not in result.output
    assert scored_rows(env) == 0
    [digest] = env.sender.digests
    assert not digest.scored and len(digest.items) == 4


# --- pannes ---------------------------------------------------------------------------------------


class Down(FakeEmbedder):
    def embed(self, texts: list[str], kind: str) -> list[list[float]]:  # type: ignore[override]
        raise EmbedderError("fournisseur indisponible")


def test_provider_outage_still_sends_the_digest_by_date_and_exits_6(
    menv: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    menv.embedder = Down()  # type: ignore[attr-defined]
    source(monkeypatch, menv)
    result = menv.run()
    assert result.exit_code == 6, result.output
    assert "match : ÉCHEC (EmbedderError : fournisseur indisponible)" in result.output
    [digest] = menv.sender.digests
    assert len(digest.items) == 4 and not digest.scored
    assert digest.note
    assert "offres à notifier : 0" in result.output  # le digest est bien parti
    assert scored_rows(menv) == 0


def test_invalid_profile_exits_6_without_leaking_values(
    menv: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    menv.profile.write_text("summary: valeur-secrete\n")  # type: ignore[attr-defined]
    source(monkeypatch, menv)
    result = menv.run()
    assert result.exit_code == 6
    assert "ProfileError" in result.output and "valeur-secrete" not in result.output
    assert len(menv.sender.digests) == 1


def test_exit_6_takes_priority_over_a_failed_source(
    menv: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    menv.embedder = Down()  # type: ignore[attr-defined]
    failing = FakeSource("adzuna", make_offers(), error=SourceHttpError("adzuna", "boom"))
    menv.use_source(monkeypatch, failing)
    assert menv.run().exit_code == 6  # 6 > 3 (une source a échoué, après avoir livré des offres)


def test_exit_4_takes_priority_over_6(menv: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    menv.embedder = Down()  # type: ignore[attr-defined]
    menv.sender.fail = True
    source(monkeypatch, menv)
    assert menv.run().exit_code == 4  # 4 > 6


def test_exit_5_takes_priority_over_6(menv: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.radar.test_cli_m2 import with_s3
    from tests.radar.test_state import client_error

    with_s3(menv, monkeypatch)
    menv.s3.put_error = client_error("PreconditionFailed", "PutObject")
    menv.embedder = Down()  # type: ignore[attr-defined]
    source(monkeypatch, menv)
    result = menv.run()
    assert result.exit_code == 5


def test_a_later_healthy_run_scores_what_the_outage_left_unscored(
    menv: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    menv.embedder = Down()  # type: ignore[attr-defined]
    menv.sender.fail = True  # le digest ne part pas : les offres restent à notifier
    source(monkeypatch, menv)
    assert menv.run().exit_code == 4
    menv.embedder = FakeEmbedder(name="local:fictif")  # type: ignore[attr-defined]
    menv.sender.fail = False
    again = menv.run()
    assert again.exit_code == 0, again.output
    assert "scorées=4" in again.output
    [digest] = menv.sender.digests
    assert digest.scored and [i.uid for i in digest.items] == ["adzuna:A"]


# --- dry-run et commande digest -------------------------------------------------------------------


def test_dry_run_scores_in_memory_only(menv: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    source(monkeypatch, menv)
    result = menv.run("--dry-run")
    assert result.exit_code == 0, result.output
    assert "match : à scorer=4 scorées=4" in result.output
    assert not menv.db.exists()
    assert menv.sender.digests == []


def test_digest_command_uses_stored_scores(
    menv: Env, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source(monkeypatch, menv)
    menv.run("--no-email")
    out = tmp_path / "digest.md"
    from typer.testing import CliRunner

    result = CliRunner().invoke(
        cli.root,
        [
            "radar",
            "digest",
            "--config",
            str(menv.config),
            "--db",
            str(menv.db),
            "--output",
            str(out),
        ],
    )
    assert result.exit_code == 0, result.output
    text = out.read_text()
    assert "score " in text and "sous le seuil" in text


def test_digest_command_marks_below_threshold_offers_when_asked(
    menv: Env, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source(monkeypatch, menv)
    menv.run("--no-email")
    from typer.testing import CliRunner

    CliRunner().invoke(
        cli.root,
        [
            "radar",
            "digest",
            "--config",
            str(menv.config),
            "--db",
            str(menv.db),
            "--output",
            str(tmp_path / "d.md"),
            "--mark-notified",
        ],
    )
    with sqlite3.connect(menv.db) as conn:
        pending = conn.execute(
            "SELECT COUNT(*) FROM offers WHERE notified_at IS NULL AND filtered_reason IS NULL"
        ).fetchone()[0]
    assert pending == 0


def test_digest_command_with_an_unreadable_profile_falls_back_with_a_note(
    menv: Env, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source(monkeypatch, menv)
    menv.run("--no-email")
    menv.profile.write_text("pas un profil")  # type: ignore[attr-defined]
    out = tmp_path / "digest.md"
    from typer.testing import CliRunner

    result = CliRunner().invoke(
        cli.root,
        [
            "radar",
            "digest",
            "--config",
            str(menv.config),
            "--db",
            str(menv.db),
            "--output",
            str(out),
        ],
    )
    assert result.exit_code == 0
    assert "profil illisible" in out.read_text()


# --- étage LLM dans `radar run` -------------------------------------------------------------------


def with_llm(e: Env, monkeypatch: pytest.MonkeyPatch, judge: Any, extra: str = "") -> None:
    """Active l'étage LLM (modèle fictif) et remplace le client OpenRouter par ``judge``."""
    import vigie.match.judge  # noqa: F401  (inscrit les fournisseurs)
    from vigie.match.judge import base as judge_base

    e.config.write_text(
        e.config.read_text().replace(
            "  digest: { min_score: 40 }",
            "  digest: { min_score: 40 }\n"
            f"  llm: {{ enabled: true, model: fournisseur/modele{extra} }}",
        )
    )
    monkeypatch.setitem(judge_base.REGISTRY, "openrouter", lambda cfg, settings: judge)


def test_llm_stage_reorders_the_digest_and_reports_counters_only(
    menv: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.match.llm_helpers import FakeJudge, assessment

    judge = FakeJudge(script=[assessment(30, "passer"), assessment(95, "postuler"), assessment(10)])
    with_llm(menv, monkeypatch, judge)
    source(monkeypatch, menv)
    result = menv.run()
    assert result.exit_code == 0, result.output
    assert (
        "étage LLM : candidates=3 en cache=0 évaluées=3 invalides=0 jetons=1500/300"
        in result.output
    )
    assert "appels LLM=3" in result.output
    [digest] = menv.sender.digests
    assert [i.uid for i in digest.items] == ["adzuna:B"]  # 95 > seuil ; A passe à 30
    assert digest.items[0].recommendation == "postuler" and digest.items[0].score == 95
    assert "match:llm : candidates 3, évaluées 3" in "\n".join(
        line.text for line in digest.run_lines
    )
    for forbidden in ("Ingénieur IA", "Comptable", "Développeur", "postuler", "python"):
        assert forbidden not in result.output  # compteurs seulement
    with sqlite3.connect(menv.db) as conn:
        row = conn.execute(
            "SELECT fetched, new, duplicates, filtered, errors FROM run_sources "
            "WHERE source = 'match:llm'"
        ).fetchone()
        calls = conn.execute("SELECT COUNT(*), SUM(prompt_tokens) FROM llm_calls").fetchone()
        status = conn.execute("SELECT status FROM runs").fetchone()[0]
    assert row == (3, 3, 0, 0, 0) and calls == (3, 1500) and status == "success"


def test_llm_outage_exits_6_keeps_the_hybrid_order_and_the_digest_goes_out(
    menv: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.match.llm_helpers import FakeJudge
    from vigie.match.judge.base import JudgeUnavailable

    judge = FakeJudge(
        script=[JudgeUnavailable("OpenRouter : crédits insuffisants sur le compte (HTTP 402)")]
    )
    with_llm(menv, monkeypatch, judge)
    source(monkeypatch, menv)
    result = menv.run()
    assert result.exit_code == 6, result.output
    assert "ÉCHEC (JudgeUnavailable : OpenRouter : crédits insuffisants" in result.output
    assert "score hybride conservé" in result.output
    [digest] = menv.sender.digests
    assert digest.scored and [i.uid for i in digest.items] == ["adzuna:A"]  # tri hybride
    assert "Étage LLM ignoré" in digest.note
    assert "offres à notifier : 0" in result.output


def test_missing_openrouter_key_is_a_clear_llm_failure_not_a_crash(
    menv: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    import vigie.match.judge  # noqa: F401

    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    menv.config.write_text(
        menv.config.read_text().replace(
            "  digest: { min_score: 40 }",
            "  digest: { min_score: 40 }\n  llm: { enabled: true, model: fournisseur/modele }",
        )
    )
    source(monkeypatch, menv)
    result = menv.run()
    assert result.exit_code == 6
    assert "OPENROUTER_API_KEY" in result.output
    assert len(menv.sender.digests) == 1 and menv.sender.digests[0].scored


def test_budget_message_when_the_call_limit_is_reached(
    menv: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.match.llm_helpers import FakeJudge

    judge = FakeJudge()
    with_llm(menv, monkeypatch, judge, ", max_calls_per_run: 1")
    source(monkeypatch, menv)
    result = menv.run()
    assert result.exit_code == 0, result.output  # le budget n'est pas une panne
    assert "budget atteint" in result.output and len(judge.seen) == 1


def test_delivery_failure_takes_priority_over_an_llm_failure(
    menv: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.match.llm_helpers import FakeJudge
    from vigie.match.judge.base import JudgeUnavailable

    with_llm(menv, monkeypatch, FakeJudge(script=[JudgeUnavailable("x")]))
    menv.sender.fail = True
    source(monkeypatch, menv)
    assert menv.run().exit_code == 4  # 4 > 6


def test_dry_run_runs_the_llm_stage_on_the_memory_copy_only(
    menv: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.match.llm_helpers import FakeJudge

    judge = FakeJudge()
    with_llm(menv, monkeypatch, judge)
    source(monkeypatch, menv)
    result = menv.run("--dry-run")
    assert result.exit_code == 0, result.output
    assert "étage LLM : candidates=3" in result.output and len(judge.seen) == 3
    assert not menv.db.exists() and menv.sender.digests == []


def test_digest_command_shows_stored_llm_scores(
    menv: Env, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from typer.testing import CliRunner

    from tests.match.llm_helpers import FakeJudge, assessment

    with_llm(
        menv,
        monkeypatch,
        FakeJudge(script=[assessment(30, "passer"), assessment(95, "postuler"), assessment(10)]),
    )
    source(monkeypatch, menv)
    menv.run("--no-email")
    out = tmp_path / "digest.md"
    result = CliRunner().invoke(
        cli.root,
        [
            "radar",
            "digest",
            "--config",
            str(menv.config),
            "--db",
            str(menv.db),
            "--output",
            str(out),
        ],
    )
    assert result.exit_code == 0, result.output
    text = out.read_text()
    assert "recommandation : postuler" in text and "score 95/100" in text
