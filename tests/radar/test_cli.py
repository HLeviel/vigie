from pathlib import Path

import pytest
from typer.testing import CliRunner

from tests.radar.factories import (
    APP_ID,
    APP_KEY,
    FakeSource,
    factory,
    make_offer,
    make_settings,
)
from vigie.radar import cli
from vigie.radar import sources as source_registry
from vigie.radar.http import SourceHttpError
from vigie.settings import Settings

runner = CliRunner()

CONFIG = """
sources:
  adzuna: { enabled: true }
  email_alerts: { enabled: true }
  france_travail: { enabled: true }
searches:
  - { name: confidentiel, keywords: [python-secret], departments: ['69'] }
filters:
  require_any: [python, data]
"""


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    monkeypatch.chdir(tmp_path)
    config = tmp_path / "radar.yaml"
    config.write_text(CONFIG)
    monkeypatch.setattr(cli, "_settings", make_settings)
    return {"config": config, "db": tmp_path / "radar.db"}


def invoke(env: dict[str, Path], *args: str) -> object:
    return runner.invoke(cli.root, ["radar", *args])


def set_source(monkeypatch: pytest.MonkeyPatch, source: FakeSource, name: str = "adzuna") -> None:
    monkeypatch.setitem(source_registry.REGISTRY, name, factory(source))


def offers(n: int) -> list[object]:
    return [make_offer(source_id=str(i), title=f"Data Engineer {i}") for i in range(n)]


def args(env: dict[str, Path], *extra: str) -> list[str]:
    return ["--config", str(env["config"]), *extra]


# --- check ------------------------------------------------------------------------------------


def test_check_ok_and_statuses(env: dict[str, Path]) -> None:
    result = runner.invoke(cli.root, ["radar", "check", *args(env)])
    assert result.exit_code == 0, result.output
    assert "configuration valide : 1 recherche" in result.output
    assert "source adzuna : active" in result.output
    assert "source email_alerts : pas encore implémentée" in result.output
    assert "source france_travail : en attente d'accès" in result.output
    assert "secrets : présents" in result.output


def test_check_names_missing_variable_without_values(
    env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        cli,
        "_settings",
        lambda: Settings(_env_file=None, adzuna_app_id="valeur-cachee"),  # type: ignore[call-arg]
    )
    result = runner.invoke(cli.root, ["radar", "check", *args(env)])
    assert result.exit_code == 1
    assert "ADZUNA_APP_KEY" in result.output
    assert "valeur-cachee" not in result.output
    assert APP_KEY not in result.output


def test_check_france_travail_with_credentials_is_active(
    env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        cli, "_settings", lambda: make_settings(ft_client_id="i", ft_client_secret="s")
    )
    result = runner.invoke(cli.root, ["radar", "check", *args(env)])
    assert "source france_travail : active" in result.output


def test_check_with_invalid_config_exits_1_without_values(env: dict[str, Path]) -> None:
    env["config"].write_text(
        "searches:\n  - { name: x, keywords: [valeur-secrete], mot_inconnu: valeur-secrete }"
    )
    result = runner.invoke(cli.root, ["radar", "check", *args(env)])
    assert result.exit_code == 1
    assert "valeur-secrete" not in result.output


def test_check_without_any_config_exits_1(env: dict[str, Path]) -> None:
    result = runner.invoke(cli.root, ["radar", "check"])
    assert result.exit_code == 1
    assert "Aucune configuration" in result.output


