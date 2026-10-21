"""Operator command line for retention and erasure (``P6-PRIV-1``).

    python -m app.privacy.cli sweep [--class CLASS ...] [--dry-run]
    python -m app.privacy.cli verify-account --user-id ID [--email ADDRESS]
    python -m app.privacy.cli verify-email --email ADDRESS
    python -m app.privacy.cli verify-organization --organization-id ID [--workspace-id ID ...]
    python -m app.privacy.cli erase-email --email ADDRESS --confirm

Every command prints one JSON document to stdout and runs against the configured
``DATABASE_URL``. Exit status: ``0`` success (and, for a verification, ``verified``);
``1`` a verification found something; ``2`` the sweep met a class whose retention
duration is not configured (nothing of that class was purged); ``3`` the request was
refused (for example an account holds the email being erased).

An address given on the command line can be visible to whoever can read the process
list or the task definition's overrides; prefer running these where that is acceptable.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict

from app.core.errors import SignalNestError
from app.db.session import SessionLocal
from app.privacy import retention
from app.privacy.erasure import erase_invitee_email
from app.privacy.inventory import RetentionClass
from app.privacy.verification import verify_organization_erased, verify_subject_erased

EXIT_OK, EXIT_FINDINGS, EXIT_POLICY_UNSET, EXIT_REFUSED = 0, 1, 2, 3


def _print(document: object) -> None:
    json.dump(document, sys.stdout, indent=2, sort_keys=True, default=str)
    sys.stdout.write("\n")


def _sweep(args: argparse.Namespace) -> int:
    classes = tuple(RetentionClass(c) for c in args.data_class) if args.data_class else None
    outcomes = retention.sweep(SessionLocal, classes=classes, dry_run=args.dry_run)
    _print({"dry_run": args.dry_run, "classes": [asdict(o) for o in outcomes]})
    return EXIT_POLICY_UNSET if any(o.status == "policy_unset" for o in outcomes) else EXIT_OK


def _verify_account(args: argparse.Namespace) -> int:
    with SessionLocal() as db:
        report = verify_subject_erased(db, user_id=args.user_id, email=args.email)
    _print(report.as_dict())
    return EXIT_OK if report.verified else EXIT_FINDINGS


def _verify_email(args: argparse.Namespace) -> int:
    with SessionLocal() as db:
        report = verify_subject_erased(db, email=args.email)
    _print(report.as_dict())
    return EXIT_OK if report.verified else EXIT_FINDINGS


def _verify_organization(args: argparse.Namespace) -> int:
    with SessionLocal() as db:
        report = verify_organization_erased(
            db, organization_id=args.organization_id, workspace_ids=tuple(args.workspace_id)
        )
    _print(report.as_dict())
    return EXIT_OK if report.verified else EXIT_FINDINGS


def _erase_email(args: argparse.Namespace) -> int:
    if not args.confirm:
        _print({"refused": "pass --confirm to erase"})
        return EXIT_REFUSED
    with SessionLocal() as db:
        try:
            counts = erase_invitee_email(db, email=args.email)
        except SignalNestError as exc:
            db.rollback()
            _print({"refused": exc.code})
            return EXIT_REFUSED
        db.commit()
        report = verify_subject_erased(db, email=args.email)
    _print({"counts": counts.as_dict(), "verification": report.as_dict()})
    return EXIT_OK if report.verified else EXIT_FINDINGS


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.privacy.cli", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    sweep = commands.add_parser("sweep", help="run the retention purge")
    sweep.add_argument(
        "--class",
        dest="data_class",
        action="append",
        choices=[c.value for c in RetentionClass],
        help="limit to this class (repeatable); default: every class",
    )
    sweep.add_argument("--dry-run", action="store_true", help="count only; delete nothing")
    sweep.set_defaults(handler=_sweep)

    account = commands.add_parser("verify-account", help="verify an account was erased")
    account.add_argument("--user-id", required=True)
    account.add_argument("--email")
    account.set_defaults(handler=_verify_account)

    email = commands.add_parser("verify-email", help="verify an email address was erased")
    email.add_argument("--email", required=True)
    email.set_defaults(handler=_verify_email)

    organization = commands.add_parser(
        "verify-organization", help="verify an organization was erased"
    )
    organization.add_argument("--organization-id", required=True)
    organization.add_argument("--workspace-id", action="append", default=[])
    organization.set_defaults(handler=_verify_organization)

    erase = commands.add_parser(
        "erase-email", help="erase an invitee who has no account (invitations, audit mentions)"
    )
    erase.add_argument("--email", required=True)
    erase.add_argument("--confirm", action="store_true")
    erase.set_defaults(handler=_erase_email)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.handler(args)


if __name__ == "__main__":  # pragma: no cover - entry point
    sys.exit(main())
