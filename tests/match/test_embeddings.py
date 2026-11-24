import math
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tests.match.fakes import FakeEmbedder
from vigie.match.embedders import base
from vigie.match.embedders.base import EmbedderError, build_embedder, normalize_vector
from vigie.match.embedders.cache import BATCH_SIZE, embed_cached
from vigie.match.store import EmbeddingStore, decode_vector, encode_vector
from vigie.radar.store import SCHEMA_VERSION, Store

NOW = datetime(2026, 11, 24, 6, 0, tzinfo=UTC)


def norm(vector: list[float]) -> float:
    return math.sqrt(sum(x * x for x in vector))


@pytest.fixture
def store() -> Store:
    return Store.open(":memory:")


# --- normalisation et interface ---------------------------------------------------------------


def test_vectors_are_normalized() -> None:
    assert normalize_vector([3.0, 4.0]) == pytest.approx([0.6, 0.8])
    assert norm(normalize_vector([1.0, 2.0, 3.0])) == pytest.approx(1.0)


@pytest.mark.parametrize("bad", [[], [0.0, 0.0], [float("nan"), 1.0], [float("inf"), 1.0]])
def test_invalid_vectors_are_refused(bad: list[float]) -> None:
    with pytest.raises(EmbedderError):
        normalize_vector(bad)


def test_unknown_provider_is_a_safe_error() -> None:
    with pytest.raises(EmbedderError) as exc:
        build_embedder("inconnu", "m")
    assert "inconnu" in str(exc.value)


def test_registry_builds_a_registered_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(base.REGISTRY, "fake", lambda model: FakeEmbedder(name=f"fake:{model}"))
    assert build_embedder("fake", "x").name == "fake:x"


def test_fake_embedder_is_deterministic() -> None:
    a, b = FakeEmbedder(), FakeEmbedder()
    assert a.embed(["x"], "document") == b.embed(["x"], "query")
    assert a.embed(["x"], "document") != a.embed(["y"], "document")
    assert len(a.embed(["x"], "document")[0]) == a.dim


# --- stockage -----------------------------------------------------------------------------------


def test_vector_encoding_round_trip_is_float32() -> None:
    vector = normalize_vector([0.1, 0.2, 0.3])
    decoded = decode_vector(encode_vector(vector), 3)
    assert decoded == pytest.approx(vector, abs=1e-6)
    assert len(encode_vector(vector)) == 12
    with pytest.raises(ValueError):
        decode_vector(b"\x00" * 5, 3)


def test_migration_creates_the_table_and_keeps_radar_data(tmp_path: Path) -> None:
    path = tmp_path / "radar.db"
    conn = sqlite3.connect(path)
    from vigie.radar.store import MIGRATIONS

    conn.executescript(MIGRATIONS[0])  # base « ancienne » : version 1 seulement
    conn.execute("PRAGMA user_version = 1")
    conn.commit()
    conn.close()
    with Store.open(path) as migrated:
        assert migrated.version == SCHEMA_VERSION
        assert EmbeddingStore(migrated).count() == 0


def test_put_get_and_replace(store: Store) -> None:
    emb = EmbeddingStore(store)
    vector = normalize_vector([1.0, 2.0])
    assert emb.get("offer:a", "m", "h") is None
    emb.put("offer:a", "m", "h", vector, NOW)
    assert emb.get("offer:a", "m", "h") == pytest.approx(vector, abs=1e-6)
    assert emb.get("offer:a", "autre-modele", "h") is None
    assert emb.get("offer:a", "m", "autre-texte") is None
    emb.put("offer:a", "m", "h", normalize_vector([2.0, 1.0]), NOW)
    assert emb.count() == 1


def test_prune_removes_only_old_profile_versions(store: Store) -> None:
    emb = EmbeddingStore(store)
    vector = normalize_vector([1.0, 1.0])
    for owner in ("profile:aaa:0", "profile:aaa:1", "profile:bbb:0", "offer:x"):
        emb.put(owner, "m", "h", vector, NOW)
    assert emb.prune_profiles("bbb") == 2
    assert emb.get("profile:bbb:0", "m", "h") is not None
    assert emb.get("offer:x", "m", "h") is not None
    assert emb.get("profile:aaa:0", "m", "h") is None


