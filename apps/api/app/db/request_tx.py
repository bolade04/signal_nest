"""Commit a write route's transaction before its response is sent (O9, FU-267-F7).

``get_db`` commits after its ``yield``. A generator dependency is request-scoped by default
in FastAPI, and FastAPI closes request-scoped dependencies only after the response has been
sent (``fastapi/routing.py``, ``request_response``). A route that relies on that commit
therefore answers 2xx before its write is durable:

* a commit that then fails still leaves the client with its 2xx, and nothing persisted;
* a read sent right after the 2xx can see the state from before the write.

:func:`commit_before_response` is a **function-scoped** dependency over the request's own
``get_db`` session. FastAPI closes function-scoped dependencies after the endpoint has
returned and its response has been serialized, and before ``http.response.start``. Its
commit therefore runs before the client sees anything: a failed commit becomes the generic
500 (``get_db`` rolls back, nothing persists), and a read sent after a 2xx sees the write.

Every route that writes through ``get_db`` declares ``db: Session = WriteDb``. A route that
must commit at a particular point of its own (a credential boundary, a pause cut-off) keeps
its explicit ``db.commit()``; a second commit on an empty transaction is harmless.
``app/tests/test_o9_write_route_guard.py`` enforces this for every served route.
"""

from __future__ import annotations

from collections.abc import Iterator

from fastapi import Depends
from sqlalchemy.orm import Session

from app.db.session import get_db


def commit_before_response(db: Session = Depends(get_db)) -> Iterator[Session]:
    """The request's ``get_db`` session, committed after the route and its response
    serialization and BEFORE the response is sent (FastAPI dependency scope ``"function"``).

    The session is the one ``get_db`` opened for this request, so it is shared with
    ``get_tenant_context`` and every other dependency that asks for ``get_db``, and a test's
    ``dependency_overrides[get_db]`` still applies.

    This never catches an exception. An exception raised by the route (including a refusal
    raised after a flush) skips the commit and reaches ``get_db``, which rolls back; a commit
    that fails propagates the same way and becomes a 500.

    Two rules:

    * Never use this, or :data:`WriteDb`, as a sub-dependency of a request-scoped dependency
      that yields. FastAPI refuses that at route registration (``DependencyScopeError``),
      and the commit would no longer come before the response.
    * The commit runs before the first byte of a streaming response. A streaming generator
      that writes while it streams must commit those writes itself.
    """
    yield db
    db.commit()


#: For every route that writes: ``db: Session = WriteDb``.
WriteDb = Depends(commit_before_response, scope="function")
