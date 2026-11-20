"""Contrôles statiques des workflows et de la documentation AWS (aucune exécution possible ici)."""

import json
import re
from pathlib import Path
from typing import Any

import yaml

from vigie.settings import Settings

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github" / "workflows"
SHA = re.compile(r"^[0-9a-f]{40}$")


def load(name: str) -> dict[Any, Any]:
    data: dict[Any, Any] = yaml.safe_load((WORKFLOWS / name).read_text())
    return data


def all_steps(workflow: dict[Any, Any]) -> list[dict[str, Any]]:
    return [step for job in workflow["jobs"].values() for step in job["steps"]]


def test_every_third_party_action_is_pinned_by_commit_sha() -> None:
    for path in WORKFLOWS.glob("*.yml"):
        for step in all_steps(load(path.name)):
            if "uses" in step:
                _, _, ref = step["uses"].partition("@")
                assert SHA.match(ref), f"{path.name} : {step['uses']} n'est pas épinglée par SHA"


def test_radar_permissions_are_minimal() -> None:
    assert load("radar.yml")["permissions"] == {"contents": "read", "id-token": "write"}
    assert load("ci.yml")["permissions"] == {"contents": "read"}


def test_schedule_is_shipped_commented_out() -> None:
    workflow = load("radar.yml")
    triggers = workflow.get("on", workflow.get(True))
    assert set(triggers) == {"workflow_dispatch"}
    text = (WORKFLOWS / "radar.yml").read_text()
    assert re.search(r"^\s*# schedule:", text, re.MULTILINE)
    assert re.search(r'^\s*#\s+- cron: "17 5 \* \* \*"', text, re.MULTILINE)  # 05:17 UTC
    assert "forks" in text


def test_single_run_in_the_radar_environment_on_this_repository_only() -> None:
    workflow = load("radar.yml")
    assert workflow["concurrency"] == {"group": "radar", "cancel-in-progress": False}
    [job] = workflow["jobs"].values()
    assert job["environment"] == "radar"
    assert job["if"] == "github.repository == 'HLeviel/vigie'"


def test_no_aws_keys_and_oidc_role_comes_from_a_variable() -> None:
    text = (WORKFLOWS / "radar.yml").read_text()
    assert "aws-access-key-id" not in text.lower()
    assert "aws-secret-access-key" not in text.lower()
    [step] = [
        s for s in all_steps(load("radar.yml")) if "configure-aws-credentials" in s.get("uses", "")
    ]
    assert step["with"]["role-to-assume"] == "${{ vars.AWS_ROLE_ARN }}"


def test_checkout_does_not_persist_credentials() -> None:
    [step] = [s for s in all_steps(load("radar.yml")) if "actions/checkout" in s.get("uses", "")]
    assert step["with"]["persist-credentials"] is False


def test_secrets_are_only_exposed_to_the_two_steps_that_need_them() -> None:
    workflow = load("radar.yml")
    [job] = workflow["jobs"].values()
    assert "env" not in workflow
    assert "env" not in job  # pas d'exposition au job entier, donc pas aux actions tierces
    with_secrets = [s for s in job["steps"] if "env" in s]
    assert [s["run"] for s in with_secrets] == [
        "uv run vigie radar check --ci",
        "uv run vigie radar run",
    ]
    for step in job["steps"]:
        if "uses" in step:
            assert "secrets." not in json.dumps(step)


def test_check_runs_before_run_and_no_secret_is_echoed_or_uploaded() -> None:
    steps = all_steps(load("radar.yml"))
    commands = [s["run"] for s in steps if "run" in s]
    assert commands.index("uv run vigie radar check --ci") < commands.index(
        "uv run vigie radar run"
    )
    text = (WORKFLOWS / "radar.yml").read_text()
    for forbidden in ("upload-artifact", "actions/cache", "echo ", "set -x", "ACTIONS_STEP_DEBUG"):
        assert forbidden not in text
    assert "digest" not in " ".join(s.get("uses", "") for s in steps)  # jamais publié en artefact


def test_workflow_secrets_match_the_settings_fields() -> None:
    text = (WORKFLOWS / "radar.yml").read_text()
    names = set(re.findall(r"\$\{\{ secrets\.([A-Z_]+) \}\}", text))
    fields = {name.upper() for name in Settings.model_fields}
    assert names <= fields, f"secrets inconnus de settings.py : {names - fields}"
    # tout ce que `check --ci` exige doit être fourni au workflow
    required = {
        "SMTP_HOST",
        "SMTP_PORT",
        "SMTP_USER",
        "SMTP_PASSWORD",
        "DIGEST_TO",
        "VIGIE_STATE_BUCKET",
    }
    assert required <= names
    assert {"VIGIE_RADAR_CONFIG", "ADZUNA_APP_ID", "ADZUNA_APP_KEY"} <= names


def aws_doc_blocks() -> list[dict[str, Any]]:
    text = (ROOT / "docs" / "aws-setup.md").read_text()
    blocks = re.findall(r"```json\n(.*?)```", text, re.DOTALL)
    return [json.loads(block) for block in blocks]


def test_aws_trust_policy_is_restricted_to_the_repository_environment() -> None:
    trust = next(b for b in aws_doc_blocks() if "Principal" in b["Statement"][0])
    statement = trust["Statement"][0]
    condition = statement["Condition"]["StringEquals"]
    assert statement["Action"] == "sts:AssumeRoleWithWebIdentity"
    assert (
        condition["token.actions.githubusercontent.com:sub"]
        == "repo:HLeviel/vigie:environment:radar"
    )
    assert condition["token.actions.githubusercontent.com:aud"] == "sts.amazonaws.com"
    assert "*" not in json.dumps(condition)


def test_aws_permissions_are_limited_to_the_state_object() -> None:
    policy = next(b for b in aws_doc_blocks() if "Principal" not in b["Statement"][0])
    actions = {
        a
        for s in policy["Statement"]
        for a in ([s["Action"]] if isinstance(s["Action"], str) else s["Action"])
    }
    assert actions == {"s3:GetObject", "s3:PutObject", "s3:ListBucket"}
    resources = [s["Resource"] for s in policy["Statement"]]
    assert resources == ["arn:aws:s3:::<BUCKET>/radar.db", "arn:aws:s3:::<BUCKET>"]
    assert policy["Statement"][1]["Condition"] == {"StringEquals": {"s3:prefix": "radar.db"}}
    assert all(s["Effect"] == "Allow" for s in policy["Statement"])
