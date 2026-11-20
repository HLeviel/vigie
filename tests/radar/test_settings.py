import pytest

from vigie.settings import MissingSettingsError, Settings

SENTINEL = "s3cr3t-valeur-de-test"


def make(**env: str) -> Settings:
    return Settings(_env_file=None, **env)  # type: ignore[call-arg]


def test_secret_repr_and_dump_are_masked() -> None:
    s = make(ft_client_id="id", ft_client_secret=SENTINEL)
    assert SENTINEL not in repr(s)
    assert SENTINEL not in str(s)
    assert SENTINEL not in s.model_dump_json()
    assert s.ft_client_secret is not None
    assert s.ft_client_secret.get_secret_value() == SENTINEL


def test_missing_variable_is_named_without_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FT_CLIENT_ID", raising=False)
    monkeypatch.delenv("FT_CLIENT_SECRET", raising=False)
    s = make(ft_client_secret=SENTINEL)
    with pytest.raises(MissingSettingsError) as exc:
        s.require(["france_travail"])
    assert "FT_CLIENT_ID" in str(exc.value)
    assert "FT_CLIENT_SECRET" not in str(exc.value)
    assert SENTINEL not in str(exc.value)


def test_no_default_for_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("FT_CLIENT_ID", "FT_CLIENT_SECRET", "ADZUNA_APP_ID", "ADZUNA_APP_KEY"):
        monkeypatch.delenv(name, raising=False)
    s = make()
    assert s.missing(["france_travail", "adzuna"]) == [
        "FT_CLIENT_ID",
        "FT_CLIENT_SECRET",
        "ADZUNA_APP_ID",
        "ADZUNA_APP_KEY",
    ]


def test_require_passes_when_present() -> None:
    make(ft_client_id="a", ft_client_secret="b").require(["france_travail"])


def test_imap_variables_are_required_together_and_masked() -> None:
    s = make(imap_host="imap.example.org", imap_user="moi", imap_password=SENTINEL)
    assert s.missing(["imap"]) == ["IMAP_FOLDER"]
    assert SENTINEL not in repr(s)
    assert SENTINEL not in s.model_dump_json()
    assert "moi" not in repr(s)  # l'identifiant est aussi un SecretStr
