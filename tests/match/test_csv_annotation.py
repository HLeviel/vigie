"""Annotation par CSV : export, tableur, import (spec `match` section 9)."""

import csv
import io
import stat
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tests.match.cli_helpers import fill_offers, invoke, stored_labels
from tests.radar.factories import make_offer
from vigie.match.evaluation.csvio import (
    BOM,
    COLUMNS,
    CsvError,
    export_csv,
    neutralize,
    parse_label,
    read_import,
)
from vigie.radar.store import Store


def read_rows(path: Path) -> list[dict[str, str]]:
    text = path.read_bytes().decode("utf-8")
    assert text.startswith(BOM)
    return list(csv.DictReader(io.StringIO(text[1:], newline=""), delimiter=";"))


def write_csv(path: Path, rows: list[list[str]], delimiter: str = ";", bom: bool = True) -> None:
    buffer = io.StringIO(newline="")
    csv.writer(buffer, delimiter=delimiter, lineterminator="\r\n").writerows(rows)
    path.write_bytes(((BOM if bom else "") + buffer.getvalue()).encode("utf-8"))


# --- export ---------------------------------------------------------------------------------------


def many_offers(count: int = 30) -> list:  # type: ignore[type-arg]
    return [
        make_offer(
            source_id=f"{i:02d}",
            title=f"Offre numéro {i:02d}",
            description=f"texte {i}",
            published_at=datetime(2026, 11, 19, 8, 0, tzinfo=UTC),
        )
        for i in range(count)
    ]


def test_export_format(tmp_path: Path) -> None:
    out = tmp_path / "labels-2026-11-25.csv"
    assert export_csv(many_offers(), out) == 30
    raw = out.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")  # BOM UTF-8 : Excel en français l'ouvre directement
    assert b"\r\n" in raw  # RFC 4180
    rows = read_rows(out)
    assert tuple(rows[0]) == COLUMNS
    assert {r["label"] for r in rows} == {""}  # colonne vide à remplir
    assert rows[0]["publiée"] == "19/11/2026"
    assert rows[0]["contrat"] == "AUTRE" and rows[0]["source"] == "adzuna"
    assert len({r["uid"] for r in rows}) == 30


def test_export_has_no_score_rank_or_position_signal(tmp_path: Path) -> None:
    out = tmp_path / "labels.csv"
    offers = many_offers(40)
    export_csv(offers, out)
    header = out.read_text(encoding="utf-8-sig").splitlines()[0].lower()
    assert "score" not in header and "rang" not in header and "rank" not in header
    uids = [r["uid"] for r in read_rows(out)]
    assert uids != sorted(uids)  # ordre aléatoire, pas l'ordre de l'échantillon
    assert sorted(uids) == sorted(o.uid for o in offers)


def test_export_file_is_private(tmp_path: Path) -> None:
    out = tmp_path / "labels.csv"
    export_csv(many_offers(3), out)
    assert stat.S_IMODE(out.stat().st_mode) == 0o600


def test_excerpt_is_cut_to_500_characters_without_line_breaks(tmp_path: Path) -> None:
    out = tmp_path / "labels.csv"
    long_text = "ligne un\nligne deux\r\n" + "mot " * 400
    export_csv([make_offer(description=long_text)], out)
    [row] = read_rows(out)
    assert len(row["extrait"]) <= 500
    assert "\n" not in row["extrait"] and "\r" not in row["extrait"]
    assert row["extrait"].startswith("ligne un ligne deux")


@pytest.mark.parametrize(
    "dangerous", ['=HYPERLINK("http://evil")', "+33 1 23", "-2+3", "@SUM(A1)", "\t=cmd", "\r=cmd"]
)
def test_formula_injection_is_neutralized_in_every_offer_cell(
    tmp_path: Path, dangerous: str
) -> None:
    offer = make_offer(
        title=dangerous,
        company=dangerous,
        location_label=dangerous,
        description=dangerous,
    )
    out = tmp_path / "labels.csv"
    export_csv([offer], out)
    [row] = read_rows(out)
    for column in ("titre", "entreprise", "lieu", "extrait"):
        assert row[column].startswith("'"), column
        assert not row[column].lstrip("'").startswith(("\t", "\r"))
    assert row["uid"] == offer.uid  # l'uid, lui, reste intact : il sert à l'import


def test_neutralize_leaves_normal_text_and_removes_invisible_characters() -> None:
    assert neutralize("Data Engineer") == "Data Engineer"
    assert neutralize("Data​ ‮Engineer\x07") == "Data Engineer"
    assert neutralize("5 ans = mieux") == "5 ans = mieux"  # seul le début compte
    assert neutralize("  =x") == "'=x"  # espace de tête nettoyé, puis formule détectée


