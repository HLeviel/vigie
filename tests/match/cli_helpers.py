"""Outils partagés par les tests des commandes `vigie match label` et `vigie match eval`."""

from pathlib import Path
from typing import Any

from typer.testing import CliRunner

from tests.match.helpers import NOW, make_offers
from vigie.match.store import LabelStore
from vigie.radar.cli import root
from vigie.radar.store import Store

runner = CliRunner()
SEARCHES = "searches:\n  - { name: s, keywords: [a], departments: ['69'] }\n"


def invoke(setup: dict[str, Path], command: str, *extra: str, input_: str | None = None) -> Any:
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
        return LabelStore(store).latest()
