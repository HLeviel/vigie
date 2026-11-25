"""`vigie match label` et `vigie match eval`."""

import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
from typer.testing import CliRunner

from tests.match.fakes import FakeEmbedder
from tests.match.helpers import NOW, PROFILE_EXAMPLE, make_offers
from vigie.match.embedders import base
from vigie.match.evaluation.dataset import load_json
from vigie.match.profile import load_profile_file
from vigie.match.store import LabelStore
from vigie.radar.cli import root
from vigie.radar.store import Store

FIXTURE = Path(__file__).parent / "fixtures" / "eval_fictif.json"
runner = CliRunner()
SEARCHES = "searches:\n  - { name: s, keywords: [a], departments: ['69'] }\n"


@pytest.fixture
def setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
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


def invoke(setup: dict[str, Path], command: str, *extra: str, input_: str | None = None):  # type: ignore[no-untyped-def]
    return runner.invoke(
        root,
        ["match", command, "--config", str(setup["config"]), "--db", str(setup["db"]), *extra],
        input=input_,
    )


def fill_offers(setup: dict[str, Path]) -> None:
    with Store.open(setup["db"]) as store:
        for offer in make_offers():
            store.insert_offer(offer, NOW)


def stored_labels(setup: dict[str, Path]) -> dict[str, str]:
    with Store.open(setup["db"]) as store:
        return LabelStore(store).for_profile(load_profile_file(setup["profile"]).profile_hash)


# --- label ----------------------------------------------------------------------------------------


def test_label_records_answers_and_skips(setup: dict[str, Path]) -> None:
    fill_offers(setup)
    result = invoke(setup, "label", "--n", "4", input_="p\nm\ns\nn\n")
    assert result.exit_code == 0, result.output
    labels = stored_labels(setup)
    assert sorted(labels.values()) == ["non pertinent", "pertinent", "peut-être"]  # « s » : passée
    assert "3 étiquette(s) posée(s) ; total 3 dont 1 pertinente(s)" in result.output
    assert "objectif : 150 dont 30" in result.output


def test_invalid_answers_are_asked_again_and_q_quits(setup: dict[str, Path]) -> None:
    fill_offers(setup)
    result = invoke(setup, "label", "--n", "4", input_="x\n\nP\nq\n")
    assert result.exit_code == 0, result.output
    assert list(stored_labels(setup).values()) == ["pertinent"]  # « P » accepté, puis « q »


def test_labeled_offers_are_not_offered_again(setup: dict[str, Path]) -> None:
    fill_offers(setup)
    invoke(setup, "label", "--n", "4", input_="p\np\np\np\n")
    again = invoke(setup, "label", "--n", "4")
    assert "aucune offre à annoter" in again.output


def test_label_output_is_clean_text(setup: dict[str, Path]) -> None:
    from tests.radar.factories import make_offer

    with Store.open(setup["db"]) as store:
        store.insert_offer(make_offer(title="Titre\x1b[31m rouge", description="a‮b\x07c " * 5), NOW)
    result = invoke(setup, "label", "--n", "1", input_="q\n")
    assert "\x1b" not in result.output and "‮" not in result.output
    assert "Titre" in result.output  # l'offre est bien affichée (donnée privée)


def test_label_needs_a_database_and_a_match_section(setup: dict[str, Path]) -> None:
    assert invoke(setup, "label").exit_code == 1  # pas de base
    setup["config"].write_text(SEARCHES)
    fill_offers(setup)
    assert invoke(setup, "label").exit_code == 1  # pas de section match


# --- eval -----------------------------------------------------------------------------------------


def label_fixture(setup: dict[str, Path], count: int | None = None, profile_hash: str = "") -> None:
    hash_ = profile_hash or load_profile_file(setup["profile"]).profile_hash
    with Store.open(setup["db"]) as store:
        for item in load_json(FIXTURE)[:count]:
            store.insert_offer(item.offer, NOW)
            LabelStore(store).put(item.uid, hash_, item.label, NOW)


