"""The only package that talks to pyslang (every module of it may import pyslang).

Loads a `.f` file list into a slang compilation and exposes the few capabilities
hdl-vet needs: byte-exact round trip, diagnostics (parse, elaboration and
analysis), elaborated instance counts, instance statements as source byte ranges,
drivers of a hierarchical name, and syntax equivalence.
"""

from __future__ import annotations

from hdl_vet.slang_adapter.api import load, parameter_names
from hdl_vet.slang_adapter.design import Design
from hdl_vet.slang_adapter.diagnostics import driven_bases
from hdl_vet.slang_adapter.loading import LoadCache
from hdl_vet.slang_adapter.loading import _Loader as _Loader
from hdl_vet.slang_adapter.records import (
    AdapterError,
    AssignInfo,
    ConnInfo,
    DeclInfo,
    Diag,
    Driver,
    InstanceInfo,
    InstanceSite,
    InstanceSnap,
    ModuleInfo,
    ParamInfo,
    ParamOverride,
    PortInfo,
)
from hdl_vet.slang_adapter.text import is_simple_identifier, syntax_equivalent, tokens

__all__ = [
    "AdapterError",
    "AssignInfo",
    "ConnInfo",
    "DeclInfo",
    "Design",
    "Diag",
    "Driver",
    "InstanceInfo",
    "InstanceSite",
    "InstanceSnap",
    "LoadCache",
    "ModuleInfo",
    "ParamInfo",
    "ParamOverride",
    "PortInfo",
    "driven_bases",
    "is_simple_identifier",
    "load",
    "parameter_names",
    "syntax_equivalent",
    "tokens",
]
