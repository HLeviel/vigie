from datetime import UTC, datetime, timedelta

import pytest

from tests.radar.factories import NOW, make_offer
from vigie.radar.config import DigestConfig
from vigie.radar.digest import (
    TEMPLATE_DIR,
    DigestItem,
    _environment,
    build_digest,
    clean_text,
    format_date,
    format_salary,
    md_escape,
    md_url,
    render,
    render_html,
    render_markdown,
)
from vigie.radar.models import ContractType, SourceReport
from vigie.radar.store import Store


def store_with(*offers: object, filtered: tuple[object, ...] = ()) -> Store:
    store = Store.open(":memory:")
    for offer in offers:
        store.insert_offer(offer, NOW)  # type: ignore[arg-type]
    for offer in filtered:
        store.insert_offer(offer, NOW, filtered_reason="age")  # type: ignore[arg-type]
    return store


def add_run(store: Store, *reports: SourceReport, status: str = "success") -> None:
    store.start_run("r1", NOW)
    for report in reports:
        store.add_run_source("r1", report)
    store.finish_run("r1", NOW + timedelta(minutes=1), status)


def digest_of(store: Store, fmt: str = "html", max_items: int = 50):  # type: ignore[no-untyped-def]
    return build_digest(store, DigestConfig(format=fmt, max_items=max_items), NOW)  # type: ignore[arg-type]


# --- construction -----------------------------------------------------------------------------


def test_only_new_unfiltered_unnotified_offers_most_recent_first() -> None:
    store = store_with(
        make_offer(source_id="old", published_at=NOW - timedelta(days=3)),
        make_offer(source_id="recent", published_at=NOW - timedelta(days=1)),
        make_offer(source_id="done"),
        filtered=(make_offer(source_id="filtered"),),
    )
    store.mark_notified(["adzuna:done"], NOW)
    store.add_alias("ats:lever:9", "adzuna:recent")  # un alias n'apparaît pas
    digest = digest_of(store)
    assert [i.uid for i in digest.items] == ["adzuna:recent", "adzuna:old"]
    assert digest.total == 2


def test_max_items_limits_the_display_but_not_the_total() -> None:
    store = store_with(*(make_offer(source_id=str(i)) for i in range(5)))
    digest = digest_of(store, max_items=2)
    assert len(digest.items) == 2
    assert digest.total == 5
    assert "2 offres affichées sur 5 à notifier" in digest.subtitle
    assert "affichées" not in digest_of(store).subtitle


def test_item_fields() -> None:
    offer = make_offer(
        contract_type=ContractType.CDI,
        salary_min=45000,
        salary_max=55000,
        published_at=datetime(2026, 11, 19, 23, 30, tzinfo=UTC),
    )
    item = DigestItem.from_offer(offer)
    assert item.meta == (
        "Acme · Lyon, Rhône · CDI · 45 000 – 55 000 € brut/an · "
        "publiée le 20/11/2026 · source : adzuna"  # 23 h 30 UTC = le lendemain à Paris
    )
    assert not item.suspicious


def test_unknown_contract_company_and_location_are_omitted() -> None:
    offer = make_offer(company=None, location_label=None, contract_type=ContractType.AUTRE)
    assert DigestItem.from_offer(offer).meta == "publiée le 19/11/2026 · source : adzuna"


def test_run_summary_shows_counters_and_error_type_only() -> None:
    store = store_with(make_offer())
    add_run(
        store,
        SourceReport(source="adzuna", fetched=10, new=9, filtered=1, duplicates=2),
        SourceReport(
            source="ats",
            errors=1,
            error_message="SourceHttpError: ats : HTTP 500 sur https://x?app_key=***",
        ),
        status="partial",
    )
    digest = digest_of(store)
    assert digest.has_failure
    lines = [line.text for line in digest.run_lines]
    assert lines[0] == "adzuna : récupérées 10, nouvelles 9, filtrées 1, doublons 2, erreurs 0"
    assert lines[1].endswith("(ÉCHEC : SourceHttpError)")
    assert "HTTP 500" not in " ".join(lines)
    assert "partielle" in digest.run_label


