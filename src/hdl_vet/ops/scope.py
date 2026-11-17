"""The scope of a `Vet`: `scope`, `open_block` and the refusals that depend on it."""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from hdl_vet.ops.common import VetError
from hdl_vet.ops.host import _VetHost
from hdl_vet.signoff import diff_snapshots
from hdl_vet.slang_adapter import AdapterError, Design, InstanceSnap

# Directives a splice in a scope may not hold: each reaches text outside the module.
_REACHING = [
    "include",
    "define",
    "undef",
    "undefineall",
    "celldefine",
    "endcelldefine",
    "line",
    "default_nettype",
    "unconnected_drive",
    "nounconnected_drive",
    "timescale",
    "resetall",
    "pragma",
    "begin_keywords",
    "end_keywords",
]
_DIRECTIVE = re.compile(r"`\s*(" + "|".join(_REACHING) + r")\b")
# comments and string literals are text, whatever they contain
_QUOTED = re.compile(r'//[^\n]*|/\*.*?\*/|"(?:\\.|[^"\\\n])*"', re.DOTALL)


def _under(path: str, roots: Iterable[str]) -> bool:
    """True when `path` is one of `roots` or lies below one."""
    return any(path == r or path.startswith(r + ".") for r in roots)


_Walk = tuple[list[str], list[str], list[str]]  # modules, paths, instance names (see `Vet._walk_path`)


@dataclass(frozen=True)
class _Scope:
    """A declared scope (see `Vet.scope`): what the design looked like when the block was entered."""

    name: str
    roots: tuple[str, ...]  # the paths of the instances of the scope module
    paths: dict[str, list[str]]  # module -> its instance paths on entry
    inside: frozenset[str]  # the modules all of whose instances are under a root
    snap: dict[str, InstanceSnap]
    outside: dict[Path, bytes]  # each file's text on entry, the inside modules cut out
    reaching: Counter[tuple[str, str]]  # (inside module, target) of the names that reach outside, on entry


def _module_ranges(
    design: Design, names: Iterable[str], files: Iterable[Path]
) -> dict[Path, list[tuple[int, int, str]]]:
    """Per file: the byte ranges (from `module` to the end of `endmodule`) of the modules `names`."""
    known = set(files)
    out: dict[Path, list[tuple[int, int, str]]] = {}
    for name in sorted(names):
        try:
            info = design.module(name)
        except AdapterError:
            continue
        key = info.path if info.path in known else info.path.resolve()
        out.setdefault(key, []).append((info.start, info.end, name))
    return out


def _reaching(design: Design, inside: Iterable[str], roots: tuple[str, ...]) -> Counter[tuple[str, str]]:
    """The hierarchical names written in modules of `inside` whose target is not under a root.

    Per (module, target) the most that any one instance of the module holds: another instance of
    the same text is not another reference.
    """
    names = set(inside)
    per_host: dict[str, Counter[tuple[str, str]]] = {}
    for r in design.hierarchical_refs():
        if r.module in names and not _under(r.target, roots):
            per_host.setdefault(r.host, Counter())[(r.module, r.target)] += 1
    out: Counter[tuple[str, str]] = Counter()
    for counts in per_host.values():
        out |= counts
    return out


def _inside_modules(snap: dict[str, InstanceSnap], roots: tuple[str, ...]) -> frozenset[str]:
    """The modules that have an instance and only instances under a root."""
    paths: dict[str, list[str]] = {}
    for p, s in snap.items():
        paths.setdefault(s.module, []).append(p)
    return frozenset(m for m, ps in paths.items() if all(_under(p, roots) for p in ps))


def _outside_text(design: Design, names: Iterable[str], text: dict[Path, bytes]) -> dict[Path, bytes]:
    """Each file's text with the modules `names` cut out (a marker keeps their place)."""
    ranges = _module_ranges(design, names, text)
    out: dict[Path, bytes] = {}
    for path, data in text.items():
        pieces: list[bytes] = []
        at = 0
        for start, end, name in sorted(ranges.get(path, [])):
            pieces += [data[at:start], b"\x00" + name.encode() + b"\x00"]
            at = end
        out[path] = b"".join(pieces) + data[at:]
    return out