def test_check_makes_no_network_call(env: dict[str, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    import socket

    def refuse(*a: object, **k: object) -> None:
        raise AssertionError("appel réseau interdit")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    assert runner.invoke(cli.root, ["radar", "check", *args(env)]).exit_code == 0


# --- run --------------------------------------------------------------------------------------


def test_run_then_second_run_adds_nothing(
    env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    set_source(monkeypatch, FakeSource("adzuna", offers(3)))
    db = ["--db", str(env["db"])]
    first = runner.invoke(cli.root, ["radar", "run", *args(env), *db])
    assert first.exit_code == 0, first.output
    assert "adzuna : récupérées=3 nouvelles=3 filtrées=0 doublons=0 erreurs=0" in first.output
    assert "offres à notifier : 3" in first.output
    second = runner.invoke(cli.root, ["radar", "run", *args(env), *db])
    assert "récupérées=3 nouvelles=0 filtrées=0 doublons=3" in second.output
    assert second.exit_code == 0


def test_run_output_has_only_counters(
    env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    set_source(
        monkeypatch,
        FakeSource(
            "adzuna", [make_offer(title="Titre-Confidentiel", company="Entreprise-Confidentielle")]
        ),
    )
    result = runner.invoke(cli.root, ["radar", "run", *args(env), "--db", str(env["db"])])
    for forbidden in ("Confidentiel", "python-secret", "confidentiel", APP_KEY, APP_ID):
        assert forbidden not in result.output


def test_dry_run_writes_nothing(env: dict[str, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    set_source(monkeypatch, FakeSource("adzuna", offers(2)))
    result = runner.invoke(
        cli.root, ["radar", "run", *args(env), "--dry-run", "--db", str(env["db"])]
    )
    assert result.exit_code == 0
    assert "nouvelles=2" in result.output
    assert "dry-run : rien n'a été écrit" in result.output
    assert not env["db"].exists()


def test_dry_run_does_not_modify_an_existing_database(
    env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    set_source(monkeypatch, FakeSource("adzuna", offers(1)))
    db = ["--db", str(env["db"])]
    runner.invoke(cli.root, ["radar", "run", *args(env), *db])
    before = env["db"].read_bytes()
    set_source(monkeypatch, FakeSource("adzuna", offers(5)))
    result = runner.invoke(cli.root, ["radar", "run", *args(env), "--dry-run", *db])
    assert "nouvelles=4 " in result.output
    assert env["db"].read_bytes() == before


def test_exit_code_3_when_a_source_fails(
    env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    env["config"].write_text(
        CONFIG.replace("email_alerts: { enabled: true }", "email_alerts: { enabled: false }")
    )
    monkeypatch.setattr(
        cli, "_settings", lambda: make_settings(ft_client_id="i", ft_client_secret="s")
    )
    set_source(
        monkeypatch, FakeSource("adzuna", [], error=SourceHttpError("adzuna : HTTP 500", 500))
    )
    set_source(monkeypatch, FakeSource("ft", offers(2)), name="france_travail")
    result = runner.invoke(cli.root, ["radar", "run", *args(env), "--db", str(env["db"])])
    assert result.exit_code == 3
    assert "ÉCHEC" in result.output
    assert "nouvelles=2" in result.output  # l'autre source a produit ses offres


def test_exit_code_2_when_all_sources_fail(
    env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    set_source(monkeypatch, FakeSource("adzuna", [], error=RuntimeError("x")))
    result = runner.invoke(cli.root, ["radar", "run", *args(env), "--db", str(env["db"])])
    assert result.exit_code == 2


def test_run_missing_secret_exits_1(env: dict[str, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "_settings", lambda: Settings(_env_file=None))  # type: ignore[call-arg]
    result = runner.invoke(cli.root, ["radar", "run", *args(env), "--db", str(env["db"])])
    assert result.exit_code == 1
    assert "ADZUNA_APP_ID" in result.output
    assert not env["db"].exists()


def test_run_source_option(env: dict[str, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    set_source(monkeypatch, FakeSource("adzuna", offers(1)))
    ok = runner.invoke(
        cli.root, ["radar", "run", *args(env), "--source", "adzuna", "--db", str(env["db"])]
    )
    assert ok.exit_code == 0
    bad = runner.invoke(
        cli.root, ["radar", "run", *args(env), "--source", "nope", "--db", str(env["db"])]
    )
    assert bad.exit_code == 1


def test_run_since_option(env: dict[str, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[object] = []

    class Spy(FakeSource):
        def fetch(self, query: object, since: object) -> object:
            seen.append(since)
            return iter(())

    set_source(monkeypatch, Spy("adzuna", []))
    result = runner.invoke(
        cli.root, ["radar", "run", *args(env), "--since", "2026-11-01", "--db", str(env["db"])]
    )
    assert result.exit_code == 0, result.output
    assert str(seen[0]).startswith("2026-11-01 00:00:00")
    bad = runner.invoke(cli.root, ["radar", "run", *args(env), "--since", "demain"])
    assert bad.exit_code != 0


# --- stats ------------------------------------------------------------------------------------


def test_stats(env: dict[str, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    empty = runner.invoke(cli.root, ["radar", "stats", "--db", str(env["db"])])
    assert "aucune base" in empty.output
    set_source(monkeypatch, FakeSource("adzuna", offers(2)))
    runner.invoke(cli.root, ["radar", "run", *args(env), "--db", str(env["db"])])
    result = runner.invoke(cli.root, ["radar", "stats", "--db", str(env["db"])])
    assert result.exit_code == 0
    assert "success" in result.output
    assert "adzuna : récupérées=2 nouvelles=2" in result.output
    assert "offres à notifier : 2" in result.output
    assert "Data Engineer" not in result.output  # compteurs seulement


def test_stats_with_failed_source(env: dict[str, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    set_source(monkeypatch, FakeSource("adzuna", [], error=RuntimeError("x")))
    runner.invoke(cli.root, ["radar", "run", *args(env), "--db", str(env["db"])])
    result = runner.invoke(cli.root, ["radar", "stats", "--db", str(env["db"])])
    assert "failed" in result.output
    assert "ÉCHEC" in result.output


def test_no_args_shows_help() -> None:
    result = runner.invoke(cli.root, [])
    assert "radar" in result.output


def test_db_path_resolution(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    configured = make_settings(vigie_db_path=tmp_path / "x.db")
    assert cli._db_path(None, configured) == tmp_path / "x.db"
    assert cli._db_path(tmp_path / "y.db", configured) == tmp_path / "y.db"
    assert cli._db_path(None, make_settings()).name == "radar.db"


def test_check_reports_unsupported_ats_platform_by_count(env: dict[str, Path]) -> None:
    env["config"].write_text(
        CONFIG.replace("sources:\n", "sources:\n  ats: { enabled: true }\n", 1)
        + "ats_companies:\n"
        + "  - { name: Entreprise-Confidentielle, platform: workable, token: tok-confidentiel }\n"
        + "  - { name: Autre, platform: lever, token: autre }\n"
    )
    result = runner.invoke(cli.root, ["radar", "check", *args(env)])
    assert result.exit_code == 0, result.output
    assert "1 entreprise(s) sur une plateforme non prise en charge (workable)" in result.output
    assert "Confidentielle" not in result.output
    assert "confidentiel" not in result.output


def test_check_ats_without_companies(env: dict[str, Path]) -> None:
    env["config"].write_text(
        CONFIG.replace("sources:\n", "sources:\n  ats: { enabled: true }\n", 1)
    )
    result = runner.invoke(cli.root, ["radar", "check", *args(env)])
    assert "aucune entreprise dans ats_companies" in result.output