def test_quotes_and_semicolons_survive_the_round_trip(tmp_path: Path) -> None:
    offer = make_offer(title='Titre ; avec "guillemets"', description="a;b;c")
    out = tmp_path / "labels.csv"
    export_csv([offer], out)
    [row] = read_rows(out)
    assert row["titre"] == 'Titre ; avec "guillemets"' and row["extrait"] == "a;b;c"


# --- étiquettes -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("cell", "label"),
    [
        ("p", "pertinent"), ("P", "pertinent"), ("pertinent", "pertinent"),
        ("PERTINENT", "pertinent"), (" Pertinent ", "pertinent"),
        ("m", "peut-être"), ("M", "peut-être"), ("peut-etre", "peut-être"),
        ("peut-être", "peut-être"), ("Peut-Être", "peut-être"),
        ("n", "non pertinent"), ("N", "non pertinent"), ("non", "non pertinent"),
        ("NON", "non pertinent"),
    ],
)  # fmt: skip
def test_accepted_labels(cell: str, label: str) -> None:
    assert parse_label(cell) == label


@pytest.mark.parametrize("cell", ["x", "oui", "non pertinent", "pertinente", "1", "peutetre", "pp"])
def test_refused_labels(cell: str) -> None:
    assert parse_label(cell) is None


# --- lecture du CSV annoté ------------------------------------------------------------------------


def test_read_import_with_and_without_bom_and_both_separators(tmp_path: Path) -> None:
    rows = [["uid", "label", "titre"], ["adzuna:A", "p", "x"], ["adzuna:B", "", "y"]]
    for delimiter in (";", ","):
        for bom in (True, False):
            path = tmp_path / f"f{ord(delimiter)}{bom}.csv"
            write_csv(path, rows, delimiter, bom)
            parsed = read_import(path)
            assert (parsed.read, parsed.skipped, len(parsed.rows)) == (2, 1, 1), (delimiter, bom)
            assert parsed.rows[0].uid == "adzuna:A" and parsed.rows[0].label == "pertinent"


def test_read_import_reports_line_numbers_and_reasons_only(tmp_path: Path) -> None:
    path = tmp_path / "f.csv"
    write_csv(
        path,
        [["uid", "label"], ["a", "p"], ["b", "SECRET-CONTENU"], ["", "p"], ["c", ""], ["d", "n"]],
    )
    parsed = read_import(path)
    assert parsed.errors == [(3, "étiquette invalide"), (4, "uid manquant")]
    assert [r.uid for r in parsed.rows] == ["a", "d"]
    assert "SECRET-CONTENU" not in repr(parsed.errors)


def test_read_import_ignores_blank_lines_and_other_columns(tmp_path: Path) -> None:
    path = tmp_path / "f.csv"
    write_csv(path, [["titre", "label", "uid", "extra"], ["", "", "", ""], ["T", "m", "u1", "z"]])
    parsed = read_import(path)
    assert parsed.read == 1 and [(r.uid, r.label) for r in parsed.rows] == [("u1", "peut-être")]


@pytest.mark.parametrize("content", [b"", b"   \n", b"titre;extrait\nA;B\n", b"uid\nx\n"])
def test_unusable_files_are_refused(tmp_path: Path, content: bytes) -> None:
    path = tmp_path / "f.csv"
    path.write_bytes(content)
    with pytest.raises(CsvError):
        read_import(path)


def test_wrong_encoding_missing_file_and_oversized_cell(tmp_path: Path) -> None:
    bad = tmp_path / "latin1.csv"
    bad.write_bytes("uid;label\nA;é\n".encode("latin-1"))
    with pytest.raises(CsvError) as exc:
        read_import(bad)
    assert "UTF-8" in str(exc.value)
    with pytest.raises(CsvError):
        read_import(tmp_path / "absent.csv")
    huge = tmp_path / "huge.csv"
    huge.write_text("uid;label\nA;" + "x" * 200_000 + "\n", encoding="utf-8")
    with pytest.raises(CsvError):
        read_import(huge)


# --- commandes ------------------------------------------------------------------------------------


def export_via_cli(setup: dict[str, Path], name: str = "labels.csv", *extra: str) -> Path:
    out = setup["tmp"] / name
    result = invoke(setup, "label", "--export", str(out), *extra)
    assert result.exit_code == 0, result.output
    return out


