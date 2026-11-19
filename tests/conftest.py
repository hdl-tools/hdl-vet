"""What every test gets, whether it asks or not.

`vet apply` looks for `hdl-vet.toml` in the current directory. A test that does not
change directory inherits the one pytest was started in, which is normally the root of
this repository. A project file there is then read by every such test: a run of
`tests/cli` with one in place failed ten tests (#64).

So each test starts in a directory of its own, where no project file can be found. A
test that wants a particular directory still calls `monkeypatch.chdir`, and that wins,
because this fixture has already run by then.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _away_from_any_project_file(tmp_path_factory: pytest.TempPathFactory) -> Iterator[None]:
    """Run each test in an empty directory, so `hdl-vet.toml` is never found by accident."""
    here = Path.cwd()
    os.chdir(tmp_path_factory.mktemp("cwd"))
    try:
        yield
    finally:
        os.chdir(here)
