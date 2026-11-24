"""Vigie : copilote IA de recherche d'emploi pour le marché français."""

from importlib.metadata import PackageNotFoundError, version


def _read_version() -> str:
    """Version du paquet installé : ``pyproject.toml`` est la seule source."""
    try:
        return version("vigie")
    except PackageNotFoundError:  # code lancé sans installation (cas anormal)
        return "0+inconnue"


__version__ = _read_version()
