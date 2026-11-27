"""`Vet`: the session of a script, its bookkeeping and the checked operations."""

from __future__ import annotations

import weakref
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import replace
from functools import partial
from pathlib import Path
from typing import Any

from hdl_vet.archive import ArchiveError, read_archive, write_archive
from hdl_vet.ops import args
from hdl_vet.ops.checks import _DAMAGE, _evaluate, _read_vet_state
from hdl_vet.ops.common import OpRecord, VetError, _Check, _Plan, _VetState
from hdl_vet.ops.hier import _HierOps
from hdl_vet.ops.module import Module
from hdl_vet.ops.plans import _PlanOps
from hdl_vet.ops.raw import _RawOps
from hdl_vet.ops.scope import _Scope, _ScopeOps
from hdl_vet.ops.text import _did_you_mean
from hdl_vet.prove import LogicOp, Outside, Proof, prove
from hdl_vet.session import Result, Session, SessionError, describe
from hdl_vet.slang_adapter import AdapterError, Design, InstanceInfo, ModuleInfo


class Vet(_RawOps, _ScopeOps, _PlanOps, _HierOps):
    def __init__(self, session: Session) -> None:
        held = session._vet() if session._vet is not None else None
        if held is not None:
            raise VetError("session: this session has a Vet already; a session has one Vet")
        session._vet = weakref.ref(self)
        self.session = session
        self.log: list[OpRecord] = []
        self._script_dir: Path | None = None  # folder of the running script, set by the CLI per script
        self.notes: list[str] = []
        self._depth = 0  # open `group` blocks (a checked operation counts as one while it runs)
        self._scope: _Scope | None = None  # the open `scope` block, if any
        # (module, instance) -> modules the instance's baseline path had before `replace_module`
        self._swapped: dict[tuple[str, str], set[str]] = {}
        # (module, signal) pairs that a removed connection or assignment used to drive
        self._drove: list[tuple[str, str]] = []
        self._checks: list[_Check] = []  # in the order registered; a key is there once
        self._dirty: set[tuple[str, ...]] = set()
        self._all = False
        # What this run added, per module: an `add_*` consults it to skip or to report drift.
        # `_sync` clears it on every re-load, and nothing removes a single entry any more
        # (#57 deleted `_Plan.unadded`). That is safe only because every removal re-loads
        # before the next addition reads this, through `_begin` or `_refresh_all_dirty`. An
        # addition that stopped re-loading would read a stale entry here
        # and skip, leaving the sign-off to pass a design without the item.
        self._added: dict[str, dict[str, tuple[str, str]]] = {}
        self._seen = session.reloads
        self._paths: dict[str, list[str]] | None = None
        self._snap_cache: dict[str, Any] | None = None
        self._gen_cache: dict[str, tuple[Path, bytes, list[tuple[str, str, bool]]]] = {}
        self._gen_headers: tuple[bytes, ...] | None = None
        session._splice_guard = self._guard_session_splice

    def __copy__(self) -> Vet:
        raise VetError("a Vet cannot be copied: its session is one state, and a copy would share it")

    def __deepcopy__(self, memo: dict[int, Any]) -> Vet:
        raise VetError("a Vet cannot be copied: its session is one state, and a copy would share it")

    def __reduce_ex__(self, protocol: Any) -> Any:
        raise VetError("a Vet cannot be copied or pickled: its session is one state")

    @property
    def design(self) -> Design:
        design = self.session.design
        self._sync()
        return design

    def finish(self, scripts: Iterable[tuple[str, str]] = ()) -> Result:
        if self._scope is not None:
            raise VetError("finish: not allowed inside a scope")
        scripts = args.scripts("finish", scripts)
        result = self.session.finish([*self.session.scripts, *scripts], self._evaluators(), self._proof())
        return replace(result, notes=list(self.notes))

    def _idle(self, op: str) -> None:
        """Refuse `op` inside a `group` block and after `finish`."""
        if self._depth:
            raise VetError(f"{op}: not allowed inside a group")
        if self._scope is not None:
            raise VetError(f"{op}: not allowed inside a scope")
        if self.session.finished:
            raise VetError(f"{op}: the session is already finished")

    def check(self, scripts: Iterable[tuple[str, str]] = ()) -> Result:
        """What `finish` would return now, without finishing: text, queue and operations are as before.

        Only `session.reloads` advances. What this Vet knows is waiting for a re-load (dirty marks,
        names added since the last load) is kept: the rolled-back session is as unloaded as before.
        """
        self._idle("check")
        scripts = args.scripts("check", scripts)
        self._sync()
        dirty, everything = set(self._dirty), self._all
        added = {mod: dict(names) for mod, names in self._added.items()}
        try:
            result = self.session.check([*self.session.scripts, *scripts], self._evaluators(), self._proof())
            return replace(result, notes=list(self.notes))
        finally:
            self._dirty, self._all, self._added = dirty, everything, added
            self._seen = self.session.reloads

    # --- saved sessions ------------------------------------------------------------

    def save(self, path: Path | str, scripts: Iterable[tuple[str, str]] = ()) -> None:
        """Apply and load what is queued, then write the session to the archive `path`.

        `scripts`: (name, sha256) of the scripts applied since the session began or was resumed;
        they join `session.scripts`. The session stays usable. Refused when `path` is inside the
        input tree, is the file list or the source of an added file. A save in the middle of a
        script ends an offset-map stage there, as a load does. See `tests/cli/test_save_resume.py`.
        """
        self._idle("save")
        args.path("save", path)
        scripts = args.scripts("save", scripts)
        dest = Path(path)
        root = self.session.layout_root
        target = dest.resolve()
        if target == root or root in target.parents:
            raise VetError(f"save: {dest} is inside the input tree {root}; the archive must not be written there")
        for owned, what in self.session.owned_paths().items():
            if target == owned:
                raise VetError(f"save: {dest} is {what}; the archive must not replace it")
        try:
            self.session.refresh()
        except AdapterError as e:
            raise VetError(f"save: the edits queued cannot be loaded: {e}") from e
        self._sync()
        applied = [(str(n), str(h)) for n, h in scripts]
        extra = {
            "log": [[r.op, r.target, r.status] for r in self.log],
            "notes": list(self.notes),
            "checks": [{"key": list(c.key), "kind": c.kind, "args": list(c.args)} for c in self._checks],
            "swapped": [[mod, name, sorted(mods)] for (mod, name), mods in sorted(self._swapped.items())],
        }
        try:
            index, files = self.session.to_state([*self.session.scripts, *applied], extra)
            write_archive(dest, index, files)
        except (SessionError, ArchiveError) as e:
            raise VetError(f"save: {e}") from e
        self.session.scripts.extend(applied)

    @classmethod
    def restore(cls, path: Path | str) -> Vet:
        """A Vet on a new session in the state saved at `path`; the input tree is not needed.

        SessionError (naming `path`) when it is not a session archive, is damaged in any way, has
        another format version or too much data, or names a path outside its layout root.
        """
        args.path("restore", path)
        try:
            index, files = read_archive(Path(path))
        except ArchiveError as e:
            raise SessionError(str(e)) from e
        try:
            log, notes, checks, swapped = _read_vet_state(index)
            vet = cls(Session.from_state(index, files))
        except SessionError as e:
            raise SessionError(f"{path}: {e}") from e
        except AdapterError as e:
            raise SessionError(f"{path}: the saved design cannot be loaded: {e}") from e
        except _DAMAGE as e:
            raise SessionError(f"{path}: the session archive is damaged ({type(e).__name__}: {e})") from e
        vet.log, vet.notes, vet._checks, vet._swapped = log, notes, checks, swapped
        return vet

    # --- selectors -----------------------------------------------------------------

    def module(self, name: str, instances: int | None = None) -> Module:
        args.name("module", "name", name)
        args.count(f"module {name}", "instances", instances, none=True)
        self._sync()
        if self._all:
            self.session.refresh()
            self._sync()
        known = self.session.loaded.modules()
        if name not in known:
            raise VetError(f"no module {name!r} in the design{_did_you_mean(name, known)}")
        paths = self.module_paths(name)
        if not paths:
            try:
                where = self.session.loaded.module(name).path
            except AdapterError:
                where = None
            if where is not None and self.session.is_added(where):
                raise VetError(f"module {name!r} is defined in the added file {where.name}, which is never edited")
            raise VetError(f"module {name!r} is not elaborated under the top, so it cannot be edited")
        if instances is not None and instances != len(paths):
            raise VetError(f"module {name!r} is elaborated {len(paths)} time(s) ({', '.join(paths)}), not {instances}")
        return Module(self, name, instances)

    # --- bookkeeping ---------------------------------------------------------------

    def _sync(self) -> None:
        """Forget what was edited when the design has been re-loaded since."""
        if self.session.reloads != self._seen:
            self._seen = self.session.reloads
            self._dirty.clear()
            self._all = False
            self._added.clear()

    def _mark(self, *key: str) -> None:
        self._sync()
        self._dirty.add(key)

    def _refresh_if(self, keys: Iterable[tuple[str, ...]]) -> None:
        self._sync()
        if self._all or any(k in self._dirty for k in keys):
            self.session.refresh()
            self._sync()

    def _info(self, module: str, keys: Iterable[tuple[str, ...]] = ()) -> ModuleInfo:
        self._refresh_if(keys)
        try:
            return self.session.loaded.module(module)
        except AdapterError as e:
            raise VetError(str(e)) from e

    def _snap(self) -> dict[str, Any]:
        if self._snap_cache is None:
            self._snap_cache = self.session.baseline_snapshot()
        return self._snap_cache

    def module_paths(self, module: str) -> list[str]:
        args.name("module_paths", "module", module)
        if self._paths is None:
            snap = self._snap()
            paths: dict[str, list[str]] = {}
            for p in sorted(snap):
                paths.setdefault(snap[p].module, []).append(p)
            self._paths = paths
        return list(self._paths.get(module, []))

    def _set_check(self, check: _Check) -> None:
        """Register a post-condition; a later one on the same target replaces the earlier."""
        self._checks = [c for c in self._checks if c.key != check.key]
        self._checks.append(check)

    def _evaluators(self) -> list[Callable[[Design], str | None]]:
        """The registered post-conditions as `Session.finish` takes them."""
        session = self.session
        return [partial(_evaluate, session, c) for c in self._checks]

    def _proof(self) -> Callable[[Session, Design], Proof] | None:
        """The equivalence check of the logic operations registered so far, as `Session.finish` takes it."""
        ops = [
            LogicOp(
                c.args[0] or "",
                c.args[1] or "",
                c.args[2] or "",
                *((c.args[5],) if c.kind == "override_net" else (c.args[3], c.kind)),
            )
            for c in self._checks
            if c.kind in ("override_net", "insert_cell_on_net")
        ]
        ops += [
            LogicOp(m.target, "", "", m.waiver, m.kind, m.label)
            for m in self.session._marks
            if m.kind in ("splice", "logic")
        ]
        outside = [Outside(m.target, m.waiver or "") for m in self.session._marks if m.kind == "outside"]
        if not ops and not outside:
            return None

        def proof(session: Session, design: Design) -> Proof:
            return prove(session, design, ops, outside)

        return proof

    def _instance_info(self, module: str, name: str) -> InstanceInfo:
        info = self._info(module)
        found = [i for i in info.instances if i.name == name]
        if not found:
            raise VetError(
                f"module {module!r} has no instance {name!r}{_did_you_mean(name, [i.name for i in info.instances])}"
            )
        if len(found) > 1:
            raise VetError(f"module {module!r} has {len(found)} instances named {name!r}")
        return found[0]

    def _queue(self, path: Path, splices: list[tuple[int, int, bytes]]) -> None:
        data = self.session.text(path)
        for start, end, text in splices:
            self.session._queue_edit(path, start, end, text, old=data[start:end])

    def _commit_all(self, op: str, plans: list[_Plan]) -> None:
        """Commit validated plans; nothing is queued unless every file to edit is editable."""
        bad = sorted(
            {str(p.path) for p in plans if not p.skip and p.path is not None and not self.session.is_editable(p.path)}
        )
        if bad:
            raise VetError(f"{op}: cannot edit {', '.join(bad)}: not editable in this session")
        for plan in plans:
            self._commit(plan)

    def _refuse_generate_copies(self, op: str, mods: list[str]) -> None:
        """Refuse an instance of one of `mods` written in a generate block or array, in any module."""
        wanted = set(mods)
        for host, flagged in self._generate_copies().items():
            for name, module, in_generate in flagged:
                if module in wanted:
                    what = "inside a generate block" if in_generate else "an instance array"
                    raise VetError(
                        f"{op}: instance {name!r} of module {module!r} in module {host!r} is "
                        f"{what} (generate blocks and instance arrays are not supported): a new port could "
                        f"stay unconnected under another parameterisation"
                    )

    def _generate_copies(self) -> dict[str, list[tuple[str, str, bool]]]:
        """Per module: its instances written in a generate block or instance array.

        Each entry is (instance name, instantiated module, in a generate block).
        Building the source model of every module costs about 0.2 s on the demo_ss
        sample, and a re-load used to throw the result away. The answer for a
        module depends on the text of the file that defines it and on the headers:
        a macro or a `define in a header can add an instance to a module whose own
        file is unchanged. So an entry is kept while its file's text is unchanged,
        and everything is dropped when any header's text changes (headers are few
        and rarely edited). A module whose definition moved or changed has a file
        with different text and is looked at again.
        """
        design = self.session.loaded
        sources = set(self.session.filelist.files)
        headers = tuple(self.session.text(p) for p in self.session.files if p not in sources)
        known = self._gen_headers
        if (
            known is None
            or len(known) != len(headers)
            or any(a is not b and a != b for a, b in zip(known, headers, strict=True))
        ):
            self._gen_cache.clear()
            self._gen_headers = headers
        out: dict[str, list[tuple[str, str, bool]]] = {}
        for host in design.modules():
            cached = self._gen_cache.get(host)
            if cached is not None:
                path, text, flagged = cached
                try:
                    current = self.session.text(path)
                except SessionError:
                    current = None
                if current is text or current == text:
                    out[host] = flagged
                    continue
            try:
                info = design.module(host)
            except AdapterError:
                self._gen_cache.pop(host, None)
                continue
            flagged = [
                (inst.name, inst.module, inst.in_generate)
                for inst in info.instances
                if inst.in_generate or inst.is_array
            ]
            try:
                text = self.session.text(info.path)
            except SessionError:
                out[host] = flagged  # a file the session does not track: do not cache
                continue
            self._gen_cache[host] = (info.path, text, flagged)
            out[host] = flagged
        return out

    def _record(self, op: str, target: str, status: str) -> None:
        self.log.append(OpRecord(op, target, status))

    # --- checked operations, groups ---------------------------------------------------

    def _begin(self, label: str) -> _VetState:
        """Apply and load what is queued, then take a checkpoint.

        So a checkpoint never has a queue: rolling back to it also means "nothing of this
        Vet is waiting for a re-load", which is what the dirty marks say after a re-load.
        """
        try:
            self.session.refresh()
        except AdapterError as e:
            raise VetError(f"{label}: the edits queued earlier cannot be loaded: {e}") from e
        self._sync()
        return _VetState(
            self.session.checkpoint(),
            len(self.log),
            len(self.notes),
            list(self._checks),
            {k: set(v) for k, v in self._swapped.items()},
            len(self._drove),
        )

    def _rollback(self, st: _VetState) -> None:
        self.session.rollback(st.session)
        del self.log[st.log :]
        del self.notes[st.notes :]
        self._checks = list(st.checks)
        self._swapped = {k: set(v) for k, v in st.swapped.items()}
        del self._drove[st.drove :]
        self._dirty.clear()
        self._all = False
        self._added.clear()
        self._seen = self.session.reloads

    def _conclude(self, st: _VetState, prefix: str) -> None:
        """Compare the edited design with the one at `st`: refuse (taking everything back) or note."""
        try:
            self.session.refresh()
            self._sync()
            found = self.session.compare_since(st.session, self._drove[st.drove :])
        except AdapterError as e:
            self._rollback(st)
            raise VetError(f"{prefix}: the edited design cannot be loaded: {e}") from e
        if found is None:
            return
        problems = [f"new {d.severity} {d.code}: {describe(d)}" for d in found.diagnostics]
        problems += [f"left dangling: {describe(d)}" for d in found.dangling]
        problems += [f"left partly undriven: {msg}" for msg in found.partial]
        if problems:
            self._rollback(st)
            more = f" (and {len(problems) - 3} more)" if len(problems) > 3 else ""
            raise VetError(f"{prefix}: " + "; ".join(problems[:3]) + more)
        self.notes.extend(f"{prefix}: {describe(d)}" for d in found.unused)

    def _checked(self, op: str, target: str, run: Callable[[], None], tried: bool = False) -> None:
        """Run a checked operation: alone it is a group of its own, in a group it is part of it.

        `tried`: the operation makes checks of its own on the edited design at the call,
        so inside a group it still loads what is queued first and is taken back by itself.
        """
        prefix = f"{op} {target}"
        if self._depth and not tried:
            run()
            return
        st = self._begin(prefix)
        outer = self._depth
        self._depth += 1
        try:
            run()
        except BaseException:
            self._depth = outer
            self._rollback(st)
            raise
        self._depth = outer
        if not outer:
            self._conclude(st, prefix)

    @contextmanager
    def group(self) -> Iterator[None]:
        """`with vet.group():` the comparison of the checked operations is made once, at the end."""
        if self._depth:
            # Part of the outer group (compared at its end), but taken back on its own when it fails.
            st = self._begin("group")
            self._depth += 1
            try:
                yield
            except BaseException:
                self._depth -= 1
                self._rollback(st)
                raise
            self._depth -= 1
            return
        st = self._begin("group")
        self._depth = 1
        try:
            yield
        except BaseException:
            self._depth = 0
            self._rollback(st)
            raise
        self._depth = 0
        self._conclude(st, "group")
