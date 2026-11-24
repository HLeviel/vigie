import io
from pathlib import Path
from typing import Any

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError

from vigie.radar.state import KEY, RemoteState, S3State, StateSyncError

BUCKET = "bucket-prive-secret-name"


def client_error(code: str, operation: str = "GetObject") -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": f"détail AWS {BUCKET}"}}, operation)


class FakeS3:
    """S3 minimal : un objet, un ETag, et les écritures conditionnelles."""

    def __init__(self, data: bytes | None = None) -> None:
        self.data = data
        self.version = 1 if data is not None else 0
        self.puts: list[dict[str, Any]] = []
        self.get_error: Exception | None = None
        self.put_error: Exception | None = None

    @property
    def etag(self) -> str:
        return f'"v{self.version}"'

    def get_object(self, Bucket: str, Key: str) -> dict[str, Any]:  # noqa: N803
        if self.get_error:
            raise self.get_error
        assert (Bucket, Key) == (BUCKET, KEY)
        if self.data is None:
            raise client_error("NoSuchKey")
        return {"Body": io.BytesIO(self.data), "ETag": self.etag}

    def put_object(self, **kwargs: Any) -> dict[str, Any]:
        self.puts.append(kwargs)
        if self.put_error:
            raise self.put_error
        if "IfMatch" in kwargs and kwargs["IfMatch"] != self.etag:
            raise client_error("PreconditionFailed", "PutObject")
        if kwargs.get("IfNoneMatch") == "*" and self.data is not None:
            raise client_error("PreconditionFailed", "PutObject")
        self.data = kwargs["Body"]
        self.version += 1
        return {"ETag": self.etag}


def test_pull_when_nothing_exists_yet(tmp_path: Path) -> None:
    path = tmp_path / "radar.db"
    remote = S3State(BUCKET, client=FakeS3()).pull(path)
    assert remote == RemoteState(None)
    assert not path.exists()


def test_pull_downloads_atomically(tmp_path: Path) -> None:
    path = tmp_path / "sous-dossier" / "radar.db"  # le dossier est créé
    remote = S3State(BUCKET, client=FakeS3(b"contenu")).pull(path)
    assert remote.etag == '"v1"'
    assert path.read_bytes() == b"contenu"
    assert [p.name for p in path.parent.iterdir()] == ["radar.db"]  # aucun fichier temporaire


def test_first_push_creates_only_if_absent_and_encrypts(tmp_path: Path) -> None:
    path = tmp_path / "radar.db"
    path.write_bytes(b"etat")
    s3 = FakeS3()
    S3State(BUCKET, client=s3).push(path, RemoteState(None))
    [put] = s3.puts
    assert put["IfNoneMatch"] == "*"
    assert "IfMatch" not in put
    assert put["ServerSideEncryption"] == "AES256"
    assert (put["Bucket"], put["Key"], put["Body"]) == (BUCKET, "radar.db", b"etat")
    assert s3.data == b"etat"


def test_push_is_conditional_on_the_downloaded_version(tmp_path: Path) -> None:
    path = tmp_path / "radar.db"
    s3 = FakeS3(b"v1")
    state = S3State(BUCKET, client=s3)
    remote = state.pull(path)
    path.write_bytes(b"v2")
    state.push(path, remote)
    assert s3.puts[0]["IfMatch"] == '"v1"'
    assert s3.data == b"v2"


def test_concurrent_modification_is_detected(tmp_path: Path) -> None:
    path = tmp_path / "radar.db"
    s3 = FakeS3(b"v1")
    state = S3State(BUCKET, client=s3)
    remote = state.pull(path)
    s3.data, s3.version = b"ecrit par une autre execution", 2  # autre exécution entre-temps
    with pytest.raises(StateSyncError, match="a changé pendant l'exécution"):
        state.push(path, remote)
    assert s3.data == b"ecrit par une autre execution"  # rien n'a été écrasé


def test_first_push_refuses_to_overwrite_a_state_created_meanwhile(tmp_path: Path) -> None:
    path = tmp_path / "radar.db"
    path.write_bytes(b"x")
    s3 = FakeS3(b"cree entre-temps")
    with pytest.raises(StateSyncError):
        S3State(BUCKET, client=s3).push(path, RemoteState(None))


@pytest.mark.parametrize("code", ["AccessDenied", "403", "InternalError", "SlowDown"])
def test_read_errors_abort_instead_of_starting_empty(tmp_path: Path, code: str) -> None:
    s3 = FakeS3(b"x")
    s3.get_error = client_error(code)
    with pytest.raises(StateSyncError) as exc:
        S3State(BUCKET, client=s3).pull(tmp_path / "radar.db")
    assert BUCKET not in str(exc.value)
    assert "détail AWS" not in str(exc.value)
    assert exc.value.__suppress_context__


def test_network_error_is_generic(tmp_path: Path) -> None:
    s3 = FakeS3()
    s3.get_error = EndpointConnectionError(endpoint_url=f"https://{BUCKET}.s3.amazonaws.com")
    with pytest.raises(StateSyncError) as exc:
        S3State(BUCKET, client=s3).pull(tmp_path / "radar.db")
    assert BUCKET not in str(exc.value)


def test_write_errors_are_generic(tmp_path: Path) -> None:
    path = tmp_path / "radar.db"
    path.write_bytes(b"x")
    s3 = FakeS3()
    s3.put_error = client_error("AccessDenied", "PutObject")
    with pytest.raises(StateSyncError) as exc:
        S3State(BUCKET, client=s3).push(path, RemoteState(None))
    assert BUCKET not in str(exc.value)
    assert "détail AWS" not in str(exc.value)
    with pytest.raises(StateSyncError):
        S3State(BUCKET, client=s3).push(tmp_path / "absent.db", RemoteState(None))


def test_failed_download_keeps_the_existing_local_file(tmp_path: Path) -> None:
    path = tmp_path / "radar.db"
    path.write_bytes(b"ancien")

    class BrokenBody:
        def read(self) -> bytes:
            raise OSError("coupure")

    class Broken(FakeS3):
        def get_object(self, Bucket: str, Key: str) -> dict[str, Any]:  # noqa: N803
            return {"Body": BrokenBody(), "ETag": '"x"'}

    with pytest.raises(OSError, match="coupure"):
        S3State(BUCKET, client=Broken()).pull(path)
    assert path.read_bytes() == b"ancien"


def test_default_client_is_created_lazily(monkeypatch: pytest.MonkeyPatch) -> None:
    created: list[str] = []
    monkeypatch.setattr("boto3.client", lambda service: created.append(service) or FakeS3())
    state = S3State(BUCKET)
    assert created == []
    state.pull(Path("/nonexistent-dir-ignored/radar.db"))
    assert created == ["s3"]
