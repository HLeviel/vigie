import os
import stat
from pathlib import Path

import pytest
from typer.testing import CliRunner

from tests.radar.factories import FakeSource, factory, make_offer, make_settings
from vigie.radar import cli
from vigie.radar import sources as source_registry
from vigie.radar.http import SourceHttpError

runner = CliRunner()

CONFIG = """
sources:
  adzuna: { enabled: true }
searches:
  - { name: demo, keywords: [data], departments: ['69'] }
filters:
  require_any: [data]
digest:
  max_items: 2
  format: html
"""


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    monkeypatch.chdir(tmp_path)
    config = tmp_path / "radar.yaml"
    config.write_text(CONFIG)
    monkeypatch.setattr(cli, "_settings", make_settings)
    return {"config": config, "db": tmp_path / "radar.db", "dir": tmp_path}


def collect(env: dict[str, Path], monkeypatch: pytest.MonkeyPatch, source: FakeSource) -> None:
    monkeypatch.setitem(source_registry.REGISTRY, "adzuna", factory(source))
    runner.invoke(
        cli.root, ["radar", "run", "--config", str(env["config"]), "--db", str(env["db"])]
    )


def offers(n: int) -> list[object]:
    return [make_offer(source_id=str(i), title=f"Data Engineer {i}") for i in range(n)]


def digest(env: dict[str, Path], *extra: str) -> object:
    return runner.invoke(
        cli.root,
        ["radar", "digest", "--config", str(env["config"]), "--db", str(env["db"]), *extra],
    )


def test_writes_the_default_file_in_private_mode(
    env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    collect(env, monkeypatch, FakeSource("adzuna", offers(2)))
    result = digest(env)
    assert result.exit_code == 0, result.output  # type: ignore[attr-defined]
    text = (env["dir"] / "digest.html").read_text()
    assert text.startswith("<!doctype html>")
    assert "Data Engineer 0" in text
    assert "digest : 2 offre(s) affichée(s) sur 2 à notifier" in result.output  # type: ignore[attr-defined]
    if os.name == "posix":
        assert stat.S_IMODE((env["dir"] / "digest.html").stat().st_mode) == 0o600


def test_console_output_contains_only_counters(
    env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    collect(env, monkeypatch, FakeSource("adzuna", [make_offer(title="Titre-Confidentiel")]))
    result = digest(env)
    assert "Confidentiel" not in result.output  # type: ignore[attr-defined]
    assert "Acme" not in result.output  # type: ignore[attr-defined]


def test_existing_file_gets_private_mode(
    env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    collect(env, monkeypatch, FakeSource("adzuna", offers(1)))
    target = env["dir"] / "digest.html"
    target.write_text("ancien")
    target.chmod(0o644)
    digest(env)
    assert "Data Engineer" in target.read_text()
    if os.name == "posix":
        assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_markdown_format_and_custom_output(
    env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    env["config"].write_text(CONFIG.replace("format: html", "format: markdown"))
    collect(env, monkeypatch, FakeSource("adzuna", offers(1)))
    digest(env)
    assert (env["dir"] / "digest.md").read_text().startswith("# Vigie : 1 nouvelle offre")
    out = env["dir"] / "mes-offres.md"
    digest(env, "--output", str(out))
    assert out.exists()


def test_output_dash_prints_the_digest(
    env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    collect(env, monkeypatch, FakeSource("adzuna", offers(1)))
    result = digest(env, "--output", "-")
    assert "Data Engineer 0" in result.output  # type: ignore[attr-defined]
    assert not (env["dir"] / "digest.html").exists()


def test_nothing_to_report_writes_nothing(
    env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    collect(env, monkeypatch, FakeSource("adzuna", []))
    result = digest(env)
    assert result.exit_code == 0  # type: ignore[attr-defined]
    assert "rien à écrire" in result.output  # type: ignore[attr-defined]
    assert not (env["dir"] / "digest.html").exists()


def test_failure_without_offers_writes_a_short_alert(
    env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    collect(
        env, monkeypatch, FakeSource("adzuna", [], error=SourceHttpError("adzuna : HTTP 500", 500))
    )
    result = digest(env)
    text = (env["dir"] / "digest.html").read_text()
    assert "alerte, une source a échoué" in text
    assert "ÉCHEC : SourceHttpError" in text
    assert "HTTP 500" not in text
    assert "alerte" in result.output  # type: ignore[attr-defined]


def test_mark_notified_marks_only_displayed_offers(
    env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    collect(env, monkeypatch, FakeSource("adzuna", offers(3)))  # max_items = 2
    first = digest(env, "--mark-notified")
    assert "2 offre(s) affichée(s) sur 3" in first.output  # type: ignore[attr-defined]
    second = digest(env)
    assert "1 offre(s) affichée(s) sur 1" in second.output  # type: ignore[attr-defined]
    third = digest(env, "--mark-notified")
    assert "1 offre(s) affichée(s) sur 1" in third.output  # type: ignore[attr-defined]
    assert "rien à écrire" in digest(env).output  # type: ignore[attr-defined]


def test_default_does_not_mark_anything(
    env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    collect(env, monkeypatch, FakeSource("adzuna", offers(2)))
    digest(env)
    again = digest(env)
    assert "2 offre(s) affichée(s)" in again.output  # type: ignore[attr-defined]


def test_missing_database_exits_1(env: dict[str, Path]) -> None:
    result = digest(env)
    assert result.exit_code == 1  # type: ignore[attr-defined]
    assert "aucune base" in result.output  # type: ignore[attr-defined]


def test_invalid_config_exits_1(env: dict[str, Path]) -> None:
    env["config"].write_text("searches: []")
    result = digest(env)
    assert result.exit_code == 1  # type: ignore[attr-defined]