def annotate(path: Path, answers: list[str]) -> None:
    """Remplit la colonne `label` du CSV exporté, comme le ferait un tableur."""
    rows = read_rows(path)
    for row, answer in zip(rows, answers, strict=False):
        row["label"] = answer
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=COLUMNS, delimiter=";", lineterminator="\r\n")
    writer.writeheader()
    writer.writerows(rows)
    path.write_bytes((BOM + buffer.getvalue()).encode("utf-8"))


def test_export_then_import_round_trip(setup: dict[str, Path]) -> None:
    fill_offers(setup)
    out = export_via_cli(setup)
    assert len(read_rows(out)) == 4
    annotate(out, ["p", "m", "n", ""])
    result = invoke(setup, "label", "--import", str(out))
    assert result.exit_code == 0, result.output
    assert (
        "lignes lues : 4, annotées : 3, ignorées (non annotées) : 1, en erreur : 0" in result.output
    )
    assert "total 3 dont 1 pertinente(s) (objectif : 150 dont 30)" in result.output
    assert sorted(stored_labels(setup).values()) == ["non pertinent", "pertinent", "peut-être"]


def test_modified_other_columns_have_no_effect(setup: dict[str, Path]) -> None:
    fill_offers(setup)
    out = export_via_cli(setup)
    rows = read_rows(out)
    for row in rows:
        row["titre"] = "TITRE MODIFIÉ DANS LE TABLEUR"
        row["entreprise"] = "autre"
    rows[0]["label"] = "p"
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=COLUMNS, delimiter=";", lineterminator="\r\n")
    writer.writeheader()
    writer.writerows(rows)
    out.write_text(BOM + buffer.getvalue(), encoding="utf-8")
    assert invoke(setup, "label", "--import", str(out)).exit_code == 0
    with Store.open(setup["db"]) as store:
        titles = {o.title for o in store.retained_offers()}
    assert "TITRE MODIFIÉ DANS LE TABLEUR" not in titles
    assert list(stored_labels(setup).values()) == ["pertinent"]


def test_errors_are_reported_by_line_and_reason_and_do_not_stop_the_import(
    setup: dict[str, Path],
) -> None:
    fill_offers(setup)
    path = setup["tmp"] / "annotated.csv"
    write_csv(
        path,
        [
            ["uid", "label"],
            ["adzuna:A", "p"],
            ["uid-secret-inconnu", "p"],
            ["adzuna:B", "SECRET-CONTENU"],
            ["adzuna:C", "n"],
        ],
    )
    result = invoke(setup, "label", "--import", str(path))
    assert result.exit_code == 0, result.output
    assert "annotated.csv ligne 3 : uid inconnu" in result.output
    assert "annotated.csv ligne 4 : étiquette invalide" in result.output
    assert "en erreur : 2" in result.output and "annotées : 2" in result.output
    for secret in ("uid-secret-inconnu", "SECRET-CONTENU", "Ingénieur IA"):
        assert secret not in result.output
    assert set(stored_labels(setup)) == {"adzuna:A", "adzuna:C"}


def test_dry_run_only_counts(setup: dict[str, Path]) -> None:
    fill_offers(setup)
    path = setup["tmp"] / "a.csv"
    write_csv(path, [["uid", "label"], ["adzuna:A", "p"], ["adzuna:B", "x"]])
    result = invoke(setup, "label", "--import", str(path), "--dry-run")
    assert result.exit_code == 0
    assert "lignes lues : 2, annotées : 1" in result.output and "dry-run" in result.output
    assert stored_labels(setup) == {}


def test_latest_label_replaces_and_later_files_win(setup: dict[str, Path]) -> None:
    fill_offers(setup)
    first, second = setup["tmp"] / "a.csv", setup["tmp"] / "b.csv"
    write_csv(first, [["uid", "label"], ["adzuna:A", "p"], ["adzuna:B", "p"]])
    write_csv(second, [["uid", "label"], ["adzuna:A", "n"]])
    result = invoke(setup, "label", "--import", str(first), "--import", str(second))
    assert result.exit_code == 0, result.output
    assert stored_labels(setup) == {"adzuna:A": "non pertinent", "adzuna:B": "pertinent"}


def test_unusable_import_file_exits_1(setup: dict[str, Path]) -> None:
    fill_offers(setup)
    bad = setup["tmp"] / "bad.csv"
    bad.write_text("titre;extrait\nA;B\n", encoding="utf-8")
    result = invoke(setup, "label", "--import", str(bad))
    assert result.exit_code == 1 and "colonne uid ou label absente" in result.output


