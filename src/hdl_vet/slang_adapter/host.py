"""Typing only: what the mixins of `Design` use from the class they are mixed into."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path
    from typing import Any

    from hdl_vet.slang_adapter.records import Diag, InstanceSite, InstanceSnap, ModuleInfo


class _DesignHost:
    """Typing only: what the mixins of `Design` use from the class they are mixed into."""

    if TYPE_CHECKING:
        _analysis: Any
        _comp: Any
        _diags: list[Diag] | None
        _engine: Any
        _gate_snap: dict[str, InstanceSnap] | None
        _instance_cache: tuple[Any, ...] | None
        _modules: dict[str, ModuleInfo]
        _sm: Any
        _snap: dict[str, InstanceSnap] | None
        _spans: list[tuple[Path, int, int, str]] | None
        _trees: dict[Path, Any]
        _udps: frozenset[str] | None
        _unused: list[Diag] | None

        def _analyze(self) -> Any: ...

        def _buffer_path(self, buffer: Any) -> Path | None: ...

        def _instances(self, exhaustive: bool = False) -> tuple[Any, ...]: ...

        def _original(self, loc: Any) -> Any: ...

        def _plain(self, node: Any) -> bool: ...

        def _sites_of(self, inst: Any, plain: bool | None = None) -> list[InstanceSite]: ...

        def _udp_names(self) -> frozenset[str]: ...

        def _visit_instances(self, fn: Any) -> None: ...
