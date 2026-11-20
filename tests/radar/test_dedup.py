from datetime import timedelta

from tests.radar.factories import NOW, make_offer
from vigie.radar.dedup import Outcome, classify
from vigie.radar.store import Store


def test_new_then_seen() -> None:
    store = Store.open(":memory:")
    offer = make_offer()
    assert classify(store, offer) == (Outcome.NEW, None)
    store.insert_offer(offer, NOW)
    assert classify(store, offer) == (Outcome.SEEN, None)


def test_other_source_same_key_is_alias() -> None:
    store = Store.open(":memory:")
    store.insert_offer(make_offer(source="adzuna", source_id="1", title="Data Engineer (H/F)"), NOW)
    other = make_offer(
        source="ats:greenhouse", source_id="9", title="data engineer F/H", company="ACME"
    )
    assert classify(store, other) == (Outcome.ALIAS, "adzuna:1")
    store.add_alias(other.uid, "adzuna:1")
    assert classify(store, other) == (Outcome.SEEN, None)


def test_same_source_same_key_stays_distinct() -> None:
    store = Store.open(":memory:")
    store.insert_offer(make_offer(source_id="1"), NOW)
    assert classify(store, make_offer(source_id="2"))[0] is Outcome.NEW


def test_alias_points_to_the_first_seen_offer() -> None:
    store = Store.open(":memory:")
    store.insert_offer(make_offer(source="a", source_id="1"), NOW)
    store.insert_offer(make_offer(source="b", source_id="1"), NOW + timedelta(hours=1))
    assert classify(store, make_offer(source="c", source_id="1")) == (Outcome.ALIAS, "a:1")


def test_missing_department_falls_back_on_city() -> None:
    store = Store.open(":memory:")
    store.insert_offer(
        make_offer(source="adzuna", source_id="1", department=None, location_label="Lyon, Rhône"),
        NOW,
    )
    email = make_offer(
        source="email:apec", source_id="x", department=None, location_label="69 - LYON 03"
    )
    assert classify(store, email) == (Outcome.ALIAS, "adzuna:1")
    other_city = make_offer(
        source="email:apec", source_id="y", department=None, location_label="Paris"
    )
    assert classify(store, other_city)[0] is Outcome.NEW