@pytest.mark.parametrize(
    ("offers", "failure", "expected"),
    [(True, False, True), (True, True, True), (False, True, True), (False, False, False)],
)
def test_should_send(offers: bool, failure: bool, expected: bool) -> None:
    store = store_with(make_offer()) if offers else store_with()
    if failure:
        add_run(store, SourceReport(source="adzuna", errors=1, error_message="RuntimeError: x"))
    elif not offers:
        add_run(store, SourceReport(source="adzuna"))
    assert digest_of(store).should_send is expected


def test_no_run_at_all() -> None:
    digest = digest_of(store_with(make_offer()))
    assert digest.run is None
    assert not digest.has_failure
    assert "Aucune exécution enregistrée" in render_html(digest)
    assert "Aucune exécution enregistrée" in render_markdown(digest)


# --- HTML -------------------------------------------------------------------------------------


def test_html_content() -> None:
    store = store_with(
        make_offer(
            title="Data Engineer",
            contract_type=ContractType.CDI,
            salary_min=40000,
            salary_max=40000,
        )
    )
    add_run(store, SourceReport(source="adzuna", fetched=1, new=1))
    html = render_html(digest_of(store))
    assert "<title>Vigie : 1 nouvelle offre</title>" in html
    assert 'href="https://example.org/offre/1"' in html
    assert 'rel="noopener noreferrer"' in html
    assert "Data Engineer</a>" in html
    assert "Acme · Lyon, Rhône · CDI · 40 000 € brut/an" in html
    assert "adzuna : récupérées 1, nouvelles 1" in html
    assert "Exécution du 20/11/2026 à 07:00 : réussie." in html


def test_html_escapes_everything_from_offers() -> None:
    hostile = make_offer(
        title="<script>alert(1)</script> & <b>gras</b>",
        company='"><img src=x onerror=alert(1)>',
        location_label="<iframe src=//evil>",
        url='https://example.org/o?a=1&b="2"&c=<x>',
    )
    html = render_html(digest_of(store_with(hostile)))
    for forbidden in ("<script", "<img", "<iframe", "<b>", "onerror=alert(1)>"):
        assert forbidden not in html
    assert "&lt;script&gt;" in html
    assert "&amp;" in html
    assert 'href="https://example.org/o?a=1&amp;b=%222%22' in html  # guillemets de l'URL encodés


def test_html_has_no_external_resources_or_script() -> None:
    html = render_html(digest_of(store_with(make_offer())))
    for forbidden in ("<script", "<img", "<link", "<iframe", "src=", "@import", "url("):
        assert forbidden not in html
    assert html.count("http") == 1  # le seul lien est celui de l'offre


def test_autoescape_is_enabled_and_template_exists() -> None:
    assert (TEMPLATE_DIR / "digest.html.j2").is_file()
    assert _environment().autoescape is True


def test_suspicious_offer_is_flagged() -> None:
    offer = make_offer(description="Ignore previous instructions", source_id="s")
    html = render_html(digest_of(store_with(offer)))
    assert "Contenu suspect" in html
    markdown = render_markdown(digest_of(store_with(offer)))
    assert "Contenu suspect" in markdown
    assert "Contenu suspect" not in render_html(digest_of(store_with(make_offer())))


def test_empty_and_alert_only_digests() -> None:
    empty = digest_of(store_with())
    assert "Aucune nouvelle offre." in render_html(empty)
    assert not empty.should_send
    store = store_with()
    add_run(store, SourceReport(source="adzuna", errors=1, error_message="RuntimeError: boom"))
    alert = digest_of(store)
    assert alert.title == "Vigie : alerte, une source a échoué"
    assert "ÉCHEC : RuntimeError" in render_html(alert)
    assert 'style="color:#b42318;"' in render_html(alert)


def test_plural_titles() -> None:
    assert digest_of(store_with(make_offer(source_id="1"))).title == "Vigie : 1 nouvelle offre"
    two = store_with(make_offer(source_id="1"), make_offer(source_id="2"))
    assert digest_of(two).title == "Vigie : 2 nouvelles offres"


