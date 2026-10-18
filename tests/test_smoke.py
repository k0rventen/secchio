"""Smoke test: the whole operator wiring imports cleanly.

`handlers` registers every kopf handler on import and mounts the FastAPI app, so a
successful import catches broken wiring, bad imports and annotation errors that a
unit test would not.
"""

from __future__ import annotations


def test_import_handlers() -> None:
    import handlers

    assert handlers.app is not None
