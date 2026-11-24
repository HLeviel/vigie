from pathlib import Path

import pytest

from vigie.match.profile import (
    ProfileError,
    canonical_skill,
    load_profile_file,
    load_profile_text,
    normalize_skill,
)

EXAMPLE = Path(__file__).parents[2] / "config" / "profile.example.yaml"

MINIMAL = """
summary: Ingénieur data.
target_roles: [data engineer]
skills:
  must_have: [Python]
"""


def test_example_profile_is_valid() -> None:
    profile = load_profile_file(EXAMPLE)
    assert profile.skills.must_have == [
        "python",
        "sql",
        "retrieval augmented generation",
        "amazon web services",
    ]
    assert profile.experiences[0].end is None
    assert profile.preferences.min_salary == 55000


def test_skills_are_normalized_and_use_synonyms() -> None:
    assert normalize_skill("  Machine   Learning ") == "machine learning"
    assert normalize_skill("Données") == "donnees"
    assert canonical_skill("ML") == "machine learning"
    assert canonical_skill("LLM") == "large language model"
    assert canonical_skill("AWS") == "amazon web services"
    assert canonical_skill("Rust") == "rust"  # sans synonyme : forme normalisée


def test_duplicates_after_synonyms_are_merged() -> None:
    profile = load_profile_text(MINIMAL.replace("[Python]", "[ML, machine learning, Python]"))
    assert profile.skills.must_have == ["machine learning", "python"]


def test_skill_cannot_be_wanted_and_avoided() -> None:
    text = MINIMAL + "  avoid: [python]\n"
    with pytest.raises(ProfileError):
        load_profile_text(text)
    # y compris par synonyme
    with pytest.raises(ProfileError):
        load_profile_text(MINIMAL.replace("[Python]", "[AWS]") + "  avoid: [amazon web services]\n")


def test_nice_to_have_does_not_repeat_must_have() -> None:
    profile = load_profile_text(MINIMAL + "  nice_to_have: [python, sql]\n")
    assert profile.skills.nice_to_have == ["sql"]


def test_strict_schema_rejects_unknown_keys() -> None:
    with pytest.raises(ProfileError) as exc:
        load_profile_text(MINIMAL + "extra: 1\n")
    assert "extra" in str(exc.value)


def test_errors_never_repeat_the_value() -> None:
    secret = "valeur-confidentielle-xyz"
    bad = f"""
summary: {secret}
target_roles: [a]
skills: {{must_have: [x]}}
preferences: {{remote: [{secret}], min_salary: {secret}, languages: [{secret}]}}
experiences:
  - {{title: t, start: {secret}}}
"""
    with pytest.raises(ProfileError) as exc:
        load_profile_text(bad)
    assert secret not in str(exc.value)
    assert "preferences.remote" in str(exc.value)


@pytest.mark.parametrize(
    "extra",
    [
        "preferences: {min_salary: 0}",
        "preferences: {min_salary: -1}",
        "preferences: {languages: [fra]}",
        "preferences: {contract_types: [CDX]}",
        "experiences: [{title: t, start: 2021}]",
        "experiences: [{title: t, start: 2021-13}]",
        "experiences: [{title: t, start: 2021-01-15}]",
        "experiences: [{title: t, start: 2021-05, end: 2020-01}]",
    ],
)
def test_invalid_values(extra: str) -> None:
    with pytest.raises(ProfileError):
        load_profile_text(MINIMAL + extra + "\n")


def test_empty_texts_and_roles_are_refused() -> None:
    with pytest.raises(ProfileError):
        load_profile_text("summary: '  '\ntarget_roles: [a]\nskills: {}\n")
    with pytest.raises(ProfileError):
        load_profile_text("summary: a\ntarget_roles: []\nskills: {}\n")
    with pytest.raises(ProfileError):
        load_profile_text("summary: a\ntarget_roles: ['  ']\nskills: {}\n")
    with pytest.raises(ProfileError):
        load_profile_text("summary: a\ntarget_roles: [a]\nskills: {must_have: ['  ']}\n")


def test_yaml_errors_give_position_only() -> None:
    with pytest.raises(ProfileError) as exc:
        load_profile_text("summary: [secret-oups\n")
    assert "ligne" in str(exc.value)
    assert "secret-oups" not in str(exc.value)
    with pytest.raises(ProfileError):
        load_profile_text("- une\n- liste\n")


def test_unreadable_file(tmp_path: Path) -> None:
    with pytest.raises(ProfileError):
        load_profile_file(tmp_path / "absent.yaml")


def test_hash_is_stable_across_formatting_and_list_order() -> None:
    a = load_profile_text(MINIMAL + "  nice_to_have: [sql, docker]\n")
    b = load_profile_text(
        """
target_roles: [data engineer]
summary: "Ingénieur   data."
skills:
  nice_to_have: [Docker, SQL]
  must_have: [python]
"""
    )
    assert a.profile_hash == b.profile_hash
    assert len(a.profile_hash) == 64


def test_hash_changes_with_the_content() -> None:
    base = load_profile_text(MINIMAL).profile_hash
    assert load_profile_text(MINIMAL.replace("data engineer", "ml engineer")).profile_hash != base
    assert load_profile_text(MINIMAL.replace("Python", "Rust")).profile_hash != base
    assert load_profile_text(MINIMAL + "preferences: {min_salary: 50000}\n").profile_hash != base
    # l'ordre des expériences compte
    two = MINIMAL + "experiences: [{title: a, start: 2020-01}, {title: b, start: 2019-01}]\n"
    swapped = MINIMAL + "experiences: [{title: b, start: 2019-01}, {title: a, start: 2020-01}]\n"
    assert load_profile_text(two).profile_hash != load_profile_text(swapped).profile_hash
