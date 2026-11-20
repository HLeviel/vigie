import pytest

from vigie.radar.sources.ats.location import Place, is_eligible, is_in_france, is_remote_compatible


@pytest.mark.parametrize(
    ("place", "expected"),
    [
        (Place(text="Paris"), True),
        (Place(text="Lyon, France"), True),
        (Place(text="Villeurbanne"), True),
        (Place(text="Paris / New York"), True),
        (Place(text="Aix-en-Provence"), True),
        (Place(text="Remote - France"), True),
        (Place(text="Remote", countries=("FR",)), True),
        (Place(text="Siège", countries=("fr",)), True),
        (Place(text="London"), False),
        (Place(text="San Francisco, CA"), False),
        (Place(text="Parisian Quarter, Ohio"), False),  # « paris » n'est pas un mot entier
        (Place(text="Remote - US"), False),
        (Place(text=""), False),
    ],
)
def test_is_in_france(place: Place, expected: bool) -> None:
    assert is_in_france(place) is expected


@pytest.mark.parametrize(
    ("place", "expected"),
    [
        (Place(text="Remote"), True),  # aucune restriction
        (Place(text="Remote - Worldwide"), True),
        (Place(text="Anywhere"), True),
        (Place(text="Remote - European Union"), True),
        (Place(text="Remote - Europe"), True),
        (Place(text="Remote, EMEA"), True),
        (Place(text="Remote - United Kingdom"), True),
        (Place(text="Télétravail"), True),
        (Place(remote=True), True),
        (Place(text="Remote - US"), False),
        (Place(text="Remote - Canada"), False),
        (Place(text="Remote, India"), False),
        (Place(text="Remote", countries=("US",)), False),  # code pays hors Europe
        (Place(text="Remote", countries=("DE",)), True),
        (Place(text="Remote", countries=("US", "FR")), True),
        (Place(text="Berlin"), False),  # pas de télétravail
        (Place(text="Hybrid - Paris"), False),  # hybride n'est pas du télétravail intégral
    ],
)
def test_is_remote_compatible(place: Place, expected: bool) -> None:
    assert is_remote_compatible(place) is expected


def test_is_eligible_combines_both() -> None:
    assert is_eligible(Place(text="Paris"))
    assert is_eligible(Place(text="Remote - Europe"))
    assert not is_eligible(Place(text="Remote - US"))
    assert not is_eligible(Place(text="New York"))
