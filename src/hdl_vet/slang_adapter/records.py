"""The data classes of the adapter: what a load reports about a design."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


class AdapterError(Exception):
    """Raised when a design cannot be loaded or a query cannot be answered."""


@dataclass(frozen=True)
class Diag:
    severity: str
    code: str
    message: str
    path: Path | None
    line: int | None
    offset: int | None
    in_macro: bool = False
    module: str | None = None  # the module the diagnostic is located in (set for dangling / unused entries)
    # the name of the signal the entry is about, slang's first argument (set for dangling / unused entries);
    # not part of equality
    symbol: str | None = field(default=None, compare=False, repr=False)
    # UndrivenPort entries: the port's external name and whether its module reads it; None / False when
    # the symbol could not be resolved (not part of equality)
    port: str | None = field(default=None, compare=False, repr=False)
    read: bool = field(default=False, compare=False, repr=False)


@dataclass(frozen=True)
class PortInfo:
    name: str
    direction: str
    start: int
    end: int
    type_text: str = ""  # ANSI: the data type as `logic ...` with packed dimensions ("" if unknown)
    unpacked: bool = False  # ANSI: the port has unpacked dimensions


@dataclass(frozen=True)
class ParamInfo:
    name: str
    default: str | None
    start: int
    end: int
    type_text: str = ""  # the declared data type as written ("" when implicit)
    decl_start: int = -1  # the whole declaration (`parameter int X = 1`), which may hold several names
    decl_end: int = -1
    decl_count: int = 1  # names the declaration declares


@dataclass(frozen=True)
class ConnInfo:
    port: str | None
    expr: str
    start: int
    end: int
    open: int | None = None  # offset of the "(" of a `.name(...)` connection


@dataclass(frozen=True)
class ParamOverride:
    name: str | None
    value: str
    start: int
    end: int
    open: int | None = None  # offset of the "(" of a `.name(...)` override


@dataclass(frozen=True)
class DeclInfo:
    """A module-level net or variable declaration."""

    name: str
    kind: str  # "net" | "var"
    type_text: str  # data type (with the net type for a net) and unpacked dimensions
    start: int
    end: int
    stmt_start: int = -1  # the whole declaration statement; -1 when it cannot be edited as text
    stmt_end: int = -1
    stmt_count: int = 1  # names the statement declares


@dataclass(frozen=True)
class AssignInfo:
    """One continuous assignment `left = right`."""

    left: str
    right: str
    start: int
    end: int
    stmt_start: int = -1  # the whole `assign ...;` statement; -1 when it cannot be edited as text
    stmt_end: int = -1
    stmt_count: int = 1  # assignments the statement holds


@dataclass(frozen=True)
class InstanceInfo:
    name: str
    module: str
    path: Path
    from_macro: bool
    stmt_start: int
    stmt_end: int
    module_start: int
    module_end: int
    start: int
    end: int
    params_close: int | None
    ports_close: int
    connections: list[ConnInfo]
    param_overrides: list[ParamOverride]
    ports_open: int = -1
    params_open: int | None = None
    in_generate: bool = False  # inside a generate block, loop or conditional
    is_array: bool = False  # an instance array `u [3:0] (...)`
    conn_directive: str | None = None  # a conditional directive inside the connection list
    override_directive: str | None = None  # ... inside the parameter value list

    def connection(self, port: str) -> ConnInfo:
        for c in self.connections:
            if c.port == port:
                return c
        raise AdapterError(f"instance {self.name!r} has no connection for port {port!r}")

    def param_override(self, name: str) -> ParamOverride:
        for p in self.param_overrides:
            if p.name == name:
                return p
        raise AdapterError(f"instance {self.name!r} has no override of parameter {name!r}")


@dataclass(frozen=True)
class ModuleInfo:
    name: str
    path: Path
    start: int
    end: int
    ports_close: int | None
    body_end: int
    ports: list[PortInfo]
    params: list[ParamInfo]
    instances: list[InstanceInfo]
    has_macros: bool
    name_end: int = 0
    ports_open: int | None = None
    params_open: int | None = None
    params_close: int | None = None
    port_list_kind: str = "none"  # "ansi" | "nonansi" | "none"
    header_end: int = 0
    body_indent: str = "  "
    decls: list[DeclInfo] = field(default_factory=list)
    assigns: list[AssignInfo] = field(default_factory=list)
    header_has_macros: bool = False
    param_kinds: dict[str, str] = field(default_factory=dict)  # "value" | "local" | "type"
    other_names: frozenset[str] = frozenset()  # typedefs, subroutines, genvars, named blocks
    imports_end: int | None = None  # end of the last package import in the header
    ports_directive: str | None = None  # a conditional directive inside the port list
    params_directive: str | None = None  # ... inside the parameter port list
    disabled_names: dict[str, str] = field(default_factory=dict)  # identifier in directive-disabled text -> directive
    generate_decls: frozenset[str] = frozenset()  # nets and variables declared inside generate constructs
    generate_assigned: frozenset[str] = frozenset()  # base names assigned inside generate constructs

    def port(self, name: str) -> PortInfo:
        for p in self.ports:
            if p.name == name:
                return p
        raise AdapterError(f"module {self.name!r} has no port {name!r}")

    def param(self, name: str) -> ParamInfo:
        for p in self.params:
            if p.name == name:
                return p
        raise AdapterError(f"module {self.name!r} has no parameter {name!r}")

    def instance(self, name: str) -> InstanceInfo:
        found = [i for i in self.instances if i.name == name]
        if not found:
            raise AdapterError(f"module {self.name!r} has no instance {name!r}")
        if len(found) > 1:
            raise AdapterError(f"module {self.name!r} has {len(found)} instances named {name!r}")
        return found[0]


@dataclass(frozen=True)
class InstanceSnap:
    module: str
    params: dict[str, str]
    ports: dict[str, tuple[str, int]]
    connections: dict[str, str]
    port_types: dict[str, str] = field(default_factory=dict)
    port_resolved: dict[str, str] = field(default_factory=dict)  # the type behind any alias


@dataclass(frozen=True)
class Driver:
    is_input_port: bool
    instance: str | None = None  # the instance whose output port connection drives it, else None


@dataclass(frozen=True)
class DriverDetail:
    """One driver of a signal: what drives it and which bits (inclusive range)."""

    kind: str  # "assign" | "process" | "instance" | "input" | "initializer" | "other"
    low: int
    high: int


@dataclass(frozen=True)
class HierRef:
    """A hierarchical name written in an instance: where it is written and what slang resolved it to."""

    host: str  # path of the instance whose module (or one of its port connections) holds the name
    module: str  # that module
    target: str  # the resolved path of the signal, task or function


@dataclass(frozen=True)
class InstanceSite:
    instance_name: str
    module_name: str
    path: Path
    start: int
    end: int
    from_macro: bool


@dataclass(frozen=True)
class PinConn:
    """How one elaborated instance connects one pin.

    `parent` is the module that holds the instance, `instance` the path of the instance inside it (with the
    names of generate blocks, without indexes). `kind` is "zero" (a literal with the value 0), "port" (the
    name of an input port of the parent: `port`) or "other". `text` is the connection as written.
    """

    parent: str
    instance: str
    text: str
    kind: str
    port: str = ""
