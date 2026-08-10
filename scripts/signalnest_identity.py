"""THE authoritative source for SignalNest account, partition and boundary identity.

Gate 4N-I7, Defect 1. The temporary-operator generator and the boundary generator each
built the boundary ARN independently. Falsifying one while the other stayed correct left
539/539 tests green, because every test handed the falsified value back as its own request
context — the wrong construction validated itself. That is the Gate 4N-I2
impossible-boundary-ARN defect, reproduced by duplication.

Every consumer — both policy generators, the bootstrap-operator generator, the resource
oracle, the rollout contract, the module inputs and all tests — imports from here. No
other file may hardcode the boundary name, path, or an ARN template for it.

The one deliberate exception is `expected_boundary_arn()` in tests, which rebuilds the ARN
from partition + account + path + name using the AWS managed-policy ARN rule. That is the
independent cross-check; it must NOT import BOUNDARY_POLICY_ARN.
"""

from __future__ import annotations

# --- account and partition ----------------------------------------------------------

# GATE 4N-I18, SEC-1. The account is NO LONGER A LITERAL IN THE REPOSITORY.
#
# Gate 4N-I17's security lane blocked the commit because the real account id sat here and
# in nine other files, and `git log --all -S` proved it appears nowhere in history — so the
# gate package would have been its first disclosure into permanent git history. The account
# now arrives from the declared anchor tier, exactly like every other piece of external
# identity evidence:
#
#   TIER_1_SYNTHETIC  -> the documentation placeholder in tests/fixtures/synthetic-anchor.json
#   TIER_2_PROTECTED  -> the real account, supplied through the protected channel with a
#                        separately-supplied expected hash
#
# There is deliberately NO literal fallback. An undeclared tier is an error, not a quiet
# downgrade — a fallback is what let the Gate 4N-I10 "clean checkout" read a developer-local
# anchor and report a portability it had never tested.
PARTITION = "aws"


def _resolve_account() -> str:
    import anchor_loader
    return anchor_loader.load(anchor_loader.declared_tier()).anchor["approved_account_id"]


ACCOUNT = _resolve_account()
REGION = "us-east-1"

PROJECT = "SignalNest"
ENVIRONMENT = "staging"
# Mirrors local.name_prefix in infra/aws/locals.tf: "${lower(var.project_name)}-${var.environment}"
PREFIX = f"{PROJECT.lower()}-{ENVIRONMENT}"

# --- boundary identity ---------------------------------------------------------------

BOUNDARY_POLICY_NAME = f"{PREFIX}-role-boundary"
# IAM managed-policy paths must begin and end with "/". The default path is bare "/", and
# the ARN then contains no path segment at all — a real source of ARN mismatches, so it is
# stated explicitly rather than assumed.
BOUNDARY_POLICY_PATH = "/"
BOUNDARY_VERSION_ID = "2026-07-31.1"

# --- the eight repository-managed roles ----------------------------------------------

MODULE_IAM_ROLE_NAMES = (
    f"{PREFIX}-ecs-execution",
    f"{PREFIX}-api-task",
    f"{PREFIX}-worker-task",
    f"{PREFIX}-migration-task",
    f"{PREFIX}-ci-publisher",
)
REVISION_READER_ROLE_NAMES = (
    f"{PREFIX}-revision-reader-publisher",
    f"{PREFIX}-revision-reader-execution",
    f"{PREFIX}-revision-reader-runner",
)
ALL_ROLE_NAMES = MODULE_IAM_ROLE_NAMES + REVISION_READER_ROLE_NAMES

READER_EXECUTION_ROLE_NAME = f"{PREFIX}-revision-reader-execution"


# --- ARN construction (one implementation, used everywhere) --------------------------


def iam_policy_arn(name: str, *, path: str = "/", account: str = ACCOUNT,
                   partition: str = PARTITION) -> str:
    """Build an IAM managed-policy ARN per the AWS rule.

    A path of "/" contributes nothing to the ARN; any other path appears verbatim between
    "policy" and the name.
    """
    if not path.startswith("/") or not path.endswith("/"):
        raise ValueError(f"IAM policy path must start and end with '/': {path!r}")
    middle = "" if path == "/" else path.strip("/") + "/"
    return f"arn:{partition}:iam::{account}:policy/{middle}{name}"


def iam_role_arn(name: str, *, account: str = ACCOUNT, partition: str = PARTITION) -> str:
    return f"arn:{partition}:iam::{account}:role/{name}"


BOUNDARY_POLICY_ARN = iam_policy_arn(BOUNDARY_POLICY_NAME, path=BOUNDARY_POLICY_PATH)

ALL_ROLE_ARNS = tuple(iam_role_arn(n) for n in ALL_ROLE_NAMES)
READER_EXECUTION_ROLE_ARN = iam_role_arn(READER_EXECUTION_ROLE_NAME)

