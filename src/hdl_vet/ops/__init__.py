"""The script API: `Vet` and the structural operations scripts are made of.

Every operation follows one contract: if the design already shows exactly the
requested result nothing is queued and the operation is logged "skipped"; if it is
in the expected "before" state the edit is queued (guarded with `old=`), its
structural expectations are registered and a post-condition is registered that
`finish` evaluates on the final design; otherwise `DriftError`. See
`tests/ops/test_ops.py` for the full specification.

Phase 5 adds the *checked* operations (`add_file`, `replace_module`, `remove_*`):
each is tried on the design before it returns and taken back, with everything it did,
when the edited design has a new diagnostic or leaves something dangling; `Vet.group`
makes the same check once for a block of operations. See `tests/ops/test_instances.py`.

Handles are live: each operation and each model attribute looks the design up
again. A re-load happens only when the lookup depends on something this `Vet` has
edited since the last load (a module's port list, parameter list or declarations;
an instance's connection or override list), on anything queued for `add_assign`,
or on anything at all after a raw `splice`.

This module does not import pyslang; it talks to `hdl_vet.session` and the
data types of `hdl_vet.slang_adapter`.
"""

from __future__ import annotations

from hdl_vet.ops.common import DriftError, OpRecord, VetError
from hdl_vet.ops.instance import Instance
from hdl_vet.ops.module import Module
from hdl_vet.ops.vet import Vet

__all__ = ["DriftError", "Instance", "Module", "OpRecord", "Vet", "VetError"]
