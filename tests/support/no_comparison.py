"""A fixture for the test files that are not about equivalence.

A raw splice inside a module and a logic call (`add_assign` and `connect` with logic, `set_param`,
`replace_module`) are compared with the original at the sign-off (tests/ops/test_raw_splice.py,
tests/ops/test_logic_ops.py). That costs a Yosys run, and the tests that use them to check something else do
not need it. A test file that imports `no_comparison` runs each test as if Yosys were not installed, so that
no comparison runs.

`vet.splice` and the four operations get the waiver "set-up of the test" by themselves when the test gives
none, also in a script that the test runs through the command line. There are some 250 such calls in the tests,
and none of them is about the waiver. The rules themselves are tested in tests/ops/test_raw_splice.py and
tests/ops/test_logic_ops.py, which do not use this fixture.

    from no_comparison import no_comparison  # noqa: F401
"""

from __future__ import annotations

from typing import Any

import pytest

from hdl_vet import equiv
from hdl_vet.ops import Instance, Module, Vet

WAIVER = "set-up of the test"


def _waived(real: Any) -> Any:
    def call(self: Any, *args: Any, **options: Any) -> Any:
        if options.get("waiver") is None:  # also a helper that passes `waiver=None` on
            options["waiver"] = WAIVER
        return real(self, *args, **options)

    return call


@pytest.fixture(autouse=True)
def no_comparison(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(equiv, "available", lambda: False)
    for owner, name in ((Module, "add_assign"), (Instance, "connect"), (Instance, "set_param")):
        monkeypatch.setattr(owner, name, _waived(getattr(owner, name)))
    monkeypatch.setattr(Instance, "replace_module", _waived(Instance.replace_module))
    monkeypatch.setattr(Vet, "splice", _waived(Vet.splice))
