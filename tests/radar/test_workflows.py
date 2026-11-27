"""Contrôles statiques de l'architecture « code public, exécution privée » (spec 1.3, § 7 et 14).

Rien de ceci ne s'exécute ici : on vérifie les fichiers de workflow, le modèle `examples/vigie-run/`
et la documentation AWS.
"""

import json
import os
import re
import subprocess
import tomllib
from pathlib import Path
from typing import Any

import pytest
import yaml

from vigie.settings import Settings

ROOT = Path(__file__).resolve().parents[2]
PUBLIC = ROOT / ".github" / "workflows"
MODEL = ROOT / "examples" / "vigie-run"
MODEL_WORKFLOWS = MODEL / ".github" / "workflows"
SHA = re.compile(r"^[0-9a-f]{40}$")
SEMVER_TAG = re.compile(r"^v\d+\.\d+\.\d+$")


def load(path: Path) -> dict[Any, Any]:
    data: dict[Any, Any] = yaml.safe_load(path.read_text())
    return data


def all_steps(workflow: dict[Any, Any]) -> list[dict[str, Any]]:
    return [step for job in workflow["jobs"].values() for step in job.get("steps", [])]


def every_workflow_file() -> list[Path]:
    return [
        *PUBLIC.glob("*.yml"),
        *MODEL_WORKFLOWS.glob("*.yml"),
        *MODEL_WORKFLOWS.glob("*.yml.example"),
    ]


def triggers(workflow: dict[Any, Any]) -> dict[str, Any]:
    value = workflow.get("on", workflow.get(True))  # PyYAML lit `on` comme un booléen
    return value if isinstance(value, dict) else {name: None for name in value}


# --- dépôt public : aucun secret, aucune exécution réelle ---------------------------------------


def test_the_public_repository_has_no_scheduled_run_workflow() -> None:
    assert not (PUBLIC / "radar.yml").exists(), (
        "l'exécution planifiée vit dans le dépôt privé vigie-run"
    )
    assert sorted(p.name for p in PUBLIC.glob("*.yml")) == ["ci.yml", "release.yml"]


def test_public_workflows_use_no_secret_but_the_builtin_token() -> None:
    for path in PUBLIC.glob("*.yml"):
        names = set(re.findall(r"secrets\.([A-Za-z0-9_]+)", path.read_text()))
        assert names <= {"GITHUB_TOKEN"}, f"{path.name} : {names}"
        assert "id-token" not in path.read_text()  # pas d'OIDC dans le dépôt public


def test_public_workflows_never_react_to_pull_request_target_or_schedule() -> None:
    for path in PUBLIC.glob("*.yml"):
        assert "pull_request_target" not in triggers(load(path)), path.name
        assert "schedule" not in triggers(load(path)), path.name


def test_no_workflow_anywhere_uses_pull_request_target() -> None:
    for path in every_workflow_file():
        assert "pull_request_target" not in path.read_text(), path.name


def test_every_third_party_action_is_pinned_by_commit_sha() -> None:
    for path in every_workflow_file():
        for step in all_steps(load(path)):
            uses = step.get("uses")
            if uses and not uses.startswith("./"):  # un workflow local n'a pas de version
                _, _, ref = uses.partition("@")
                assert SHA.match(ref), f"{path.name} : {uses} n'est pas épinglée par SHA"


def test_ci_is_read_only_callable_and_checks_the_wheel() -> None:
    ci = load(PUBLIC / "ci.yml")
    assert ci["permissions"] == {"contents": "read"}
    assert "workflow_call" in triggers(ci)
    assert any("check_wheel.py" in s.get("run", "") for s in all_steps(ci))
    [checkout] = [s for s in all_steps(ci) if "actions/checkout" in s.get("uses", "")]
    assert checkout["with"]["persist-credentials"] is False


# --- release.yml ----------------------------------------------------------------------------------


def test_release_runs_on_semver_tags_only_and_reruns_the_ci_first() -> None:
    release = load(PUBLIC / "release.yml")
    tags = triggers(release)["push"]["tags"]
    assert tags == ["v[0-9]+.[0-9]+.[0-9]+"]
    assert release["jobs"]["ci"]["uses"] == "./.github/workflows/ci.yml"
    assert release["jobs"]["release"]["needs"] == "ci"


