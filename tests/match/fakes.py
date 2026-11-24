"""Faux fournisseur d'embeddings, déterministe, pour tous les tests de ``match``."""

import hashlib
import struct

from vigie.match.embedders.base import Kind


class FakeEmbedder:
    """Vecteurs dérivés du SHA-256 du texte ; compte les appels et les textes reçus."""

    def __init__(self, name: str = "fake:test", dim: int = 8) -> None:
        self.name = name
        self.dim = dim
        self.calls: list[tuple[list[str], Kind]] = []

    @property
    def texts_embedded(self) -> int:
        return sum(len(texts) for texts, _ in self.calls)

    def embed(self, texts: list[str], kind: Kind) -> list[list[float]]:
        self.calls.append((list(texts), kind))
        return [self._vector(text) for text in texts]

    def _vector(self, text: str) -> list[float]:
        values: list[float] = []
        block = 0
        while len(values) < self.dim:
            digest = hashlib.sha256(f"{block}:{text}".encode()).digest()
            values.extend(n / 32768 - 1.0 for n in struct.unpack("<8H", digest[:16]))
            block += 1
        return values[: self.dim]
