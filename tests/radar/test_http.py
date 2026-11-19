from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import httpx
import pytest
import respx

from vigie.radar.http import (
    USER_AGENT,
    SourceHttpClient,
    SourceHttpError,
    mask_url,
    parse_retry_after,
)

URL = "https://api.example.org/search"
SECRET = "s3cr3t-valeur-de-test"


class Clock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


@pytest.fixture
def clock() -> Clock:
    return Clock()


def make_client(clock: Clock, min_interval: float = 0.0) -> SourceHttpClient:
    return SourceHttpClient(
        "demo", min_interval=min_interval, sleep=clock.sleep, monotonic=clock.monotonic
    )


@respx.mock
def test_user_agent_and_success(clock: Clock) -> None:
    route = respx.get(URL).mock(return_value=httpx.Response(200, json={"ok": True}))
    with make_client(clock) as client:
        response = client.request("GET", URL)
    assert response.json() == {"ok": True}
    assert route.calls.last.request.headers["User-Agent"] == USER_AGENT
    assert USER_AGENT.startswith("vigie-radar/")
    assert "+https://github.com/HLeviel/vigie" in USER_AGENT


def test_timeouts() -> None:
    with SourceHttpClient("demo") as client:
        timeout = client._client.timeout
    assert timeout.connect == 10.0
    assert timeout.read == 30.0


@respx.mock
def test_retry_on_5xx_then_success(clock: Clock) -> None:
    route = respx.get(URL).mock(
        side_effect=[httpx.Response(503), httpx.Response(500), httpx.Response(200)]
    )
    with make_client(clock) as client:
        assert client.request("GET", URL).status_code == 200
    assert route.call_count == 3
    assert clock.sleeps == [1.0, 2.0]


@respx.mock
def test_retry_after_is_honoured(clock: Clock) -> None:
    route = respx.get(URL).mock(
        side_effect=[httpx.Response(429, headers={"Retry-After": "7"}), httpx.Response(200)]
    )
    with make_client(clock) as client:
        client.request("GET", URL)
    assert route.call_count == 2
    assert clock.sleeps == [7.0]


@respx.mock
def test_retry_after_is_capped(clock: Clock) -> None:
    respx.get(URL).mock(
        side_effect=[httpx.Response(429, headers={"Retry-After": "9999"}), httpx.Response(200)]
    )
    with make_client(clock) as client:
        client.request("GET", URL)
    assert clock.sleeps == [60.0]


@respx.mock
def test_gives_up_after_three_attempts(clock: Clock) -> None:
    route = respx.get(URL).mock(return_value=httpx.Response(500))
    with make_client(clock) as client, pytest.raises(SourceHttpError) as exc:
        client.request("GET", URL)
    assert route.call_count == 3
    assert exc.value.status_code == 500


@pytest.mark.parametrize("status", [400, 401, 403, 404])
@respx.mock
def test_no_retry_on_other_4xx(clock: Clock, status: int) -> None:
    route = respx.get(URL).mock(return_value=httpx.Response(status))
    with make_client(clock) as client, pytest.raises(SourceHttpError) as exc:
        client.request("GET", URL)
    assert route.call_count == 1
    assert exc.value.status_code == status
    assert clock.sleeps == []


@respx.mock
def test_retry_on_network_error(clock: Clock) -> None:
    route = respx.get(URL).mock(side_effect=[httpx.ConnectError("boom"), httpx.Response(200)])
    with make_client(clock) as client:
        assert client.request("GET", URL).status_code == 200
    assert route.call_count == 2


@respx.mock
def test_network_error_message_has_no_url(clock: Clock) -> None:
    respx.get(URL).mock(side_effect=httpx.ConnectError(f"failed {URL}?app_key={SECRET}"))
    with make_client(clock) as client, pytest.raises(SourceHttpError) as exc:
        client.request("GET", URL, params={"app_key": SECRET})
    assert SECRET not in str(exc.value)
    assert SECRET not in repr(exc.value)


@respx.mock
def test_error_message_masks_secret_params(clock: Clock) -> None:
    respx.get(URL).mock(return_value=httpx.Response(401))
    with make_client(clock) as client, pytest.raises(SourceHttpError) as exc:
        client.request("GET", URL, params={"app_id": "ID", "app_key": SECRET, "what": "python"})
    assert SECRET not in str(exc.value)
    assert "app_key=***" in str(exc.value)
    assert "what=python" in str(exc.value)


@respx.mock
def test_minimum_pause_between_requests(clock: Clock) -> None:
    respx.get(URL).mock(return_value=httpx.Response(200))
    with make_client(clock, min_interval=0.2) as client:
        client.request("GET", URL)
        client.request("GET", URL)
    assert clock.sleeps == [pytest.approx(0.2)]


def test_mask_url() -> None:
    masked = mask_url(f"https://x.org/a?app_id=1&app_key={SECRET}&client_secret={SECRET}&q=py")
    assert SECRET not in masked
    assert "q=py" in masked
    assert mask_url("https://x.org/a") == "https://x.org/a"


def test_parse_retry_after() -> None:
    assert parse_retry_after(None) is None
    assert parse_retry_after("5") == 5.0
    assert parse_retry_after("abc") is None
    future = datetime.now(UTC) + timedelta(seconds=30)
    value = parse_retry_after(format_datetime(future, usegmt=True))
    assert value is not None
    assert 20 < value <= 30
    past = datetime.now(UTC) - timedelta(seconds=30)
    assert parse_retry_after(format_datetime(past, usegmt=True)) == 0.0