BOOTSTRAP_OPERATOR_NAME = "SignalNestBoundaryBootstrapOperator"


def identity_summary() -> dict:
    """A serialisable snapshot, for artifacts and cross-consumer reconciliation."""
    return {
        "partition": PARTITION,
        "account": ACCOUNT,
        "region": REGION,
        "prefix": PREFIX,
        "boundary_policy_name": BOUNDARY_POLICY_NAME,
        "boundary_policy_path": BOUNDARY_POLICY_PATH,
        "boundary_policy_arn": BOUNDARY_POLICY_ARN,
        "boundary_version_id": BOUNDARY_VERSION_ID,
        "role_names": list(ALL_ROLE_NAMES),
        "role_arns": list(ALL_ROLE_ARNS),
        "reader_execution_role_arn": READER_EXECUTION_ROLE_ARN,
        "bootstrap_operator_name": BOOTSTRAP_OPERATOR_NAME,
    }


# --- THE AUTHORITATIVE CRITICAL-RESOURCE LAYER (Gate 4N-I10, Defect 5) ------------------
#
# Policy generators were still rebuilding critical ARNs independently:
# gen_boundary_policy.py constructed SECRETS_CMK, STATE_CMK, STATE_BUCKET, AUDIT_BUCKET,
# LOCK_TABLE and TRAIL as its own f-strings, and every test then probed those values FROM
# THE GENERATOR — the same self-witnessing shape as the Gate 4N-I7 boundary-ARN defect, one
# layer down. Gate 4N-I8 made them WITNESSED by the external anchor join; it did not make
# them SINGLE-SOURCED.
#
# They live here now. A generator may import; it may not reconstruct. scripts/ has a static
# audit (tests/test_critical_arn_audit.py) that fails on a literal "arn:aws" or a critical
# name template inside a policy generator.
#
# PROVENANCE, per identifier:
#   AWS-ASSIGNED   bucket suffixes and KMS key ids cannot be derived from the repository.
#                  They come from the live read-only inventory captured in earlier gates and
#                  are corroborated by scripts/resource_oracle.py against SOURCE B.
#   REPO-DERIVED   everything built from PREFIX, which is itself resolved from locals.tf.

# GATE 4N-I18, SEC-1. AWS-ASSIGNED identifiers are NO LONGER LITERALS IN THE REPOSITORY.
#
# Every value below carries a provider-generated suffix, a KMS key id or an operator-supplied
# name. Gate 4N-I17's security lane established by `git log --all -S` that none of them
# appears anywhere in git history, so committing the gate package would have disclosed all of
# them permanently. They now resolve from the DECLARED TIER, exactly like ACCOUNT above:
# Tier 1 yields the tracked synthetic fixture's values (mechanism only), Tier 2 yields the
# real inventory supplied through the protected channel with a separately-supplied hash.
#
# A generator may import these; it may not reconstruct them. The static audit in
# tests/test_critical_arn_audit.py still fails on a literal "arn:aws" or a critical name
# template inside a policy generator.


def _inventory():
    import protected_inventory
    return protected_inventory.load()


_INV = _inventory()


def _live(field: str):
    value, present = _INV.dig(field)
    if not present or value is None:
        raise RuntimeError(
            f"the resolved inventory has no {field!r}. A missing AWS-assigned identifier is "
            "an error, never a value silently reconstructed from a naming convention.")
    return value


# AWS-ASSIGNED — provider-generated bucket_prefix suffixes; not derivable from the repository.
STATE_BUCKET_NAME = _live("buckets_by_role.tfstate")
AUDIT_BUCKET_NAME = _live("buckets_by_role.audit")
APP_BUCKET_NAME = _live("buckets_by_role.app")
SPA_BUCKET_NAME = _live("buckets_by_role.spa")
ALB_LOGS_BUCKET_NAME = _live("buckets_by_role.alb")

# AWS-ASSIGNED — KMS key ids, read out of the alias table rather than pasted.
STATE_CMK_KEY_ID = _live("aliases.1.1")
SECRETS_CMK_KEY_ID = _live("aliases.0.1")

# OPERATOR-SUPPLIED — the lock table name arrives through a git-ignored tfvars at bootstrap
# and is recorded in the inventory; it must NOT be re-derived from a naming convention.
LOCK_TABLE_NAME = _live("lock_table_name")

# AWS-ASSIGNED — CloudFront ids. These previously lived in infra/aws/cloudfront-expected.json,
# which Gate 4N-I17's security lane flagged for the same first-disclosure reason; that file is
# now contained and its identifiers are carried by the tier-resolved inventory.
CLOUDFRONT_DISTRIBUTION_ID = _live("cloudfront.distribution_id")
CLOUDFRONT_OAC_ID = _live("cloudfront.oac_id")

