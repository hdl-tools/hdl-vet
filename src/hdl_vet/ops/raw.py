"""The raw interface of `Vet`: splice, text, expect, permit."""

from __future__ import annotations

from pathlib import Path

from hdl_vet.ops.common import VetError
from hdl_vet.ops.host import _VetHost
from hdl_vet.session import SessionError
from hdl_vet.splice import SpliceError


class _RawOps(_VetHost):
    """`Vet.splice`, `text`, `expect` and `permit`."""

    # --- raw escape hatch ----------------------------------------------------------

    def splice(self, path: Path, start: int, end: int, text: bytes, old: bytes | None = None) -> None:
        label = f"splice {Path(path).name}" if isinstance(path, (str, Path)) else f"splice {path!r}"
        if not isinstance(path, (str, Path)):
            raise VetError(f"{label}: the file must be a str or a Path, not {type(path).__name__}")
        for what, value in (("start", start), ("end", end)):
            if type(value) is not int:
                raise VetError(f"{label}: {what} must be an int, not {type(value).__name__}")
        if start < 0 or end < start:
            raise VetError(f"{label}: the range [{start}, {end}) is not valid")
        if not isinstance(text, bytes):
            raise VetError(f"{label}: text must be bytes, not {type(text).__name__}")
        if old is not None and not isinstance(old, bytes):
            raise VetError(f"{label}: old must be bytes, not {type(old).__name__}")
        if self._scope is not None:
            self._refuse_splice(self._scope, Path(path), start, end, text)
        try:
            self.session.splice(path, start, end, text, old=old)
        except (SpliceError, SessionError) as e:
            raise VetError(f"splice {Path(path).name}: {e}") from e
        self._sync()
        self._all = True

    def expect(self, kind: str, path: str = "*", name: str = "*", *, exact: bool = False) -> None:
        self.session.expect(kind, path, name, exact=exact)

    def permit(self, kind: str, path: str = "*", name: str = "*", *, exact: bool = False) -> None:
        self.session.permit(kind, path, name, exact=exact)

    def text(self, path: Path) -> bytes:
        if not isinstance(path, (str, Path)):
            raise VetError(f"text {path!r}: the file must be a str or a Path, not {type(path).__name__}")
        try:
            return self.session.text(path)
        except (SpliceError, SessionError) as e:
            raise VetError(f"text {Path(path).name}: {e}") from e
