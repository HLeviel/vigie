from pathlib import Path

import pytest
from typer.testing import CliRunner

from vigie.match.config import MatchConfig
from vigie.radar.cli import root
from vigie.radar.config import ConfigError, load_config_text

EXAMPLE_RADAR = Path(__file__).parents[2] / "config" / "radar.example.yaml"
EXAMPLE_PROFILE = Path(__file__).parents[2] / "config" / "profile.example.yaml"
runner = CliRunner()

SEARCHES = "searches:\n  - { name: s, keywords: [a], departments: ['69'] }\n"


def config_text(match_block: str) -> str:
    return SEARCHES + match_block


def test_radar_config_without_match_section_is_unchanged() -> None:
    assert load_config_text(SEARCHES).match is None


def test_commented_example_block_is_valid_once_uncommented() -> None:
    lines = EXAMPLE_RADAR.read_text().splitlines()
    start = lines.index("# match:")
    block = "\n".join(line.removeprefix("# ") for line in lines[start:] if line.startswith("#"))
    cfg = load_config_text(SEARCHES + block.replace("<id du modèle>", "modele-fictif"))
    assert cfg.match is not None
    assert cfg.match.embeddings.model == "modele-fictif"
    assert cfg.match.weights.semantic == 0.7


def test_model_is_mandatory() -> None:
    with pytest.raises(ConfigError):
        load_config_text(config_text("match: { embeddings: { provider: local } }\n"))
    with pytest.raises(ConfigError):
        load_config_text(config_text("match: { enabled: true }\n"))


@pytest.mark.parametrize(
    "block",
    [
        "match: { embeddings: { provider: autre, model: m } }",
        "match: { embeddings: { provider: local, model: '' } }",
        "match: { embeddings: { provider: local, model: m }, weights: { semantic: -1 } }",
        "match: { embeddings: { provider: local, model: m }, weights: { lexical: 0 } }".replace(
            "lexical: 0 }", "lexical: 0, semantic: 0 }"
        ),
        "match: { embeddings: { provider: local, model: m }, top_k_profile: 0 }",
        "match: { embeddings: { provider: local, model: m }, penalties: { remote: 101 } }",
        "match: { embeddings: { provider: local, model: m }, digest: { min_score: 120 } }",
        "match: { embeddings: { provider: local, model: m }, llm: { enabled: true } }",
        "match: { embeddings: { provider: local, model: m }, inconnu: 1 }",
    ],
)
def test_invalid_match_sections(block: str) -> None:
    with pytest.raises(ConfigError):
        load_config_text(config_text(block + "\n"))


def test_defaults() -> None:
    cfg = MatchConfig.model_validate({"embeddings": {"provider": "bedrock", "model": "m"}})
    assert cfg.enabled is False
    assert cfg.profile == Path("profile/profile.yaml")
    assert (cfg.weights.semantic, cfg.weights.lexical) == (0.7, 0.3)
    assert (cfg.penalties.remote, cfg.penalties.salary, cfg.penalties.avoid) == (15, 15, 20)
    assert cfg.top_k_profile == 3
    assert cfg.digest.min_score == 40
    assert cfg.llm.enabled is False
    assert (cfg.llm.top_n, cfg.llm.max_calls_per_run) == (10, 15)


def write_setup(tmp_path: Path, match: str, profile: Path | None = EXAMPLE_PROFILE) -> Path:
    cfg = tmp_path / "radar.yaml"
    profile_line = f"  profile: {profile}\n" if profile else ""
    cfg.write_text(SEARCHES + "match:\n" + profile_line + match)
    return cfg


EMB = "  embeddings: { provider: local, model: modele-fictif }\n"


def clear_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)  # pas de .env du développeur
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)


def test_check_ok_prints_counters_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    clear_env(monkeypatch, tmp_path)
    cfg = write_setup(tmp_path, "  enabled: true\n" + EMB)
    result = runner.invoke(root, ["match", "check", "--config", str(cfg)])
    assert result.exit_code == 0, result.output
    assert "configuration match valide" in result.output
    assert "profil valide : 3 rôle(s) cible, 4 compétence(s) maîtrisée(s)" in result.output
    assert "empreinte du profil :" in result.output
    assert "étage LLM : désactivé" in result.output
    # ni contenu du profil, ni modèle saisi
    for forbidden in ("AI engineer", "Exemple SAS", "modele-fictif", "python"):
        assert forbidden not in result.output


def test_check_reports_a_disabled_section(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    clear_env(monkeypatch, tmp_path)
    cfg = write_setup(tmp_path, EMB)
    result = runner.invoke(root, ["match", "check", "--config", str(cfg)])
    assert result.exit_code == 0
    assert "(désactivée)" in result.output


def test_check_without_match_section(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    clear_env(monkeypatch, tmp_path)
    cfg = tmp_path / "radar.yaml"
    cfg.write_text(SEARCHES)
    result = runner.invoke(root, ["match", "check", "--config", str(cfg)])
    assert result.exit_code == 1
    assert "section match absente" in result.output


def test_check_with_invalid_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    clear_env(monkeypatch, tmp_path)
    result = runner.invoke(root, ["match", "check", "--config", str(tmp_path / "absent.yaml")])
    assert result.exit_code == 1


def test_check_with_missing_or_invalid_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clear_env(monkeypatch, tmp_path)
    missing = write_setup(tmp_path, EMB, profile=tmp_path / "absent.yaml")
    assert runner.invoke(root, ["match", "check", "--config", str(missing)]).exit_code == 1
    bad = tmp_path / "bad.yaml"
    bad.write_text("summary: secret-confidentiel\n")
    cfg = write_setup(tmp_path, EMB, profile=bad)
    result = runner.invoke(root, ["match", "check", "--config", str(cfg)])
    assert result.exit_code == 1
    assert "secret-confidentiel" not in result.output


def test_check_llm_anthropic_requires_its_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clear_env(monkeypatch, tmp_path)
    llm = "  llm: { enabled: true, provider: anthropic, model: modele-fictif }\n"
    cfg = write_setup(tmp_path, EMB + llm)
    result = runner.invoke(root, ["match", "check", "--config", str(cfg)])
    assert result.exit_code == 1
    assert "ANTHROPIC_API_KEY" in result.output
    assert "pas encore implémenté" in result.output

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-valeur-secrete")
    ok = runner.invoke(root, ["match", "check", "--config", str(cfg)])
    assert ok.exit_code == 0
    assert "sk-ant-valeur-secrete" not in ok.output


def test_check_llm_bedrock_needs_no_secret(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    clear_env(monkeypatch, tmp_path)
    llm = "  llm: { enabled: true, provider: bedrock, model: modele-fictif }\n"
    cfg = write_setup(tmp_path, EMB + llm)
    result = runner.invoke(root, ["match", "check", "--config", str(cfg)])
    assert result.exit_code == 0
    assert "OIDC" in result.output


def test_match_makes_no_network_call(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import socket

    def refuse(*_a: object, **_k: object) -> None:
        raise AssertionError("appel réseau interdit")

    monkeypatch.setattr(socket, "socket", refuse)
    clear_env(monkeypatch, tmp_path)
    cfg = write_setup(tmp_path, "  enabled: true\n" + EMB)
    assert runner.invoke(root, ["match", "check", "--config", str(cfg)]).exit_code == 0
