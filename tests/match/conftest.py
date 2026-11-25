from pathlib import Path

import pytest

from tests.match.cli_helpers import SEARCHES
from tests.match.fakes import FakeEmbedder
from tests.match.helpers import PROFILE_EXAMPLE
from vigie.match.embedders import base


@pytest.fixture
def setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """Configuration, profil et base fictifs, avec de faux fournisseurs d'embeddings."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setitem(base.REGISTRY, "local", lambda model: FakeEmbedder(name=f"local:{model}"))
    monkeypatch.setitem(
        base.REGISTRY, "bedrock", lambda model: FakeEmbedder(name=f"bedrock:{model}")
    )
    profile = tmp_path / "profile.yaml"
    profile.write_text(PROFILE_EXAMPLE.read_text())
    config = tmp_path / "radar.yaml"
    config.write_text(
        SEARCHES + f"match:\n  enabled: true\n  profile: {profile}\n"
        "  embeddings: { provider: local, model: fictif }\n"
        "  evaluation:\n    candidates:\n"
        "      - { provider: bedrock, model: autre-modele }\n"
        "      - { provider: local, model: fictif }\n"  # doublon du modèle configuré : ignoré
    )
    return {"config": config, "db": tmp_path / "radar.db", "tmp": tmp_path, "profile": profile}
