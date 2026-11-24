import io
import json
from typing import Any

import pytest
from botocore.exceptions import BotoCoreError, ClientError

from vigie.match.embedders import base
from vigie.match.embedders.base import EmbedderError, build_embedder
from vigie.match.embedders.bedrock import DIMENSIONS, BedrockEmbedder
from vigie.match.embedders.local import LocalEmbedder, load_sentence_transformer

TITAN = "amazon.titan-embed-text-v2:0"


# --- registre -------------------------------------------------------------------------------------


def test_both_providers_are_registered() -> None:
    import vigie.match.embedders  # noqa: F401

    assert {"local", "bedrock"} <= set(base.REGISTRY)


def test_build_embedder_uses_the_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    client = FakeBedrock()
    monkeypatch.setitem(base.REGISTRY, "bedrock", lambda model: BedrockEmbedder(model, client))
    assert build_embedder("bedrock", TITAN).name == f"bedrock:{TITAN}"


# --- local ----------------------------------------------------------------------------------------


class FakeModel:
    def __init__(self, dim: int = 3) -> None:
        self.dim = dim
        self.seen: list[list[str]] = []
        self.options: dict[str, Any] = {}

    def get_sentence_embedding_dimension(self) -> int:
        return self.dim

    def encode(self, texts: list[str], **options: Any) -> list[list[float]]:
        self.seen.append(texts)
        self.options = options
        return [[float(len(t)), 1.0, 0.0][: self.dim] + [0.0] * (self.dim - 3) for t in texts]


def test_local_embedder_loads_the_model_and_exposes_name_and_dim() -> None:
    loaded: list[str] = []

    def loader(model: str) -> FakeModel:
        loaded.append(model)
        return FakeModel(dim=4)

    embedder = LocalEmbedder("un/modele", loader)
    assert loaded == ["un/modele"]
    assert (embedder.name, embedder.dim) == ("local:un/modele", 4)


def test_local_embed_returns_plain_float_lists_without_progress_bar() -> None:
    model = FakeModel()
    embedder = LocalEmbedder("un/modele", lambda _m: model)
    vectors = embedder.embed(["ab", "cde"], "document")
    assert vectors == [[2.0, 1.0, 0.0], [3.0, 1.0, 0.0]]
    assert model.seen == [["ab", "cde"]]  # aucun préfixe hors famille E5
    assert model.options == {"normalize_embeddings": True, "show_progress_bar": False}


def test_local_e5_models_get_query_and_passage_prefixes() -> None:
    model = FakeModel()
    embedder = LocalEmbedder("intfloat/multilingual-e5-small", lambda _m: model)
    embedder.embed(["profil"], "query")
    embedder.embed(["offre"], "document")
    assert model.seen == [["query: profil"], ["passage: offre"]]


def test_local_rejects_an_unknown_dimension() -> None:
    class NoDim(FakeModel):
        def get_sentence_embedding_dimension(self) -> int:
            return None  # type: ignore[return-value]

    with pytest.raises(EmbedderError):
        LocalEmbedder("m", lambda _m: NoDim())


def test_local_encode_failure_is_a_safe_error() -> None:
    class Boom(FakeModel):
        def encode(self, texts: list[str], **options: Any) -> list[list[float]]:
            raise RuntimeError("texte-confidentiel")

    embedder = LocalEmbedder("m", lambda _m: Boom())
    with pytest.raises(EmbedderError) as exc:
        embedder.embed(["texte-confidentiel"], "document")
    assert "texte-confidentiel" not in str(exc.value)


def test_missing_extra_gives_an_actionable_message(monkeypatch: pytest.MonkeyPatch) -> None:
    import importlib

    def refuse(name: str) -> Any:
        raise ImportError(name)

    monkeypatch.setattr(importlib, "import_module", refuse)
    with pytest.raises(EmbedderError) as exc:
        load_sentence_transformer("m")
    assert "extra" in str(exc.value)


