"""Build-baked deployment pins — the values that decide WHICH database is measured.

These four constants are the reader's trust anchor for destination authenticity. They are
baked into the image at ``docker build`` time from the ``EXPECTED_DB_HOST`` /
``EXPECTED_DB_NAME`` / ``EXPECTED_DB_USER`` build args (see the Dockerfile), which the
publication workflow supplies from a protected GitHub environment. Because they live in an
image layer of a digest-pinned, read-only-root, shell-free container, NO ECS ``RunTask``
parameter can reach or replace them: ``ContainerOverride`` has no member that rewrites image
contents, and ``environment`` overrides only touch ``os.environ`` — which this module is
deliberately NOT read from. An image ``ENV`` value WOULD be caller-overridable; a source
constant is not. That distinction is the whole point of this file.

The values committed to git are SENTINELS that fail the reader closed. ``reader.py`` rejects
an empty/malformed ``EXPECTED_DB_HOST`` (and empty name/user) with READER-CONFIG-FAILED
before any connection, so a reader built without real build args — or an accidental
placeholder image — can never certify a schema head. The build regenerates this file and
FAILS if any of the three build args is empty, so a real image never ships these sentinels.
"""

from __future__ import annotations

# Empty -> fails reader.py's host/name/user validation -> READER-CONFIG-FAILED (fail closed).
EXPECTED_DB_HOST = ""
EXPECTED_DB_NAME = ""
EXPECTED_DB_USER = ""

# Fixed path of the committed, checksum-verified AWS RDS global CA bundle inside the image.
# The reader passes this to libpq as ``sslrootcert`` alongside ``sslmode=verify-full``.
CA_BUNDLE_PATH = "/etc/ssl/rds/rds-global-bundle.pem"
