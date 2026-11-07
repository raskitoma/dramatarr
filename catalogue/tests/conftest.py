from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.fetch.types import RawDoc

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> RawDoc:
    """Rebuild a RawDoc from a recorded response.

    Parsers and classifiers take RawDoc rather than an httpx object precisely
    so they can be driven from disk with no network anywhere in the test suite.
    """
    base = FIXTURES / name
    meta = json.loads(base.with_suffix(".meta.json").read_text())
    return RawDoc(
        url=meta["url"],
        status=meta["status"],
        headers=meta.get("headers", {}),
        body=base.with_suffix(".body").read_bytes(),
    )


@pytest.fixture
def fixture() -> callable:
    return load_fixture
