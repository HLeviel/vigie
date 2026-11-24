"""Vérifie qu'un tag ``vX.Y.Z`` correspond à la version de ``pyproject.toml`` (release.yml)."""

import re
import sys
import tomllib
from pathlib import Path

TAG = re.compile(r"^v(\d+\.\d+\.\d+)$")


def check(tag: str, pyproject: Path) -> str | None:
    """Message d'erreur, ou ``None`` si le tag est valide et correspond à la version du paquet."""
    match = TAG.match(tag)
    if not match:
        return f"tag invalide : attendu vX.Y.Z (ex. v0.1.0), reçu {tag!r}"
    version = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]["version"]
    if match.group(1) != version:
        return (
            f"le tag {tag} ne correspond pas à la version du paquet ({version}) dans pyproject.toml"
        )
    return None


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage : check_release_tag.py <tag>", file=sys.stderr)
        return 2
    error = check(argv[1], Path("pyproject.toml"))
    if error:
        print(error, file=sys.stderr)
        return 1
    print(f"tag {argv[1]} : conforme à pyproject.toml")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
