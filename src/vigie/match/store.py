"""Tables de ``match`` dans ``radar.db`` : embeddings (étape 2) ; scores et labels suivent.

Même base et mêmes migrations que ``radar`` (voir ``vigie.match.schema``). Les vecteurs sont des
``float32`` little-endian, de norme 1.
"""

import sqlite3
import struct
from datetime import UTC, datetime

from vigie.radar.store import Store


def encode_vector(vector: list[float]) -> bytes:
    return struct.pack(f"<{len(vector)}f", *vector)


def decode_vector(blob: bytes, dim: int) -> list[float]:
    if len(blob) != 4 * dim:
        raise ValueError("vecteur stocké de taille inattendue")
    return list(struct.unpack(f"<{dim}f", blob))


class EmbeddingStore:
    def __init__(self, store: Store) -> None:
        self._conn: sqlite3.Connection = store.connection

    def get(self, owner: str, model: str, text_hash: str) -> list[float] | None:
        row = self._conn.execute(
            "SELECT dim, vector FROM embeddings WHERE owner = ? AND model = ? AND text_hash = ?",
            (owner, model, text_hash),
        ).fetchone()
        return None if row is None else decode_vector(row["vector"], int(row["dim"]))

    def put(
        self, owner: str, model: str, text_hash: str, vector: list[float], now: datetime
    ) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO embeddings (owner, model, text_hash, dim, vector, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                owner,
                model,
                text_hash,
                len(vector),
                encode_vector(vector),
                now.astimezone(UTC).isoformat(),
            ),
        )

    def count(self) -> int:
        return int(self._conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0])

    def prune_profiles(self, keep_hash: str) -> int:
        """Supprime les embeddings d'anciennes versions du profil ; renvoie leur nombre."""
        cursor = self._conn.execute(
            "DELETE FROM embeddings WHERE owner LIKE 'profile:%' AND owner NOT LIKE ?",
            (f"profile:{keep_hash}:%",),
        )
        return cursor.rowcount
