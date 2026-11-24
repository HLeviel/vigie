# Vigie : consignes pour Claude Code

## Spécifications
- Les spécifications sont dans [`docs/specs/`](docs/specs/) ; module `radar` : [`docs/specs/radar.md`](docs/specs/radar.md) ; module `match` : [`docs/specs/match.md`](docs/specs/match.md) (les règles transverses de `radar` s'y appliquent). Les lire avant d'implémenter ou de modifier un module.
- En cas d'ambiguïté ou de contradiction avec la documentation officielle d'une API, **s'arrêter et poser la question** plutôt que deviner.

## Décisions hors spec (section 18 de la spec)
- Une décision prise pendant l'implémentation que la spec ne précise pas va dans la **description de la PR**, section « Décisions » : décision, raison, vérifiée ou non.
- Un point à vérifier ou à trancher devient une **issue** étiquetée `à-vérifier`.
- Les décisions confirmées sont reportées dans la spec à la version suivante (journal des modifications compris).
- Pas de fichier de décisions dans le dépôt.

## Règles constantes
- Français, ton sobre ; ne rien inventer (« non vérifié » / « prévu » si ce n'est pas établi).
- Aucun secret ni donnée personnelle dans le dépôt public ; journaux : compteurs seulement.
- Qualité avant PR : `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy`, `uv run pytest`.
