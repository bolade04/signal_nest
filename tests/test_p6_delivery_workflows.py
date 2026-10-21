"""Structural guards for the four delivery workflows (P6-INF-2, P6-INF-7).

Pins the properties a reviewer would otherwise have to re-derive from each file: manual
dispatch only and no inputs; least-privilege token permissions; the protected environment
each job federates through; serialized, never-cancelled runs; `main`-only execution with
the ref routed through `env`; no expression interpolated into a shell script; operator
identifiers only as environment secrets; no AWS call before the OIDC step; no destructive
or override-bearing command; every `run:` block parses as bash.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"

EXPECTED = {
    "spa-publish.yml": ("publish-spa", "staging-spa-publish", "staging-spa"),
    "spa-rollback.yml": ("rollback-spa", "staging-spa-publish", "staging-spa"),
    "staging-deploy.yml": ("deploy", "staging-deploy", "staging-release"),
    "staging-rollback.yml": ("rollback", "staging-deploy", "staging-release"),
}
OIDC_ACTION = "aws-actions/configure-aws-credentials@v4"
FORBIDDEN_COMMANDS = (
    "--delete", "s3 rm", "s3api delete-object", "delete-objects", "--overrides",
    "create-service", "delete-service", "deregister-task-definition", "delete-task-definitions",
    "stop-task", "execute-command", "put-bucket", "update-distribution", "secretsmanager",
    "--force-new-deployment", "--desired-count", "--network-configuration-override",
)


def load(name: str) -> dict:
    return yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))


def steps(name: str) -> list[dict]:
    job_name = EXPECTED[name][0]
    return load(name)["jobs"][job_name]["steps"]


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_manual_dispatch_only_with_no_inputs(name):
    doc = load(name)
    triggers = doc.get(True, doc.get("on"))  # PyYAML reads the bare key `on` as True
    assert list(triggers) == ["workflow_dispatch"]
    assert triggers["workflow_dispatch"] is None, "inputs are an override channel"


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_least_privilege_token_and_protected_environment(name):
    doc = load(name)
    job_name, environment, group = EXPECTED[name]
    assert doc["permissions"] == {"contents": "read"}
    assert list(doc["jobs"]) == [job_name]
    job = doc["jobs"][job_name]
    assert job["environment"] == environment
    assert job["permissions"] == {"contents": "read", "id-token": "write"}
    assert doc["concurrency"] == {"group": group, "cancel-in-progress": False}
    assert doc["defaults"]["run"]["shell"] == "bash --noprofile --norc -euo pipefail {0}"
    assert isinstance(job.get("timeout-minutes"), int)


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_first_step_refuses_off_main_without_interpolating_the_ref(name):
    first = steps(name)[0]
    assert first["if"] == "github.ref != 'refs/heads/main'"
    assert first["env"] == {"REF": "${{ github.ref }}"}
    assert "${{" not in first["run"] and "exit 1" in first["run"]


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_no_untrusted_expression_inside_a_shell_script(name):
    # `${{ job.status }}` (a fixed enum) is the one expression allowed in a script body; every
    # other value reaches the shell through `env:`, never by text substitution.
    for step in steps(name):
        for expr in re.findall(r"\$\{\{(.*?)\}\}", step.get("run", "")):
            assert expr.strip() == "job.status", (step["name"], expr)


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_operator_identifiers_are_secrets_never_vars(name):
    text = (WORKFLOWS / name).read_text(encoding="utf-8")
    used_vars = set(re.findall(r"vars\.([A-Z_]+)", text))
    assert used_vars <= {"NAME_PREFIX", "DEPLOY_ROLLBACK_FLOOR_COMMIT", "RELEASE_LEDGER_PARAMETER"}
    for secret in re.findall(r"secrets\.([A-Z_]+)", text):
        assert secret != "GITHUB_TOKEN"
    assert "AWS_ACCESS_KEY" not in text and "aws_secret_access_key" not in text.lower()


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_no_aws_call_before_federation(name):
    seen_oidc = False
    for step in steps(name):
        if step.get("uses") == OIDC_ACTION:
            seen_oidc = True
            assert step["with"]["role-to-assume"].startswith("${{ secrets.")
            assert int(step["with"]["role-duration-seconds"]) <= 2700
            continue
        if re.search(r"(^|[\s;(|&])aws\s", step.get("run", "")):
            assert seen_oidc, f"{step['name']} calls AWS before the OIDC step"
    assert seen_oidc


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_no_destructive_or_override_command(name):
    for step in steps(name):
        body = step.get("run", "")
        for forbidden in FORBIDDEN_COMMANDS:
            assert forbidden not in body, (step["name"], forbidden)


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_the_identity_is_verified_right_after_federation(name):
    names = [s.get("name", "") for s in steps(name)]
    index = next(i for i, s in enumerate(steps(name)) if s.get("uses") == OIDC_ACTION)
    assert "Verify the assumed identity" in names[index + 1]


def test_publish_uploads_the_index_last_and_never_deletes():
    names = [s["name"] for s in steps("spa-publish.yml")]
    assets = names.index("Upload every asset first (immutable, never deleted)")
    index = names.index("Upload index.html last, stamped with its lineage")
    invalidate = names.index("Invalidate the index and wait for completion")
    prove = names.index("Prove the live page is the page the CSP guard checked")
    assert assets < index < invalidate < prove


def test_deploy_migrates_before_moving_services_and_records_before_smoke():
    names = [s["name"] for s in steps("staging-deploy.yml")]
    order = [
        "Refuse a release below the AUTH4 rollback floor (offline)",
        "Resolve the published digests for this commit",
        "Read what is running now",
        "Select the OpenTofu template revisions and render the releases (offline checks)",
        "Register the three release revisions",
        "Run the single migration actor and require a verified head",
        "Move both services to the release revisions",
        "Verify every running task reports the target digest",
        "Append the release to the ledger",
        "Post-deploy smoke against the live API",
    ]
    assert [names.index(n) for n in order] == sorted(names.index(n) for n in order)


def test_rollback_never_migrates_and_checks_the_floor_before_any_write():
    body = "\n".join(s.get("run", "") for s in steps("staging-rollback.yml"))
    assert "run-task" not in body and "app.db.migrate" not in body
    names = [s["name"] for s in steps("staging-rollback.yml")]
    floor = names.index("Refuse a target below the AUTH4 rollback floor (offline)")
    write = names.index("Register the revisions and move both services")
    assert floor < write


def _bash_body(run: str) -> str:
    return re.sub(r"\$\{\{.*?\}\}", "placeholder", run)


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not available")
@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_every_run_block_parses_as_bash(name, tmp_path):
    for index, step in enumerate(steps(name)):
        if "run" not in step:
            continue
        script = tmp_path / f"step{index}.sh"
        script.write_text(_bash_body(step["run"]), encoding="utf-8")
        result = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
        assert result.returncode == 0, (step["name"], result.stderr)
