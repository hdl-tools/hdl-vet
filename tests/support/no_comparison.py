"""A fixture for the test files that use a raw splice as set-up and are not about equivalence.

A raw splice inside a module is compared with the original at the sign-off (tests/ops/test_raw_splice.py).
That costs a Yosys run, and the tests that use `vet.splice` only to prepare something else do not need it.
A test file that imports `no_comparison` runs each test as if Yosys were not installed, so that no comparison
runs. A raw splice in such a file then needs a waiver: `waiver="set-up of the test"`.

    from no_comparison import no_comparison  # noqa: F401
"""

from __future__ import annotations

import pytest

from hdl_vet import equiv


@pytest.fixture(autouse=True)
def no_comparison(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(equiv, "available", lambda: False)
