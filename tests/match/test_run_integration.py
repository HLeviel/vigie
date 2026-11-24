"""``vigie radar run`` et ``vigie radar digest`` avec le module match activé."""

import sqlite3
from pathlib import Path

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
