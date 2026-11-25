"""The raw interface of `Vet`: splice, text, expect, permit.

A raw splice needs a proof or a waiver (see `tests/ops/test_raw_splice.py`, rules S1 to S12). `splice` sorts the
call before it queues anything, from the syntax tree through the adapter: the file as it is before the splice,
the spans of its module declarations, and the range of the splice.

- Only blanks and comments change (`equivalent_bytes`): nothing is registered.
- The range lies inside the text of one module and cannot reach beyond it: the module is marked with a
  post-condition of the kind "splice" (its arguments are the module and the waiver). The sign-off compares each
  marked module with the one of the input design (`hdl_vet.prove`).
- Anything else is outside the modules: it needs a waiver, and no comparison is run for it ("outside").

A splice that an operation makes goes through `Vet._queue`, never through here, so it is never marked.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace as replaced
from pathlib import Path

from hdl_vet import equiv
from hdl_vet.ops import args
from hdl_vet.ops.common import VetError, _Plan
from hdl_vet.ops.host import _VetHost
from hdl_vet.session import SessionError
from hdl_vet.slang_adapter import (
    AdapterError,
    equivalent_bytes,
    meets_directive,
    splice_stays,
    touches_define,
    unconfined_reason,
)
from hdl_vet.splice import RawMark, RawSplice, Splice, SpliceError, apply_splices


def _check_waiver(label: str, waiver: object) -> None:
    """Refuse a waiver that is not one line of text that says something."""
    if waiver is None:
        return
    if not isinstance(waiver, str) or not waiver.strip():
        raise VetError(f"{label}: the waiver must be a text that says why the check is not needed")
    if waiver.splitlines() != [waiver]:
        raise VetError(f"{label}: the waiver must be one line")


class _RawOps(_VetHost):
    """`Vet.splice`, `text`, `expect` and `permit`."""

    # --- raw escape hatch ----------------------------------------------------------

    def splice(
        self, path: Path, start: int, end: int, text: bytes, old: bytes | None = None, *, waiver: str | None = None
    ) -> None:
        """Replace the bytes [start, end) of `path` with `text`: the one raw edit, which needs a proof or a waiver."""
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
        _check_waiver(label, waiver)
        if self._scope is not None:
            self._refuse_splice(self._scope, Path(path), start, end, text)
        undo = self._judge_splice(Path(path), start, end, text, old, waiver)
        try:
            self.session._queue_edit(path, start, end, text, old=old)
        except (SpliceError, SessionError) as e:
            undo()
            raise VetError(f"splice {Path(path).name}: {e}") from e
        self._sync()
        self._all = True

    # --- logic operations (tests/ops/test_logic_ops.py, rules L1 to L5) -------------

    def _logic_gate(self, prefix: str, waiver: str | None) -> None:
        """Refuse a logic call that nothing can judge: without Yosys it needs a waiver. Writes nothing."""
        if waiver is None and not equiv.available():
            raise VetError(
                f"{prefix}: the equivalence check needs Yosys, which is not installed (install the extra "
                f"hdl-vet[equiv]); give the call a waiver to go on without the check"
            )

    def _logic_mark(self, module: str, label: str, waiver: str | None) -> None:
        """Register the module that a logic call changed, so the sign-off compares it with the input design."""
        self.session._marks.append(RawMark("logic", module, waiver, label))

    def _logic_commit(
        self, plan: _Plan, prefix: str, module: str, label: str, waiver: str | None, logic: bool = True
    ) -> None:
        """Commit `plan`; when it changes text and is `logic`, refuse it first without a way to judge it, then mark."""
        counts = logic and not plan.skip
        if counts:
            self._logic_gate(prefix, waiver)
        self._commit(plan)
        if counts:
            self._logic_mark(module, label, waiver)

    def _guard_session_splice(
        self, path: Path, start: int, end: int, text: bytes, old: bytes | None
    ) -> Callable[[], None]:
        """`Session.splice` called by a script is a raw splice: sort it as `splice` does (it has no waiver).

        Returns what takes the judgement back, for a splice that the queue then refuses.
        """
        undo = self._judge_splice(path, start, end, text, old, None)
        self._sync()
        self._all = True
        return undo

    def _judge_splice(
        self, path: Path, start: int, end: int, text: bytes, old: bytes | None, waiver: str | None
    ) -> Callable[[], None]:
        """Sort a raw splice, refuse it (VetError) or register its marks, and remember it as waiting.

        Returns what takes all this back, for a splice that the queue then refuses.

        Splices that wait in the same file are judged together with this one, in the coordinates of
        `vet.text()`: they all have ranges in the same text, and the script must not see them move.
        """
        label = f"splice {path.name}"
        try:
            before = self.session.check_splice(path, start, end, old)
        except (SpliceError, SessionError) as e:
            raise VetError(f"{label}: {e}") from e
        key = path.resolve()
        waiting = self.session._waiting.setdefault(key, [])
        saved = ([replaced(w) for w in waiting], list(self.session._marks))

        def undo() -> None:
            self.session._waiting[key] = saved[0]
            self.session._marks[:] = saved[1]

        if end > len(before):
            return undo  # the session refuses the range
        around = tuple(self._modules_around(key, start, end))
        entry = RawSplice(start, end, text, waiver, around)
        parts = [*waiting, entry]  # every raw splice of the file that waits: judged together, whatever module it is in
        try:
            after = apply_splices(before, [Splice(key, w.start, w.end, w.text) for w in parts])
        except SpliceError:
            return undo  # the queue refuses an overlap
        if not touches_define(before, [(w.start, w.end) for w in parts]) and equivalent_bytes(before, after):
            waiting.append(entry)  # blanks and comments, also together with the ones that wait
            return undo
        inside = [w for w in parts if len(w.modules) == 1]
        if len(around) == 1:
            why = unconfined_reason(before[start:end], text)
        elif touches_define(before, [(start, end)]) or not equivalent_bytes(
            before, apply_splices(before, [Splice(key, start, end, text)])
        ):
            why = "the range is not inside the text of one module (it is at file level, or over more than one module)"
        else:
            why = None  # blanks and comments beside the modules: only the whole is judged
        if why is None:
            spans = self._spans(parts, inside) if inside else None
            if spans is None or not splice_stays(before, after, spans):
                why = "the splices do not leave the declarations of the file whole"
        if why is None and meets_directive(before, [(w.start, w.end) for w in parts]):
            why = "a range meets the line of a directive, which reaches the text after it"
        marks: list[RawMark] = []
        if why is None:
            modules = sorted({w.modules[0] for w in inside})
            for name in modules:
                if not self.module_paths(name):
                    raise VetError(f"{label}: module {name!r} is not elaborated under the top, so it cannot be edited")
            # A splice that counts only because of a later one takes the waiver of that one.
            todo = [(w, w.waiver if w.waiver is not None else waiver) for w in inside if not w.marked]
            if not equiv.available() and any(given is None for _, given in todo):
                raise VetError(
                    f"{label}: the equivalence check needs Yosys, which is not installed (install the extra "
                    f"hdl-vet[equiv]); give the splice a waiver to go on without the check"
                )
            for w, given in todo:
                marks.append(RawMark("splice", w.modules[0], given))
                w.marked = True
        else:
            if waiver is None:
                raise VetError(
                    f"{label}: the range [{start}, {end}) is outside the text of one module, so it is not compared "
                    f"and needs a waiver: {why}"
                )
            marks.append(RawMark("outside", self._design_path(key), waiver))
            for w in waiting:
                w.marked = True
            entry.marked = True
        self.session._marks.extend(marks)
        waiting.append(entry)
        return undo

    def _spans(self, parts: list[RawSplice], inside: list[RawSplice]) -> list[tuple[int, int, int, int]] | None:
        """(start, end, new_start, new_end) of each module that one of `parts` lies in; None when a module is unknown.

        The new place counts what every splice before the module and inside it adds or takes away.
        """
        spans: list[tuple[int, int, int, int]] = []
        design = self.session.loaded
        found = []
        for name in sorted({w.modules[0] for w in inside}):
            try:
                info = design.module(name)
            except AdapterError:
                return None
            found.append((info.start, info.end))
        for start, end in sorted(found):
            before_it = sum(len(w.text) - (w.end - w.start) for w in parts if w.end <= start)
            within = sum(
                len(w.text) - (w.end - w.start)
                for w in parts
                if start <= w.start and start < w.end <= end and not w.start == w.end == end
            )
            spans.append((start, end, start + before_it, end + before_it + within))
        return spans

    def _modules_around(self, key: Path, start: int, end: int) -> list[str]:
        """The modules of the file whose text holds [start, end), well inside it (not at its first or last byte)."""
        design = self.session.loaded
        try:
            names = design.defined_in(key)
        except AdapterError:
            return []
        found: list[str] = []
        for name in names:
            try:
                info = design.module(name)
            except AdapterError:
                continue
            if (info.path == key or info.path.resolve() == key) and info.start <= start and end <= info.end:
                if start == end and start in (info.start, info.end):
                    continue  # an insertion beside the module, not in it
                found.append(name)
        return found

    def _design_path(self, key: Path) -> str:
        """The path of the file in the design, with slashes."""
        try:
            return key.relative_to(self.session.layout_root).as_posix()
        except ValueError:
            return key.as_posix()

    def _check_pattern(self, op: str, kind: object, path: object, name: object, exact: object) -> None:
        for arg, given in (("kind", kind), ("path", path), ("name", name)):
            args.text(op, arg, given)
        args.flag(op, "exact", exact)

    def expect(self, kind: str, path: str = "*", name: str = "*", *, exact: bool = False) -> None:
        self._check_pattern("expect", kind, path, name, exact)
        self.session.expect(kind, path, name, exact=exact, script=True)

    def permit(self, kind: str, path: str = "*", name: str = "*", *, exact: bool = False) -> None:
        self._check_pattern("permit", kind, path, name, exact)
        self.session.permit(kind, path, name, exact=exact, script=True)

    def text(self, path: Path) -> bytes:
        if not isinstance(path, (str, Path)):
            raise VetError(f"text {path!r}: the file must be a str or a Path, not {type(path).__name__}")
        try:
            return self.session.text(path)
        except (SpliceError, SessionError) as e:
            raise VetError(f"text {Path(path).name}: {e}") from e