# --- cache --------------------------------------------------------------------------------------


def test_second_pass_does_not_recompute(store: Store) -> None:
    emb, fake = EmbeddingStore(store), FakeEmbedder()
    items = [("offer:a", "texte a"), ("offer:b", "texte b")]
    first, stats1 = embed_cached(fake, emb, items, "document", NOW)
    assert (stats1.computed, stats1.cached) == (2, 0)
    second, stats2 = embed_cached(fake, emb, items, "document", NOW)
    assert (stats2.computed, stats2.cached) == (0, 2)
    assert fake.texts_embedded == 2  # aucun nouvel appel
    assert second == first  # même valeur depuis le cache


def test_only_changed_texts_are_recomputed(store: Store) -> None:
    emb, fake = EmbeddingStore(store), FakeEmbedder()
    embed_cached(fake, emb, [("offer:a", "v1"), ("offer:b", "b")], "document", NOW)
    _, stats = embed_cached(fake, emb, [("offer:a", "v2"), ("offer:b", "b")], "document", NOW)
    assert (stats.computed, stats.cached) == (1, 1)


def test_cache_is_per_model(store: Store) -> None:
    emb = EmbeddingStore(store)
    one, two = FakeEmbedder(name="fake:un"), FakeEmbedder(name="fake:deux")
    embed_cached(one, emb, [("offer:a", "t")], "document", NOW)
    _, stats = embed_cached(two, emb, [("offer:a", "t")], "document", NOW)
    assert stats.computed == 1


def test_stored_and_returned_vectors_are_normalized(store: Store) -> None:
    result, _ = embed_cached(
        FakeEmbedder(), EmbeddingStore(store), [("offer:a", "t")], "document", NOW
    )
    assert norm(result["offer:a"]) == pytest.approx(1.0, abs=1e-6)


def test_batches_and_kind_are_forwarded(store: Store) -> None:
    fake = FakeEmbedder()
    items = [(f"offer:{n}", f"texte {n}") for n in range(BATCH_SIZE + 1)]
    result, _ = embed_cached(fake, EmbeddingStore(store), items, "query", NOW)
    assert len(result) == BATCH_SIZE + 1
    assert [len(texts) for texts, _ in fake.calls] == [BATCH_SIZE, 1]
    assert {kind for _, kind in fake.calls} == {"query"}


def test_empty_input_makes_no_call(store: Store) -> None:
    fake = FakeEmbedder()
    result, stats = embed_cached(fake, EmbeddingStore(store), [], "document", NOW)
    assert result == {} and stats.computed == 0 and fake.calls == []


class Broken(FakeEmbedder):
    def __init__(self, mode: str) -> None:
        super().__init__()
        self.mode = mode

    def embed(self, texts: list[str], kind: str) -> list[list[float]]:  # type: ignore[override]
        if self.mode == "count":
            return [[1.0] * self.dim]
        if self.mode == "dim":
            return [[1.0] * (self.dim + 1) for _ in texts]
        return [[0.0] * self.dim for _ in texts]


@pytest.mark.parametrize("mode", ["count", "dim", "zero"])
def test_invalid_provider_output_is_refused_and_not_stored(store: Store, mode: str) -> None:
    emb = EmbeddingStore(store)
    with pytest.raises(EmbedderError) as exc:
        embed_cached(Broken(mode), emb, [("offer:a", "secret-texte"), ("offer:b", "y")], "document")
    assert "secret-texte" not in str(exc.value)
    assert emb.count() == 0


def test_cached_vector_of_wrong_dimension_is_recomputed(store: Store) -> None:
    emb, fake = EmbeddingStore(store), FakeEmbedder()
    from vigie.match.text import text_hash

    emb.put("offer:a", fake.name, text_hash("t"), normalize_vector([1.0, 1.0]), NOW)  # dim 2
    _, stats = embed_cached(fake, emb, [("offer:a", "t")], "document", NOW)
    assert stats.computed == 1