def test_unreadable_model_is_a_safe_error(monkeypatch: pytest.MonkeyPatch) -> None:
    import importlib

    class Module:
        @staticmethod
        def SentenceTransformer(model: str) -> Any:  # noqa: N802
            raise OSError("chemin-secret")

    monkeypatch.setattr(importlib, "import_module", lambda _name: Module)
    with pytest.raises(EmbedderError) as exc:
        load_sentence_transformer("m")
    assert "chemin-secret" not in str(exc.value)


# --- bedrock --------------------------------------------------------------------------------------


class FakeBedrock:
    def __init__(self, vector: list[float] | None = None) -> None:
        self.vector = vector if vector is not None else [0.5] * DIMENSIONS
        self.calls: list[dict[str, Any]] = []

    def invoke_model(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return {"body": io.BytesIO(json.dumps({"embedding": self.vector}).encode())}


def test_bedrock_sends_the_documented_titan_v2_request() -> None:
    client = FakeBedrock()
    embedder = BedrockEmbedder(TITAN, client)
    vectors = embedder.embed(["texte a", "texte b"], "document")
    assert len(vectors) == 2 and len(vectors[0]) == embedder.dim == DIMENSIONS
    first = client.calls[0]
    assert first["modelId"] == TITAN
    assert first["contentType"] == first["accept"] == "application/json"
    assert json.loads(first["body"]) == {
        "inputText": "texte a",
        "dimensions": DIMENSIONS,
        "normalize": True,
    }
    assert embedder.name == f"bedrock:{TITAN}"


@pytest.mark.parametrize("model", ["cohere.embed-multilingual-v3", "autre", ""])
def test_other_model_families_are_refused_not_guessed(model: str) -> None:
    with pytest.raises(EmbedderError) as exc:
        BedrockEmbedder(model, FakeBedrock())
    assert "Titan" in str(exc.value)


def client_error(code: str, message: str = "texte-aws-confidentiel") -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": message}}, "InvokeModel")


class Failing:
    def __init__(self, error: Exception) -> None:
        self.error = error

    def invoke_model(self, **_kwargs: Any) -> Any:
        raise self.error


def test_bedrock_errors_expose_only_the_aws_error_code() -> None:
    embedder = BedrockEmbedder(TITAN, Failing(client_error("AccessDeniedException")))
    with pytest.raises(EmbedderError) as exc:
        embedder.embed(["texte-offre"], "document")
    assert "AccessDeniedException" in str(exc.value)
    assert "texte-aws-confidentiel" not in str(exc.value)
    assert "texte-offre" not in str(exc.value)


def test_bedrock_transport_error_is_generic() -> None:
    embedder = BedrockEmbedder(TITAN, Failing(BotoCoreError()))
    with pytest.raises(EmbedderError):
        embedder.embed(["x"], "document")


@pytest.mark.parametrize("payload", [b"pas du json", b"{}", b'{"embedding": "abc"}', b"[1]"])
def test_bedrock_malformed_response_is_refused(payload: bytes) -> None:
    class Bad:
        def invoke_model(self, **_kwargs: Any) -> Any:
            return {"body": io.BytesIO(payload)}

    with pytest.raises(EmbedderError):
        BedrockEmbedder(TITAN, Bad()).embed(["x"], "document")


def test_bedrock_client_is_created_lazily_and_region_errors_are_safe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import boto3

    created: list[tuple[str, Any]] = []

    def fake_client(service: str, **kwargs: Any) -> FakeBedrock:
        created.append((service, kwargs))
        return FakeBedrock()

    monkeypatch.setattr(boto3, "client", fake_client)
    embedder = BedrockEmbedder(TITAN)
    assert created == []  # rien avant le premier appel
    embedder.embed(["x"], "document")
    embedder.embed(["y"], "document")
    assert [service for service, _ in created] == ["bedrock-runtime"]

    def no_region(*_a: Any, **_k: Any) -> Any:
        raise BotoCoreError()

    monkeypatch.setattr(boto3, "client", no_region)
    with pytest.raises(EmbedderError):
        BedrockEmbedder(TITAN).embed(["x"], "document")
