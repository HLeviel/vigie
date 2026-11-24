from pathlib import Path

import pytest

from vigie.radar.config import ConfigError, load_config_text, resolve_config

EXAMPLE = Path(__file__).parents[2] / "config" / "radar.example.yaml"


def test_example_config_is_valid() -> None:
    config = load_config_text(EXAMPLE.read_text())
    assert config.sources.enabled() == ["adzuna", "ats", "email_alerts"]
    assert not config.sources.france_travail.enabled  # en attente d'accès
    assert config.searches[0].keywords == ["data engineer", "machine learning", "IA"]
    assert config.digest.format == "html"
    assert len(config.ats_companies) == 2


def test_defaults() -> None:
    config = load_config_text("searches:\n  - { name: x, keywords: [a], departments: ['69'] }")
    assert config.sources.enabled() == []
    assert config.filters.exclude_title == []
    assert config.digest.max_items == 50


@pytest.mark.parametrize(
    "text",
    [
        "searches: []",
        "{}",
        "searches:\n  - { name: x, keywords: [] }",
        "searches:\n  - { name: x, keywords: [a], departement: ['69'] }",  # faute de frappe
        "searches:\n  - { name: x, keywords: [a], departments: ['69'] }\nunknown: 1",
        "searches:\n  - { name: x, keywords: [a], departments: ['69'] }\ndigest: { format: pdf }",
        "searches:\n  - { name: x, keywords: [a], departments: ['69'] }\ndigest: { max_items: 0 }",
        "searches:\n  - { name: x, keywords: [a], contract_types: [CDX] }",
        "searches:\n  - { name: x, keywords: [a] }\n"
        "ats_companies:\n  - { name: A, platform: nope, token: t }",
        "- liste\n- au lieu d'un dictionnaire",
        "searches: [unclosed",
    ],
)
def test_invalid_configs_raise(text: str) -> None:
    with pytest.raises(ConfigError):
        load_config_text(text)


def test_allowed_senders_must_name_a_known_parser() -> None:
    base = "searches:\n  - { name: x, keywords: [a], departments: ['69'] }\n"
    ok = base + "email_alerts:\n  allowed_senders:\n    apec: ['@apec.fr']\n"
    assert load_config_text(ok).email_alerts.allowed_senders == {"apec": ["@apec.fr"]}
    bad = base + "email_alerts:\n  allowed_senders:\n    inconnu: ['@x.fr']\n"
    with pytest.raises(ConfigError) as exc:
        load_config_text(bad)
    assert "expéditeur inconnu" in str(exc.value)
    assert "inconnu:" not in str(exc.value)


def test_errors_never_echo_values() -> None:
    text = (
        "searches:\n  - { name: confidentiel, keywords: [valeur-secrete], "
        "departments: ['69'], mot_inconnu: valeur-secrete-2 }\ndigest: { format: valeur-secrete-3 }"
    )
    with pytest.raises(ConfigError) as exc:
        load_config_text(text)
    message = str(exc.value)
    assert "valeur-secrete" not in message
    assert "confidentiel" not in message
    assert "searches.0" in message  # le chemin, lui, est indiqué


def test_yaml_error_has_position_but_no_content() -> None:
    with pytest.raises(ConfigError) as exc:
        load_config_text("searches: [valeur-secrete")
    assert "ligne" in str(exc.value)
    assert "valeur-secrete" not in str(exc.value)


def test_resolve_order(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    file_cfg = "searches:\n  - { name: depuis-fichier, keywords: [a], departments: ['69'] }"
    default_cfg = "searches:\n  - { name: par-defaut, keywords: [a], departments: ['69'] }"
    path = tmp_path / "x.yaml"
    path.write_text(file_cfg)
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "radar.yaml").write_text(default_cfg)

    assert resolve_config(path).searches[0].name == "depuis-fichier"
    assert resolve_config(None).searches[0].name == "par-defaut"


def test_resolve_without_any_source(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ConfigError) as exc:
        resolve_config(None)
    assert "--config" in str(exc.value)
    assert "VIGIE_RADAR_CONFIG" not in str(exc.value)  # plus un secret d'environnement


def test_the_real_config_is_never_an_environment_secret() -> None:
    from vigie.settings import Settings

    assert "vigie_radar_config" not in Settings.model_fields


def test_unreadable_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        resolve_config(tmp_path / "absent.yaml")