class _ScopeOps(_VetHost):
    """`Vet.scope` and what it needs."""

    @contextmanager
    def scope(self, module: str) -> Iterator[None]:
        """`with vet.scope("blk"):` edits are refused outside `blk` and what is instantiated only below it.

        See `tests/ops/test_scope.py` for the rules.
        """
        self._idle("scope")
        st = self._begin("scope")
        self.module(module)  # the errors of `vet.module`
        design = self.session.loaded
        snap = design.snapshot()
        roots = tuple(sorted(p for p, s in snap.items() if s.module == module))
        if not roots:  # the baseline knows an instance that an earlier operation took away
            raise VetError(f"module {module!r} is not elaborated under the top, so it cannot be edited")
        paths: dict[str, list[str]] = {}
        for p in sorted(snap):
            paths.setdefault(snap[p].module, []).append(p)
        inside = _inside_modules(snap, roots)
        text = {p: self.session.text(p) for p in self.session.files}
        scope = _Scope(
            module, roots, paths, inside, snap, _outside_text(design, inside, text), _reaching(design, inside, roots)
        )
        self._scope = scope
        try:
            yield
        except BaseException:
            self._scope = None
            self._rollback(st)
            raise
        self._scope = None
        try:
            self.session.refresh()
            self._sync()
            problem = self._left_scope(scope)
        except AdapterError as e:
            self._rollback(st)
            raise VetError(f"scope {module!r}: the edited design cannot be loaded: {e}") from e
        except BaseException:
            self._rollback(st)
            raise
        if problem is not None:
            self._rollback(st)
            raise VetError(f"scope {module!r}: {problem}")

    def _left_scope(self, scope: _Scope) -> str | None:
        """What changed outside the scope since it was entered, or None."""
        now = self.session.loaded
        after = _outside_text(now, scope.inside, {p: self.session.text(p) for p in scope.outside})
        for path in sorted(after, key=lambda p: p.as_posix()):
            if scope.outside[path] != after[path]:
                return f"{path.name} changed outside the scope (its text beside the modules that are inside)"
        for change in diff_snapshots(scope.snap, now.snapshot()):
            if not _under(change.path, scope.roots):
                return f"the design changed outside the scope: {change.kind} at {change.path}"
        added = _reaching(now, _inside_modules(now.snapshot(), scope.roots), scope.roots) - scope.reaching
        if added:
            module, target = min(added)
            return f"module {module!r} names {target}, which is outside the scope"
        return None

    def open_block(self) -> str | None:
        """What is still open: "scope 'name'", "group", or None."""
        if self._scope is not None:
            return f"scope {self._scope.name!r}"
        return "group" if self._depth else None

    def _refuse_scope(self, op: str, *mods: str, because: str | None = None) -> None:
        """Inside a scope: refuse `op` when it would edit the text of a module that is not inside."""
        sc = self._scope
        if sc is None:
            return
        for m in mods:
            if m in sc.inside:
                continue
            where = [p for p in sc.paths.get(m, []) if not _under(p, sc.roots)]
            at = (
                "not elaborated under the top"
                if not where
                else "the top module"
                if "." not in where[0]
                else f"instantiated at {where[0]}"
            )
            why = "" if because is None else f"; {because}"
            raise VetError(f"{op}: module {m!r} is outside scope {sc.name!r} ({at}){why}")

    def _refuse_scope_with_hosts(self, op: str, mod: str) -> None:
        """As `_refuse_scope`, for an operation that edits `mod` and every module that instantiates it."""
        if self._scope is None:
            return
        self._refuse_scope(op, mod)
        for host in sorted({h for h, _ in self._instances_of(mod)}):
            self._refuse_scope(op, host, because=f"it instantiates {mod!r}, which {op} edits too")

    def _refuse_splice(self, scope: _Scope, path: Path, start: int, end: int, text: bytes) -> None:
        sc = scope.name
        key = path.resolve()
        ranges = _module_ranges(self.session.loaded, scope.inside, self.session.files).get(key, [])
        if not any(a <= start and end <= b for a, b, _ in ranges):
            raise VetError(
                f"splice {path.name}[{start}:{end}]: not inside the text of one module of scope {sc!r} "
                f"(from its `module` keyword to the end of its `endmodule`)"
            )
        found = _DIRECTIVE.search(_QUOTED.sub(" ", text.decode("utf-8", errors="replace")))
        if found is not None:
            raise VetError(
                f"splice {path.name}: the new text has a `{found.group(1)} directive, which would reach "
                f"files outside scope {sc!r}"
            )

    def _scoped_walk(self, op: str, to: str) -> tuple[str, _Walk | None]:
        """Inside a scope: the path from the top and its walk from the scope module down; else `to`, None.

        `to` starts at the scope module; the modules on the walk must all be inside.
        """
        sc = self._scope
        if sc is None:
            return to, None
        if len(sc.roots) != 1:
            raise VetError(
                f"{op}: scope {sc.name!r}: the scope module is elaborated {len(sc.roots)} times "
                f"({', '.join(sc.roots)}); hierarchical operations need it elaborated once"
            )
        segs = str(to).split(".")
        if segs[0] != sc.name:
            raise VetError(f"{op}: in scope {sc.name!r} the path {to!r} must start at the scope module {sc.name!r}")
        if len(segs) == 1:
            raise VetError(f"{op}: {to!r} is the scope module; the path must name an instance below it")
        root = sc.roots[0]
        full = ".".join([root, *segs[1:]])
        cut = root.count(".")
        mods, paths, names = self._walk_path(op, full)
        self._refuse_scope(op, *mods[cut:])
        return full, (mods[cut:], paths[cut:], names[cut:])
