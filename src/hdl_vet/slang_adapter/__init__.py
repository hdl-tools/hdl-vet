"""The only package that talks to pyslang (every module of it may import pyslang).

Loads a `.f` file list into a slang compilation and exposes the few capabilities
hdl-vet needs: byte-exact round trip, diagnostics (parse, elaboration and
analysis), elaborated instance counts, instance statements as source byte ranges,
drivers of a hierarchical name, and syntax equivalence.
"""

from __future__ import annotations

from hdl_vet.slang_adapter.api import (
    check_expression,
    instance_parameters,
    is_atom,
    is_wiring,
    load,
    parameter_names,
    value_head_identifiers,
    value_identifiers,
)
from hdl_vet.slang_adapter.design import Design
from hdl_vet.slang_adapter.diagnostics import driven_bases
from hdl_vet.slang_adapter.loading import LoadCache
from hdl_vet.slang_adapter.loading import _Loader as _Loader
from hdl_vet.slang_adapter.records import (
    AdapterError,
    AssignInfo,
    BindRef,
    ConnInfo,
    DeclInfo,
    Diag,
    Driver,
    DriverDetail,
    InstanceInfo,
    InstanceSite,
    InstanceSnap,
    ModuleInfo,
    ParamInfo,
    ParamOverride,
    PinConn,
    PortInfo,
)
from hdl_vet.slang_adapter.text import (
    confined,
    equivalent_bytes,
    is_simple_identifier,
    meets_directive,
    splice_stays,
    syntax_equivalent,
    tokens,
    touches_define,
    unconfined_reason,
)

__all__ = [
    "AdapterError",
    "AssignInfo",
    "BindRef",
    "ConnInfo",
    "DeclInfo",
    "Design",
    "Diag",
    "Driver",
    "DriverDetail",
    "InstanceInfo",
    "InstanceSite",
    "InstanceSnap",
    "LoadCache",
    "ModuleInfo",
    "ParamInfo",
    "ParamOverride",
    "PinConn",
    "PortInfo",
    "check_expression",
    "confined",
    "driven_bases",
    "equivalent_bytes",
    "instance_parameters",
    "is_atom",
    "is_simple_identifier",
    "is_wiring",
    "load",
    "meets_directive",
    "parameter_names",
    "splice_stays",
    "syntax_equivalent",
    "tokens",
    "touches_define",
    "unconfined_reason",
    "value_head_identifiers",
    "value_identifiers",
]
