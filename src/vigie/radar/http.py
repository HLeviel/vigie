"""Client HTTP commun des sources : timeouts, retries, pause, masquage des secrets."""

import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from types import TracebackType
from typing import Any, Self
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx
from tenacity import RetryCallState, Retrying, retry_if_exception_type, stop_after_attempt

from vigie import __version__

logger = logging.getLogger(__name__)

# httpx journalise l'URL complète (paramètres compris, donc ``app_key``) au niveau INFO.
# Nos propres logs passent par ``mask_url`` ; on coupe ceux des bibliothèques.
for _name in ("httpx", "httpcore"):
    logging.getLogger(_name).setLevel(logging.WARNING)

USER_AGENT = f"vigie-radar/{__version__} (+https://github.com/HLeviel/vigie)"
TIMEOUT = httpx.Timeout(30.0, connect=10.0)
MAX_ATTEMPTS = 3
MAX_RETRY_AFTER_S = 60.0
MAX_BACKOFF_S = 30.0
DEFAULT_MIN_INTERVAL_S = 0.2
SENSITIVE_PARAMS = frozenset({"app_key", "app_id", "client_secret", "client_id", "access_token"})
MASK = "***"


def mask_url(url: str) -> str:
    """URL dont les valeurs des paramètres sensibles sont remplacées par ``***``."""
    parts = urlsplit(url)
    if not parts.query:
        return url
    pairs = [
        (key, MASK if key.lower() in SENSITIVE_PARAMS else value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
    ]
    return urlunsplit(parts._replace(query=urlencode(pairs, safe="*")))


class SourceHttpError(Exception):
    """Échec HTTP définitif. Le message ne contient jamais de secret."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class _RetryableStatusError(Exception):
    def __init__(self, status_code: int, retry_after: float | None) -> None:
        super().__init__(f"HTTP {status_code}")
        self.status_code = status_code
        self.retry_after = retry_after


def parse_retry_after(value: str | None) -> float | None:
    """Délai en secondes d'un en-tête ``Retry-After`` (secondes ou date HTTP)."""
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, (when - datetime.now(UTC)).total_seconds())


class SourceHttpClient:
    """Client ``httpx`` pour une source : une pause minimale entre requêtes et des retries."""

    def __init__(
        self,
        source: str,
        *,
        min_interval: float = DEFAULT_MIN_INTERVAL_S,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.source = source
        self._min_interval = min_interval
        self._sleep = sleep
        self._monotonic = monotonic
        self._last_request: float | None = None
        self._client = httpx.Client(timeout=TIMEOUT, headers={"User-Agent": USER_AGENT})

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def _wait(self, state: RetryCallState) -> float:
        exc = state.outcome.exception() if state.outcome else None
        if isinstance(exc, _RetryableStatusError) and exc.retry_after is not None:
            return min(exc.retry_after, MAX_RETRY_AFTER_S)
        return min(float(2 ** (state.attempt_number - 1)), MAX_BACKOFF_S)

    def _pause(self) -> None:
        if self._last_request is not None:
            remaining = self._min_interval - (self._monotonic() - self._last_request)
            if remaining > 0:
                self._sleep(remaining)
        self._last_request = self._monotonic()

    def _attempt(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        self._pause()
        response = self._client.request(method, url, **kwargs)
        status = response.status_code
        if status == 429 or status >= 500:
            retry_after = parse_retry_after(response.headers.get("Retry-After"))
            raise _RetryableStatusError(status, retry_after)
        return response

    def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        """Envoie la requête. Retry sur erreur réseau, 429 et 5xx ; erreur pour tout 4xx/5xx final.

        Lève ``SourceHttpError`` : le message n'expose l'URL qu'après masquage des secrets.
        """
        retrying = Retrying(
            stop=stop_after_attempt(MAX_ATTEMPTS),
            retry=retry_if_exception_type((httpx.TransportError, _RetryableStatusError)),
            wait=self._wait,
            sleep=self._sleep,
            reraise=True,
        )
        try:
            response = retrying(self._attempt, method, url, **kwargs)
        except _RetryableStatusError as exc:
            raise SourceHttpError(
                f"{self.source} : HTTP {exc.status_code} après {MAX_ATTEMPTS} tentatives",
                exc.status_code,
            ) from None
        except httpx.TransportError as exc:
            raise SourceHttpError(
                f"{self.source} : erreur réseau ({type(exc).__name__}) après "
                f"{MAX_ATTEMPTS} tentatives"
            ) from None
        if response.status_code >= 400:
            raise SourceHttpError(
                f"{self.source} : HTTP {response.status_code} sur {mask_url(str(response.url))}",
                response.status_code,
            )
        logger.debug("%s %s -> %s", method, mask_url(str(response.url)), response.status_code)
        return response