def test_export_only_offers_not_yet_annotated_unless_relabel(
    setup: dict[str, Path],
) -> None:
    fill_offers(setup)
    out = export_via_cli(setup)
    annotate(out, ["p", "p", "", ""])
    invoke(setup, "label", "--import", str(out))
    again = export_via_cli(setup, "second.csv")
    assert len(read_rows(again)) == 2  # seulement les deux non annotées
    relabel = export_via_cli(setup, "third.csv", "--relabel", "--n", "5")
    assert len(read_rows(relabel)) == 2  # seulement les deux annotées


def test_export_n_and_nothing_to_export(setup: dict[str, Path]) -> None:
    fill_offers(setup)
    assert len(read_rows(export_via_cli(setup, "a.csv", "--n", "2"))) == 2
    out = setup["tmp"] / "none.csv"
    full = export_via_cli(setup, "all.csv")
    annotate(full, ["p"] * 4)
    invoke(setup, "label", "--import", str(full))
    result = invoke(setup, "label", "--export", str(out))
    assert "aucune offre à annoter" in result.output and not out.exists()


def test_incompatible_options(setup: dict[str, Path]) -> None:
    fill_offers(setup)
    f = str(setup["tmp"] / "x.csv")
    assert invoke(setup, "label", "--export", f, "--import", f).exit_code == 1
    assert invoke(setup, "label", "--dry-run").exit_code == 1
    assert invoke(setup, "label", "--import", f, "--n", "3").exit_code == 1


# --- état S3 (workflows de vigie-run) -------------------------------------------------------------


def with_remote_state(setup: dict[str, Path], monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    from tests.radar.test_state import BUCKET, FakeS3
    from vigie.match import cli
    from vigie.radar.state import S3State

    fill_offers(setup)
    fake = FakeS3(setup["db"].read_bytes())
    setup["db"].unlink()  # le poste de travail n'a plus la base : elle vient de S3
    monkeypatch.setattr(cli, "_state", lambda settings: S3State(BUCKET, client=fake))
    return fake


def test_export_reads_the_state_from_s3_and_writes_nothing_back(
    setup: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = with_remote_state(setup, monkeypatch)
    result = invoke(setup, "label", "--export", str(setup["tmp"] / "out.csv"))
    assert result.exit_code == 0, result.output
    assert "état S3 : téléchargé" in result.output
    assert fake.puts == []  # un export ne modifie pas la base


def test_import_rewrites_the_state_with_a_conditional_write(
    setup: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = with_remote_state(setup, monkeypatch)
    path = setup["tmp"] / "a.csv"
    write_csv(path, [["uid", "label"], ["adzuna:A", "p"]])
    result = invoke(setup, "label", "--import", str(path))
    assert result.exit_code == 0, result.output
    assert "état S3 : synchronisé" in result.output
    assert len(fake.puts) == 1 and fake.puts[0]["IfMatch"] == '"v1"'
    # dry-run : lecture seule
    again = invoke(setup, "label", "--import", str(path), "--dry-run")
    assert again.exit_code == 0 and len(fake.puts) == 1


def test_s3_read_failure_exits_5(setup: dict[str, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.radar.test_state import client_error

    fake = with_remote_state(setup, monkeypatch)
    fake.get_error = client_error("AccessDenied")
    result = invoke(setup, "label", "--export", str(setup["tmp"] / "out.csv"))
    assert result.exit_code == 5


def test_s3_push_conflict_exits_5(setup: dict[str, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.radar.test_state import client_error

    fake = with_remote_state(setup, monkeypatch)
    fake.put_error = client_error("PreconditionFailed", "PutObject")
    path = setup["tmp"] / "a.csv"
    write_csv(path, [["uid", "label"], ["adzuna:A", "p"]])
    assert invoke(setup, "label", "--import", str(path)).exit_code == 5


def test_a_failed_command_does_not_push_the_state(
    setup: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = with_remote_state(setup, monkeypatch)
    bad = setup["tmp"] / "bad.csv"
    bad.write_text("titre;extrait\nA;B\n", encoding="utf-8")
    assert invoke(setup, "label", "--import", str(bad)).exit_code == 1
    assert fake.puts == []


def test_s3_absent_state_means_no_database(
    setup: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.radar.test_state import BUCKET, FakeS3
    from vigie.match import cli
    from vigie.radar.state import S3State

    monkeypatch.setattr(cli, "_state", lambda settings: S3State(BUCKET, client=FakeS3()))
    result = invoke(setup, "label", "--export", str(setup["tmp"] / "out.csv"))
    assert result.exit_code == 1 and "aucune base" in result.output
