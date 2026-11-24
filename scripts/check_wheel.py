"""Vérifie que la roue contient ce dont `vigie-run` a besoin une fois Vigie installé depuis un tag.

Le modèle du digest vit à la racine du dépôt (``templates/``) : sans cette vérification, une roue
sans modèle passerait la CI puis ferait échouer l'envoi du digest en production.
"""

import sys
import zipfile
from pathlib import Path

REQUIRED = ("vigie/templates/digest.html.j2", "vigie/py.typed", "vigie/radar/cli.py")


def check(directory: Path) -> str | None:
    wheels = sorted(directory.glob("*.whl"))
    if len(wheels) != 1:
        return f"une roue attendue dans {directory}, {len(wheels)} trouvée(s)"
    names = set(zipfile.ZipFile(wheels[0]).namelist())
    missing = [name for name in REQUIRED if name not in names]
    if missing:
        return f"fichiers absents de {wheels[0].name} : {', '.join(missing)}"
    return None


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage : check_wheel.py <dossier>", file=sys.stderr)
        return 2
    error = check(Path(argv[1]))
    if error:
        print(error, file=sys.stderr)
        return 1
    print("roue conforme")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
