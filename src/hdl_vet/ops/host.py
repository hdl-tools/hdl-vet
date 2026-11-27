"""Typing only: what the mixins of `Vet` use from the class they are mixed into."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable
    from pathlib import Path
    from typing import Any

    from hdl_vet.ops.common import _Check, _Plan, _VetState
    from hdl_vet.ops.module import Module
    from hdl_vet.ops.scope import _Scope, _Walk
    from hdl_vet.session import Session
    from hdl_vet.slang_adapter import InstanceInfo, ModuleInfo


class _VetHost:
    """Typing only: what the mixins of `Vet` use from the class they are mixed into."""

    if TYPE_CHECKING:
        _added: dict[str, dict[str, tuple[str, str]]]
        _all: bool
        _checks: list[_Check]
        _depth: int
        _dirty: set[tuple[str, ...]]
        _drove: list[tuple[str, str]]
        _scope: _Scope | None
        _script_dir: Path | None
        _swapped: dict[tuple[str, str], set[str]]
        session: Session

        def _begin(self, label: str) -> _VetState: ...

        def _checked(self, op: str, target: str, run: Callable[[], None], tried: bool = False) -> None: ...

        def _commit(self, plan: _Plan) -> None: ...

        def _commit_all(self, op: str, plans: list[_Plan], who: str | None = None) -> None: ...

        def _idle(self, op: str) -> None: ...

        def _info(self, module: str, keys: Iterable[tuple[str, ...]] = ()) -> ModuleInfo: ...

        def _instance_info(self, module: str, name: str) -> InstanceInfo: ...

        def _instances_of(self, mod: str) -> list[tuple[str, InstanceInfo]]: ...

        def _mark(self, *key: str) -> None: ...

        def _need_editable(self, who: str, path: Path | None) -> None: ...

        def _plan_add_param(
            self, mod: str, name: str, type: str, default: object, info: ModuleInfo, paths: list[str]
        ) -> _Plan: ...

        def _plan_add_port(
            self, mod: str, name: str, decl: str, info: ModuleInfo, paths: list[str], width: str | None = None
        ) -> _Plan: ...

        def _plan_assign(
            self, mod: str, info: ModuleInfo, target: str, expr: object, planned_port: bool = False
        ) -> _Plan: ...

        def _queue(self, path: Path, splices: list[tuple[int, int, bytes]]) -> None: ...

        def _record(self, op: str, target: str, status: str, key: tuple[str, ...] | None = None) -> None: ...

        def _refresh_if(self, keys: Iterable[tuple[str, ...]]) -> None: ...

        def _refuse_generate_copies(self, op: str, mods: list[str]) -> None: ...

        def _refuse_splice(self, scope: _Scope, path: Path, start: int, end: int, text: bytes) -> None: ...

        def _rollback(self, st: _VetState) -> None: ...

        def _scoped_walk(self, op: str, to: str) -> tuple[str, _Walk | None]: ...

        def _set_check(self, check: _Check) -> None: ...

        def _snap(self) -> dict[str, Any]: ...

        def _sync(self) -> None: ...

        def _walk_path(self, op: str, to: str) -> tuple[list[str], list[str], list[str]]: ...

        def module(self, name: str, instances: int | None = None) -> Module: ...

        def module_paths(self, module: str) -> list[str]: ...