def test_eval_writes_markdown_and_svg_reports(setup: dict[str, Path]) -> None:
    label_fixture(setup)
    out = setup["tmp"] / "rapport" / "match.md"
    out.parent.mkdir()
    result = invoke(setup, "eval", "--output", str(out))
    assert result.exit_code == 0, result.output
    assert "60 offres annotées" in result.output and "jeu insuffisant" in result.output
    report = out.read_text()
    assert "emb:local:fictif" in report and "emb:bedrock:autre-modele" in report
    assert report.count("`emb:local:fictif`") >= 1
    assert "![ndcg@10 par variante](match.svg)" in report
    ET.fromstring(out.with_suffix(".svg").read_text())  # noqa: S314 (sortie produite par ce test)
    # aucune donnée d'offre dans le rapport ni dans la console
    for text in ("Société fictive", "Ingénieur IA", "Comptable"):
        assert text not in report and text not in result.output


def test_eval_default_output_name_is_dated(setup: dict[str, Path]) -> None:
    label_fixture(setup)
    assert invoke(setup, "eval").exit_code == 0
    reports = list(setup["tmp"].glob("match-????-??-??.md"))
    assert len(reports) == 1 and reports[0].with_suffix(".svg").exists()


def test_eval_refuses_too_few_labels(setup: dict[str, Path]) -> None:
    label_fixture(setup, count=5)
    result = invoke(setup, "eval")
    assert result.exit_code == 1 and "au moins 10" in result.output


def test_eval_ignores_labels_of_another_profile_version(setup: dict[str, Path]) -> None:
    label_fixture(setup, profile_hash="ancien-profil")
    result = invoke(setup, "eval")
    assert result.exit_code == 1
    assert "60 étiquette(s) posée(s) sous une autre version du profil : ignorées" in result.output


def test_eval_reports_a_provider_failure_without_details(
    setup: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    from vigie.match.embedders.base import EmbedderError

    label_fixture(setup)

    def broken(model: str) -> FakeEmbedder:
        raise EmbedderError("fournisseur indisponible")

    monkeypatch.setitem(base.REGISTRY, "bedrock", broken)
    result = invoke(setup, "eval")
    assert result.exit_code == 1 and "fournisseur indisponible" in result.output


def test_eval_reports_an_embedding_failure_during_the_run(
    setup: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    from vigie.match.embedders.base import EmbedderError

    label_fixture(setup)

    class Down(FakeEmbedder):
        def embed(self, texts: list[str], kind: str) -> list[list[float]]:  # type: ignore[override]
            raise EmbedderError("fournisseur indisponible")

    monkeypatch.setitem(base.REGISTRY, "local", lambda model: Down(name=f"local:{model}"))
    result = invoke(setup, "eval")
    assert result.exit_code == 1 and "embeddings : fournisseur indisponible" in result.output


def test_setup_errors_exit_1_for_both_commands(setup: dict[str, Path]) -> None:
    fill_offers(setup)
    setup["profile"].write_text("summary: valeur-secrete\n")
    for command in ("label", "eval"):
        result = invoke(setup, command)
        assert result.exit_code == 1 and "valeur-secrete" not in result.output
    missing = runner.invoke(root, ["match", "eval", "--config", str(setup["tmp"] / "absent.yaml")])
    assert missing.exit_code == 1
    no_db = invoke({**setup, "db": setup["tmp"] / "inexistante.db"}, "eval")
    assert no_db.exit_code == 1 and "aucune base" in no_db.output


def test_a_newer_database_is_refused(setup: dict[str, Path]) -> None:
    import sqlite3

    with sqlite3.connect(setup["db"]) as conn:
        conn.execute("PRAGMA user_version = 999")
    result = invoke(setup, "label")
    assert result.exit_code == 1 and "plus récente" in result.output