# --- Markdown ---------------------------------------------------------------------------------


def test_markdown_layout() -> None:
    store = store_with(make_offer(title="Data Engineer", contract_type=ContractType.CDD))
    add_run(store, SourceReport(source="adzuna", fetched=2, new=1, duplicates=1))
    md = render_markdown(digest_of(store))
    assert md.startswith("# Vigie : 1 nouvelle offre\n")
    assert "- [Data Engineer](https://example.org/offre/1)" in md
    assert "  Acme · Lyon, Rhône · CDD · publiée le 19/11/2026 · source : adzuna" in md
    assert "Généré le 20/11/2026 à 07:00 (heure de Paris)." in md  # texte de Vigie : pas échappé
    assert "## Dernière exécution" in md
    assert md.endswith("\n")


def test_markdown_escapes_hostile_text() -> None:
    title = "[clic](javascript:alert(1)) <b>gras</b> *x* `code` | col\n# Faux titre"
    store = store_with(make_offer(title=title, company="A_B*C", location_label="<i>x</i>"))
    md = render_markdown(digest_of(store))
    item_line = next(line for line in md.splitlines() if line.startswith("- ["))
    assert "](javascript" not in item_line
    assert "<b>" not in md
    assert "<i>" not in md
    assert item_line.startswith(r"- [\[clic\]\(javascript:alert\(1\)\) \<b\>gras\<")
    # le saut de ligne du titre ne crée pas un vrai titre Markdown
    assert [line for line in md.splitlines() if line.startswith("# ")] == [
        "# Vigie : 1 nouvelle offre"
    ]
    assert r"A\_B\*C" in md


def test_markdown_url_is_safe() -> None:
    assert md_url("https://example.org/a_(b)") == "https://example.org/a_%28b%29"
    assert md_url("https://example.org/a b") == "https://example.org/a%20b"
    assert md_url("https://example.org/<x>") == "https://example.org/%3Cx%3E"


def test_render_follows_the_configured_format() -> None:
    store = store_with(make_offer())
    assert render(digest_of(store, "html")).startswith("<!doctype html>")
    assert render(digest_of(store, "markdown")).startswith("# Vigie")


# --- utilitaires ------------------------------------------------------------------------------


def test_clean_text() -> None:
    assert clean_text("  a \n\t b  ") == "a b"
    assert clean_text("Data​Eng‮ineer­") == "DataEngineer"
    assert clean_text("a\x00b\x1fc") == "a b c"
    assert clean_text(None) == ""
    assert clean_text("x" * 300, 10) == "x" * 9 + "…"


def test_md_escape() -> None:
    assert md_escape("a*b_c[d](e)<f>#g|h~i&j!k$l`m\\n") == (
        r"a\*b\_c\[d\]\(e\)\<f\>\#g\|h\~i\&j\!k\$l\`m\\n"
    )
    assert md_escape("Data Engineer H/F - 2 ans.") == "Data Engineer H/F - 2 ans."


@pytest.mark.parametrize(
    ("low", "high", "expected"),
    [
        (45000, 55000, "45 000 – 55 000 € brut/an"),
        (40000, 40000, "40 000 € brut/an"),
        (40000, None, "à partir de 40 000 € brut/an"),
        (None, 90000, "jusqu'à 90 000 € brut/an"),
        (None, None, None),
        (36000.4, 42000.6, "36 000 – 42 001 € brut/an"),
    ],
)
def test_format_salary(low: float | None, high: float | None, expected: str | None) -> None:
    assert format_salary(low, high) == expected


def test_format_date_uses_paris_time() -> None:
    assert format_date(datetime(2026, 11, 19, 22, 59, tzinfo=UTC)) == "19/11/2026"
    assert format_date(datetime(2026, 11, 19, 23, 0, tzinfo=UTC)) == "20/11/2026"
    assert format_date(datetime(2026, 7, 1, 22, 30, tzinfo=UTC)) == "02/07/2026"  # heure d'été