def test_release_is_the_only_public_job_with_write_access() -> None:
    release = load(PUBLIC / "release.yml")
    assert release["permissions"] == {"contents": "read"}
    assert release["jobs"]["release"]["permissions"] == {"contents": "write"}
    assert "permissions" not in release["jobs"]["ci"]


def test_release_checks_the_tag_before_publishing_and_never_interpolates_it() -> None:
    steps = all_steps(load(PUBLIC / "release.yml"))
    commands = [s["run"] for s in steps if "run" in s]
    assert commands[0].startswith("python3 scripts/check_release_tag.py")
    assert "gh release create" in commands[1]
    assert "--generate-notes" in commands[1]
    assert "--verify-tag" in commands[1]
    assert commands[1].index("gh release view") < commands[1].index("gh release create")
    # injection : le nom du tag passe par l'environnement, pas par le texte du script
    assert all("github.ref_name" not in command for command in commands)
    assert all(s["env"]["TAG"] == "${{ github.ref_name }}" for s in steps if "run" in s)


def run_release_step(tmp_path: Path, view_ok: bool, create_ok: bool = True) -> tuple[int, str, str]:
    """Exécute le vrai bloc `run:` de la release avec un faux `gh` qui journalise ses appels."""
    steps = all_steps(load(PUBLIC / "release.yml"))
    script = next(s["run"] for s in steps if "gh release create" in s.get("run", ""))
    log = tmp_path / "gh.log"
    fake = tmp_path / "gh"
    fake.write_text(
        "#!/bin/sh\n"
        'echo "$@" >> "$GH_LOG"\n'
        'case "$1 $2" in\n'
        f'  "release view") exit {0 if view_ok else 1} ;;\n'
        f'  "release create") exit {0 if create_ok else 1} ;;\n'
        "esac\n"
    )
    fake.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "GH_LOG": str(log),
        "TAG": "v9.9.9",
    }
    # le runner GitHub exécute les étapes avec `bash -e`
    done = subprocess.run(  # noqa: S603
        ["bash", "-e", "-c", script],  # noqa: S607
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    return done.returncode, log.read_text() if log.exists() else "", done.stdout


def test_release_step_does_nothing_if_the_release_already_exists(tmp_path: Path) -> None:
    code, calls, out = run_release_step(tmp_path, view_ok=True)
    assert code == 0
    assert calls.splitlines() == ["release view v9.9.9"]  # aucune création
    assert "existe déjà" in out


def test_release_step_creates_the_release_when_missing(tmp_path: Path) -> None:
    code, calls, _ = run_release_step(tmp_path, view_ok=False)
    assert code == 0
    assert calls.splitlines() == [
        "release view v9.9.9",
        "release create v9.9.9 --title v9.9.9 --generate-notes --verify-tag",
    ]


def test_release_step_still_fails_on_a_real_creation_error(tmp_path: Path) -> None:
    code, calls, _ = run_release_step(tmp_path, view_ok=False, create_ok=False)
    assert code != 0  # une vraie erreur ne doit pas être avalée
    assert "release create" in calls


# --- modèle du dépôt privé : examples/vigie-run/ ---


def radar_model() -> dict[Any, Any]:
    return load(MODEL_WORKFLOWS / "radar.yml")


def test_model_files_are_present() -> None:
    for name in ("README.md", "config/radar.yaml", ".github/workflows/radar.yml"):
        assert (MODEL / name).is_file(), name


def test_model_config_is_the_fictitious_example() -> None:
    assert (MODEL / "config" / "radar.yaml").read_text() == (
        ROOT / "config" / "radar.example.yaml"
    ).read_text()


def test_model_profile_is_the_fictitious_example() -> None:
    assert (MODEL / "profile" / "profile.yaml").read_text() == (
        ROOT / "config" / "profile.example.yaml"
    ).read_text()


def test_model_installs_a_pinned_tag_never_main() -> None:
    workflow = radar_model()
    assert SEMVER_TAG.match(workflow["env"]["VIGIE_REF"])
    commands = [s["run"] for s in all_steps(workflow) if "run" in s]
    install = next(c for c in commands if "uv tool install" in c)
    assert install == 'uv tool install "git+https://github.com/HLeviel/vigie@${VIGIE_REF}"'
    for path in (
        MODEL_WORKFLOWS / "radar.yml",
        MODEL_WORKFLOWS / "radar-state-in-repo.yml.example",
    ):
        text = path.read_text()
        assert "@main" not in text and "@master" not in text
        assert SEMVER_TAG.match(load(path)["env"]["VIGIE_REF"])


def test_model_pinned_tag_is_not_ahead_of_the_package_version() -> None:
    version = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    assert radar_model()["env"]["VIGIE_REF"] == f"v{version}"


def test_model_triggers_permissions_and_concurrency() -> None:
    workflow = radar_model()
    assert set(triggers(workflow)) == {"schedule", "workflow_dispatch"}
    assert triggers(workflow)["schedule"] == [{"cron": "17 5 * * *"}]  # 05:17 UTC
    assert workflow["permissions"] == {"contents": "read", "id-token": "write"}
    assert workflow["concurrency"] == {"group": "radar", "cancel-in-progress": False}
    [job] = workflow["jobs"].values()
    assert job["environment"] == "radar"


def test_model_has_no_aws_keys_and_role_comes_from_a_variable() -> None:
    text = (MODEL_WORKFLOWS / "radar.yml").read_text().lower()
    assert "aws-access-key-id" not in text and "aws-secret-access-key" not in text
    [step] = [
        s for s in all_steps(radar_model()) if "configure-aws-credentials" in s.get("uses", "")
    ]
    assert step["with"]["role-to-assume"] == "${{ vars.AWS_ROLE_ARN }}"


def test_model_checkout_does_not_persist_credentials() -> None:
    [step] = [s for s in all_steps(radar_model()) if "actions/checkout" in s.get("uses", "")]
    assert step["with"]["persist-credentials"] is False


def test_model_secrets_are_only_exposed_to_the_two_steps_that_need_them() -> None:
    workflow = radar_model()
    [job] = workflow["jobs"].values()
    assert "env" in workflow and set(workflow["env"]) == {
        "VIGIE_REF"
    }  # une seule variable, publique
    assert "env" not in job  # pas d'exposition au job entier, donc pas aux actions tierces
    with_secrets = [s for s in job["steps"] if "env" in s]
    assert [s["run"] for s in with_secrets] == [
        "vigie radar check --config config/radar.yaml --ci",
        "vigie radar run --config config/radar.yaml",
    ]
    for step in job["steps"]:
        if "uses" in step:
            assert "secrets." not in json.dumps(step)


def test_model_check_runs_before_run_and_nothing_is_echoed_or_uploaded() -> None:
    text = (MODEL_WORKFLOWS / "radar.yml").read_text()
    commands = [s["run"] for s in all_steps(radar_model()) if "run" in s]
    assert commands.index("vigie radar check --config config/radar.yaml --ci") < commands.index(
        "vigie radar run --config config/radar.yaml"
    )
    for forbidden in ("upload-artifact", "actions/cache", "set -x", "ACTIONS_STEP_DEBUG"):
        assert forbidden not in text
    # le seul `echo` autorisé ajoute un dossier (non secret) au PATH
    echoes = [c for c in commands if "echo" in c]
    assert echoes == ['echo "$(uv tool dir --bin)" >> "$GITHUB_PATH"']


def test_model_secret_names_match_the_settings_fields() -> None:
    text = (MODEL_WORKFLOWS / "radar.yml").read_text()
    names = set(re.findall(r"\$\{\{ secrets\.([A-Z_]+) \}\}", text))
    fields = {name.upper() for name in Settings.model_fields}
    assert names <= fields, f"secrets inconnus de settings.py : {names - fields}"
    required = {"SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASSWORD", "DIGEST_TO"}
    assert required | {"VIGIE_STATE_BUCKET", "ADZUNA_APP_ID", "ADZUNA_APP_KEY"} <= names
    assert (
        "VIGIE_RADAR_CONFIG" not in text
    )  # la config est un fichier du dépôt privé, pas un secret
    assert "VIGIE_RADAR_CONFIG" not in (MODEL / "README.md").read_text()


def test_state_in_repo_fallback_is_the_only_workflow_with_write_access() -> None:
    fallback = load(MODEL_WORKFLOWS / "radar-state-in-repo.yml.example")
    assert fallback["permissions"] == {"contents": "write"}  # et rien d'autre : ni AWS ni OIDC
    assert "id-token" not in json.dumps(fallback["permissions"])
    assert "aws-actions" not in (MODEL_WORKFLOWS / "radar-state-in-repo.yml.example").read_text()
    assert "contents: write" not in (MODEL_WORKFLOWS / "radar.yml").read_text()
    assert "state/radar.db" in (MODEL_WORKFLOWS / "radar-state-in-repo.yml.example").read_text()
    # le fichier d'état est conservé même si la collecte échoue, puis l'échec est rendu visible
    steps = all_steps(fallback)
    assert next(s for s in steps if s.get("id") == "run")["continue-on-error"] is True
    assert steps[-1]["if"] == "steps.run.outcome == 'failure'"


# --- documentation AWS du modèle ---


def aws_doc_blocks() -> list[dict[str, Any]]:
    text = (MODEL / "README.md").read_text()
    blocks = re.findall(r"```json\n(.*?)```", text, re.DOTALL)
    return [json.loads(block) for block in blocks]


def test_aws_trust_policy_is_restricted_to_the_private_repository_environment() -> None:
    trust = next(b for b in aws_doc_blocks() if "Principal" in b["Statement"][0])
    statement = trust["Statement"][0]
    condition = statement["Condition"]["StringEquals"]
    assert statement["Action"] == "sts:AssumeRoleWithWebIdentity"
    # GitHub met les identifiants numériques du propriétaire et du dépôt dans le `sub`
    # (observé sur un vrai job : repo:HLeviel@13158585/vigie-run@1469974815:environment:radar)
    assert (
        condition["token.actions.githubusercontent.com:sub"]
        == "repo:HLeviel@<OWNER_ID>/vigie-run@<REPO_ID>:environment:radar"
    )
    assert condition["token.actions.githubusercontent.com:aud"] == "sts.amazonaws.com"
    assert "*" not in json.dumps(condition)
    assert "HLeviel/vigie:" not in json.dumps(trust)  # jamais le dépôt public


def test_readme_explains_how_to_find_the_real_subject_claim() -> None:
    text = (MODEL / "README.md").read_text()
    assert "Trouver le `sub` réel" in text
    assert "ACTIONS_ID_TOKEN_REQUEST_TOKEN" in text
    assert "<OWNER_ID>" in text and "<REPO_ID>" in text
    assert "jamais le jeton" in text
    # l'étape de debug ne doit pas afficher le jeton : seulement certaines revendications
    snippet = text[text.index("Debug OIDC") :]
    assert "print(json.dumps({k: c.get(k)" in snippet
    assert "print(json.load" not in snippet


def test_aws_permissions_are_limited_to_the_state_object() -> None:
    policy = next(b for b in aws_doc_blocks() if "Principal" not in b["Statement"][0])
    actions = {
        a
        for s in policy["Statement"]
        for a in ([s["Action"]] if isinstance(s["Action"], str) else s["Action"])
    }
    assert actions == {"s3:GetObject", "s3:PutObject", "s3:ListBucket"}
    assert [s["Resource"] for s in policy["Statement"]] == [
        "arn:aws:s3:::<BUCKET>/radar.db",
        "arn:aws:s3:::<BUCKET>",
    ]
    assert policy["Statement"][1]["Condition"] == {"StringEquals": {"s3:prefix": "radar.db"}}
    assert all(s["Effect"] == "Allow" for s in policy["Statement"])


def test_readme_restricts_the_environment_to_main() -> None:
    text = (MODEL / "README.md").read_text()
    assert "uniquement `main`" in text
    assert "privé" in text.lower()


# --- workflows d'annotation et d'évaluation du modèle (module match) ---

ANNOTATION = ("match-label-export.yml", "match-label-import.yml", "match-eval.yml")
PINNED = re.compile(r"^[^@\s]+@[0-9a-f]{40}( # v[0-9.]+)?$")


def annotation_workflow(name: str) -> dict[Any, Any]:
    return load(MODEL_WORKFLOWS / name)


def annotation_runs(name: str) -> list[str]:
    return [s["run"] for s in all_steps(annotation_workflow(name)) if "run" in s]


@pytest.mark.parametrize("name", ANNOTATION)
def test_annotation_workflows_share_the_radar_guard_rails(name: str) -> None:
    workflow = annotation_workflow(name)
    text = (MODEL_WORKFLOWS / name).read_text()
    assert workflow["permissions"] == {"contents": "read", "id-token": "write"}
    assert workflow["concurrency"] == {"group": "radar", "cancel-in-progress": False}
    [job] = workflow["jobs"].values()
    assert job["environment"] == "radar"
    assert "env" not in job and set(workflow["env"]) == {"VIGIE_REF"}
    assert workflow["env"]["VIGIE_REF"] == radar_model()["env"]["VIGIE_REF"]
    assert "pull_request_target" not in text and "@main" not in text
    assert "aws-access-key-id" not in text.lower()
    for forbidden in ("actions/cache", "set -x", "ACTIONS_STEP_DEBUG"):
        assert forbidden not in text
    # toutes les actions sont épinglées par SHA
    assert all(PINNED.match(s["uses"]) for s in all_steps(workflow) if "uses" in s)
    [checkout] = [s for s in all_steps(workflow) if "actions/checkout" in s.get("uses", "")]
    assert checkout["with"]["persist-credentials"] is False
    [aws] = [s for s in all_steps(workflow) if "configure-aws-credentials" in s.get("uses", "")]
    assert aws["with"]["role-to-assume"] == "${{ vars.AWS_ROLE_ARN }}"


@pytest.mark.parametrize("name", ANNOTATION)
def test_annotation_workflows_install_the_pinned_tag_and_echo_nothing_else(name: str) -> None:
    version = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    assert annotation_workflow(name)["env"]["VIGIE_REF"] == f"v{version}"
    commands = annotation_runs(name)
    installs = [c for c in commands if "uv tool install" in c]
    assert installs and all("github.com/HLeviel/vigie@${VIGIE_REF}" in c for c in installs)
    echoes = [c for c in commands if "echo" in c]
    assert echoes == ['echo "$(uv tool dir --bin)" >> "$GITHUB_PATH"']


@pytest.mark.parametrize("name", ANNOTATION)
def test_annotation_secrets_only_reach_the_vigie_step_and_inputs_never_reach_scripts(
    name: str,
) -> None:
    workflow = annotation_workflow(name)
    [job] = workflow["jobs"].values()
    exposed = [s for s in job["steps"] if "secrets." in json.dumps(s)]
    assert len(exposed) == 1 and "vigie match " in exposed[0]["run"]
    assert set(exposed[0]["env"]) <= {"N", "VIGIE_STATE_BUCKET", "OPENROUTER_API_KEY"}
    assert exposed[0]["env"]["VIGIE_STATE_BUCKET"] == "${{ secrets.VIGIE_STATE_BUCKET }}"
    for command in annotation_runs(name):  # une entrée utilisateur n'est jamais dans un script
        assert "${{" not in command and "github.event" not in command


def test_export_workflow_is_manual_and_uploads_a_one_day_artifact() -> None:
    workflow = annotation_workflow("match-label-export.yml")
    assert set(triggers(workflow)) == {"workflow_dispatch"}
    assert triggers(workflow)["workflow_dispatch"]["inputs"]["n"]["default"] == "50"
    [export] = [c for c in annotation_runs("match-label-export.yml") if "match label" in c]
    assert "--export" in export and '--n "$N"' in export and "--import" not in export
    [upload] = [s for s in all_steps(workflow) if "upload-artifact" in s.get("uses", "")]
    assert upload["with"]["retention-days"] == 1
    assert upload["with"]["path"] == "labels-*.csv"  # le CSV seulement


def test_import_workflow_triggers_on_label_files_pushed_to_main() -> None:
    workflow = annotation_workflow("match-label-import.yml")
    assert set(triggers(workflow)) == {"push", "workflow_dispatch"}
    assert triggers(workflow)["push"] == {"branches": ["main"], "paths": ["labels/*.csv"]}
    text = (MODEL_WORKFLOWS / "match-label-import.yml").read_text()
    assert "upload-artifact" not in text  # rien n'est republié


def test_eval_workflow_is_manual_and_uploads_only_the_report() -> None:
    workflow = annotation_workflow("match-eval.yml")
    assert set(triggers(workflow)) == {"workflow_dispatch"}
    assert triggers(workflow)["workflow_dispatch"]["inputs"]["local"]["default"] is False
    [upload] = [s for s in all_steps(workflow) if "upload-artifact" in s.get("uses", "")]
    assert upload["with"]["path"] == "eval/" and upload["with"]["retention-days"] <= 7


def run_import_step(tmp_path: Path, names: list[str]) -> list[str]:
    """Exécute le vrai bloc `run:` d'import avec un faux `vigie` ; renvoie ses arguments."""
    steps = all_steps(annotation_workflow("match-label-import.yml"))
    script = next(s["run"] for s in steps if "--import" in s.get("run", ""))
    (tmp_path / "labels").mkdir()
    for name in names:
        (tmp_path / "labels" / name).write_text("uid;label\n")
    fake = tmp_path / "vigie"
    fake.write_text('#!/bin/sh\nfor a in "$@"; do printf "%s\\n" "$a"; done > "$ARGS_FILE"\n')
    fake.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "ARGS_FILE": str(tmp_path / "args.txt"),
    }
    done = subprocess.run(  # noqa: S603
        ["bash", "-e", "-c", script],  # noqa: S607
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    return (tmp_path / "args.txt").read_text().splitlines()


def test_import_script_passes_every_file_in_order_even_with_spaces(tmp_path: Path) -> None:
    args = run_import_step(
        tmp_path, ["labels-2026-12-02.csv", "labels-2026-11-25.csv", "mon fichier.csv"]
    )
    assert args == [
        "match",
        "label",
        "--config",
        "config/radar.yaml",
        "--import",
        "labels/labels-2026-11-25.csv",
        "--import",
        "labels/labels-2026-12-02.csv",
        "--import",
        "labels/mon fichier.csv",
    ]


def test_model_readme_documents_the_annotation_flow() -> None:
    text = (MODEL / "README.md").read_text()
    for needle in (
        "match-label-export",
        "match-label-import",
        "match-eval",
        "labels/",
        "Upload files",
        "sans score ni rang",
        "`p` ou `pertinent`",
    ):
        assert needle in text, needle


def test_openrouter_key_only_reaches_the_steps_that_call_the_llm() -> None:
    """La clé est un secret de plus : exposée aux seules étapes `vigie radar run` et
    `vigie match eval` (jamais à un workflow qui n'appelle pas le LLM, ni au job entier)."""
    for name in ("radar.yml", "match-eval.yml", "radar-state-in-repo.yml.example"):
        workflow = load(MODEL_WORKFLOWS / name)
        [job] = workflow["jobs"].values()
        assert "env" not in job and "OPENROUTER_API_KEY" not in workflow.get("env", {})
        holders = [s for s in job["steps"] if "OPENROUTER_API_KEY" in s.get("env", {})]
        assert len(holders) == 1, name
        assert holders[0]["env"]["OPENROUTER_API_KEY"] == "${{ secrets.OPENROUTER_API_KEY }}"
        assert holders[0]["run"].startswith(("vigie radar run", "mkdir eval && vigie match eval"))
    for name in ("match-label-export.yml", "match-label-import.yml"):
        assert "OPENROUTER" not in (MODEL_WORKFLOWS / name).read_text()
    assert "openrouter_api_key" in Settings.model_fields  # nom de secret = champ de settings.py


def test_model_readme_documents_the_llm_stage_and_its_privacy() -> None:
    text = (MODEL / "README.md").read_text()
    for needle in (
        "OPENROUTER_API_KEY",
        "match.llm.model",
        'data_collection: "deny"',
        "require_parameters",
        "sans aucune PR dans le dépôt",
        "code 6",
        "sans noms d'entreprise ni dates",
    ):
        assert needle in text, needle
    # aucun identifiant de modèle réel dans la documentation du modèle de dépôt
    assert "<fournisseur>/<modèle>" in text
