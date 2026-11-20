"""Synchronisation de ``radar.db`` avec un bucket S3 privé (lot M2).

Le cache GitHub Actions est évincé après 7 jours : il ne sert pas de stockage. Au début d'une
exécution l'état est téléchargé, à la fin d'une exécution terminée il est renvoyé.

Règles de sûreté :
- une erreur autre que « objet absent » à la lecture **interrompt** l'exécution : repartir d'un état
  vide renverrait des offres déjà notifiées ;
- l'écriture est conditionnelle (``If-Match`` avec l'ETag lu, ``If-None-Match: *`` au premier
  envoi) : deux exécutions simultanées ne s'écrasent pas ;
- le chiffrement SSE-S3 est demandé explicitement ;
- aucun nom de bucket ni texte d'erreur AWS dans les messages.
"""

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from botocore.exceptions import BotoCoreError, ClientError

KEY = "radar.db"
_ABSENT = {"NoSuchKey", "404", "NotFound"}
_CONFLICT = {"PreconditionFailed", "ConditionalRequestConflict", "412", "409"}


class StateSyncError(Exception):
    """Échec de synchronisation de l'état. Le message est générique."""


@dataclass(frozen=True)
class RemoteState:
    etag: str | None  # ``None`` : l'objet n'existe pas encore (première exécution)


def _code(exc: ClientError) -> str:
    return str(exc.response.get("Error", {}).get("Code", ""))


class S3State:
    def __init__(self, bucket: str, *, client: Any | None = None, key: str = KEY) -> None:
        self._bucket = bucket
        self._key = key
        self._client = client

    def _s3(self) -> Any:
        if self._client is None:
            import boto3  # import tardif : inutile hors des exécutions avec S3

            self._client = boto3.client("s3")
        return self._client

    def pull(self, path: Path) -> RemoteState:
        """Télécharge l'état dans ``path`` ; ``RemoteState(None)`` si rien n'existe encore."""
        try:
            response = self._s3().get_object(Bucket=self._bucket, Key=self._key)
            data = response["Body"].read()
            etag = str(response["ETag"])
        except ClientError as exc:
            if _code(exc) in _ABSENT:
                return RemoteState(None)
            raise StateSyncError("lecture de l'état S3 impossible") from None
        except BotoCoreError:
            raise StateSyncError("lecture de l'état S3 impossible") from None
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=".radar-", suffix=".tmp")
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(data)
            os.replace(tmp_name, path)  # remplacement atomique
        except BaseException:
            Path(tmp_name).unlink(missing_ok=True)
            raise
        return RemoteState(etag)

    def push(self, path: Path, remote: RemoteState) -> None:
        """Envoie l'état, à condition qu'il n'ait pas changé depuis le téléchargement."""
        options: dict[str, Any] = {"ServerSideEncryption": "AES256"}
        if remote.etag is not None:
            options["IfMatch"] = remote.etag
        else:
            options["IfNoneMatch"] = "*"
        try:
            self._s3().put_object(
                Bucket=self._bucket, Key=self._key, Body=path.read_bytes(), **options
            )
        except ClientError as exc:
            if _code(exc) in _CONFLICT:
                raise StateSyncError(
                    "l'état S3 a changé pendant l'exécution (exécutions simultanées ?)"
                ) from None
            raise StateSyncError("écriture de l'état S3 impossible") from None
        except (BotoCoreError, OSError):
            raise StateSyncError("écriture de l'état S3 impossible") from None