# AWS-ASSIGNED — the Route53 hosted zone. Gate 4N-I27Y's aws-permissions lane found this id
# hardcoded as a bare literal in gen_operator_policies.py, verify_closure.py AND
# tests/test_operator_policies.py. Those three agreed only with each other, so "CLOSURE: clean"
# and the policy tests were SELF-ATTESTING on this one identity — the Gate 4N-I7 Defect 1 shape.
# leak_scan could not see it either: a `Z`+20-alphanumeric zone id matches none of its patterns,
# and `arn:aws:route53:::hostedzone/...` carries no account segment. It is now carried by the
# tier-resolved inventory like every other AWS-assigned identifier, so the repository holds only
# a synthetic value and the real one arrives, if ever, through the authorized Tier-2 path.
ROUTE53_HOSTED_ZONE_ID = _live("route53.hosted_zone_id")

# REPO-DERIVED.
STATE_OBJECT_KEY = f"{PREFIX}/root.tfstate"
TRAIL_NAME = f"{PREFIX}-audit"
SECRETS_PREFIX_PATH = f"{PREFIX}/"


def route53_hosted_zone_arn(*, partition: str = PARTITION) -> str:
    """The hosted-zone ARN, built from the tier-resolved id rather than a repository literal."""
    return f"arn:{partition}:route53:::hostedzone/{ROUTE53_HOSTED_ZONE_ID}"


def s3_bucket_arn(bucket: str, *, partition: str = PARTITION) -> str:
    return f"arn:{partition}:s3:::{bucket}"


def kms_key_arn(key_id: str, *, region: str = REGION, account: str = ACCOUNT,
                partition: str = PARTITION) -> str:
    return f"arn:{partition}:kms:{region}:{account}:key/{key_id}"


def regional_arn(service: str, resource: str, *, region: str = REGION,
                 account: str = ACCOUNT, partition: str = PARTITION) -> str:
    return f"arn:{partition}:{service}:{region}:{account}:{resource}"


STATE_BUCKET_ARN = s3_bucket_arn(STATE_BUCKET_NAME)
STATE_OBJECTS_ARN = f"{STATE_BUCKET_ARN}/*"
STATE_OBJECT_ARN = f"{STATE_BUCKET_ARN}/{STATE_OBJECT_KEY}"
AUDIT_BUCKET_ARN = s3_bucket_arn(AUDIT_BUCKET_NAME)
AUDIT_OBJECTS_ARN = f"{AUDIT_BUCKET_ARN}/*"
APP_BUCKET_ARN = s3_bucket_arn(APP_BUCKET_NAME)
STATE_CMK_ARN = kms_key_arn(STATE_CMK_KEY_ID)
SECRETS_CMK_ARN = kms_key_arn(SECRETS_CMK_KEY_ID)
LOCK_TABLE_ARN = regional_arn("dynamodb", f"table/{LOCK_TABLE_NAME}")
TRAIL_ARN = regional_arn("cloudtrail", f"trail/{TRAIL_NAME}")
SECRETS_PREFIX_ARN = regional_arn("secretsmanager", f"secret:{SECRETS_PREFIX_PATH}*")


def critical_resources() -> dict:
    """Every critical identity, for the witness reconciliation artifact."""
    return {
        "state_bucket": STATE_BUCKET_ARN,
        "state_objects": STATE_OBJECTS_ARN,
        "state_object": STATE_OBJECT_ARN,
        "audit_bucket": AUDIT_BUCKET_ARN,
        "audit_objects": AUDIT_OBJECTS_ARN,
        "app_bucket": APP_BUCKET_ARN,
        "state_cmk": STATE_CMK_ARN,
        "secrets_cmk": SECRETS_CMK_ARN,
        "lock_table": LOCK_TABLE_ARN,
        "trail": TRAIL_ARN,
        "secrets_prefix": SECRETS_PREFIX_ARN,
        "boundary_policy": BOUNDARY_POLICY_ARN,
        **{f"role:{n}": a for n, a in zip(ALL_ROLE_NAMES, ALL_ROLE_ARNS)},
    }


READER_TASK_DEFINITION_FAMILY = f"{PREFIX}-revision-reader"
READER_TASK_DEFINITION_ARNS = regional_arn(
    "ecs", f"task-definition/{READER_TASK_DEFINITION_FAMILY}:*")
READER_ECR_REPOSITORY_PATH = f"{PREFIX}/revision-reader"
READER_ECR_ARN = regional_arn("ecr", f"repository/{READER_ECR_REPOSITORY_PATH}")

GITHUB_OIDC_PROVIDER_ARN = (
    f"arn:{PARTITION}:iam::{ACCOUNT}:oidc-provider/token.actions.githubusercontent.com")
