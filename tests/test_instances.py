"""Phase 5: instance changes: added files, module replacement, removals.

Owner decisions (2026-11-15):
    - A script brings in a module that is not in the file list with
      `vet.add_file`. The file is loaded with the design; a swap is checked
      against its real ports. No black-box swaps.
    - An added file is copied into the output tree under `hdl_vet_added/` and
      the output file list points at the copy. hdl-vet never edits added files.
    - Replacing an instance's module: same-named ports and parameters carry
      over; every other one must be named by the script. Anything unaccounted
      for, and any port whose width or direction differs, is refused, and
      nothing stays queued.
    - A removal that would leave something dangling is refused, naming what is
      left. Nothing is removed on the script's behalf (no cascade).

Everything in phase 3 and 4 stays as it is: the skip / apply / drift contract,
live handles, the refusals for macros, generate blocks, instance arrays,
positional and `.*` lists, conditional directives inside a list, non-ANSI
headers, and the `instances=` confirmation for a module elaborated more than
once. Names that are unknown raise VetError with the usual suggestions.


Checked operations
------------------
The operations of this phase (`add_file`, `replace_module`, `remove_*`) are
*checked*: the edit is tried on the design before the call returns.

    1. Anything queued earlier is applied and loaded first.
    2. The operation's edit is applied and the design is loaded again.
    3. The operation is refused with VetError when, compared with the design
       of step 1:
         - the edited design cannot be loaded, or
         - there is a new diagnostic as sign-off counts them (any new error
           or warning; warnings located inside an added file excepted, see
           add_file), or
         - something is left *dangling*: `Design.dangling()` has an entry that
           it did not have before.
       The message starts with the operation and its target and quotes up to
       three of the offending diagnostics (slang's message; the file's name).
    4. On refusal the Vet and its session are exactly as before the call:
       text, queue, expectations, post-conditions, added files, log, notes.
       A following operation, or `finish`, behaves as if the refused call had
       never been made.
    5. On success, anything left *unused* that was not unused before is added
       to `vet.notes` (list[str]); each note starts with the operation and its
       target and contains slang's message. Notes never fail anything. The
       command line prints each as "note: ...".

    with vet.group():
        ...
    Inside a group, step 3's comparison (new diagnostics, dangling) and step 5
    are made once, when the block ends, against the design as it was when the
    block began; the checks that belong to one operation alone (below) are
    still made at the call. Phase 3 and 4 operations inside the block are part
    of the group. If the comparison fails, or any exception leaves the block,
    everything done inside the block is taken back as in step 4 and the
    exception (VetError for a failed comparison, starting "group:") is raised.
    A group inside a group is part of the outer one. A checked operation
    outside a group is a group of its own; phase 3 and 4 operations outside a
    group are not checked. A group begins by applying and loading anything
    queued (no re-load when nothing is). Notes from a `with` group start with
    "group:".

    Why: removing a driver and its last reader are each refused alone; together
    they are a clean removal.

Adapter:
    Design.dangling() -> list[Diag]    slang codes UndrivenNet, UndrivenPort,
                                       UnassignedVariable
    Design.unused()   -> list[Diag]    UnusedNet, UnusedVariable, UnusedPort,
                                       UnusedButSetNet, UnusedButSetVariable,
                                       UnusedButSetPort, UnusedImplicitNet
    Both come from an analysis with slang's unused checking switched on. That
    analysis is separate: `Design.diagnostics()` is unchanged by it, so nothing
    about the baseline or about sign-off changes. "Not there before" compares
    multisets of (code, file, message); the message names the signal, so an
    entry that only moved is not new.

Session:
    Session.checkpoint() -> object     opaque
    Session.rollback(cp)               text, queue, stages, expectations, added
                                       files and the loaded design are as at
                                       the checkpoint. `reloads` keeps counting.
    Session.add_file(path)             see add_file; SessionError on refusal
    Session.added -> dict[str, bytes]  output-relative POSIX path -> bytes, in
                                       the order added
    Result.added                       the same mapping at sign-off


vet.add_file(path)
------------------
    `path` is a source file outside the design. A relative path is taken from
    the current directory (scripts use `Path(__file__).parent / ...`).
    The file is read once and joins the design as the last source of the file
    list. It is not editable: `splice` refuses it, and no operation edits a
    module defined in it.
    Output name: "hdl_vet_added/" + the file's name.
    Skipped when that file is already part of the design (a source of the
    file list, or added earlier). VetError when:
        - it cannot be read;
        - another added file has the same name, or the input tree already has
          an `hdl_vet_added` directory at its layout root;
        - it includes a header that is not already a file of the design
          (checked by hdl-vet: slang would load it without complaint);
        - the design with the file does not pass the check above (a module it
          defines already exists; a syntax error).
    Warnings located inside an added file are never "new diagnostics", neither
    for a checked operation nor at sign-off: the file is the user's input, as
    the baseline is. Errors always count.
    Added files take no part in the layout root, in `Session.files` or in the
    mirrored part of the output tree. `Result.added` is filled also when no
    file of the design changed.
    Post-condition at `finish`: some instance that is not a top of the final
    design has a module defined in the file; otherwise the failed check
    "add_file <name>: none of its modules is instantiated".
    Log: OpRecord("add_file", <file name>, status).

    write_tree: each added file is written at its output name under the output
    directory, byte for byte; the file list gets one line per added file after
    the design's own files, relative to the file list like every other line;
    the manifest gains "added": {<output name>: {"sha256": ...}} (an empty
    object when nothing was added). The command line prints, on stdout,
    "added <output name>" after its "changed" lines, then the notes.


Instance.replace_module(module, *, ports=None, params=None, connect=None,
                        set_params=None, drop_ports=(), drop_params=(),
                        unconnected=(), was=None)
--------------------------------------------------------------------------
    `module` is any module of the design (it need not be instantiated yet).
    Old side: every named connection and every parameter override the instance
    has now must be accounted for:
        ports={"old": "new"}      the connection is renamed
        drop_ports=["old"]        the connection is removed
        (neither)                 kept, when `module` has a port of that name
        params / drop_params      the same for overrides
    New side: every port of `module` must be accounted for:
        the target of a kept or renamed connection, or
        connect={"new": expr}     a connection that is added, or
        unconnected=["new"]       outputs and inouts only; written as an empty
                                  connection `.new()` (an absent one makes
                                  slang warn)
    set_params={"name": value} adds an override (or replaces a kept one).
    Parameters of `module` that are not overridden keep their defaults.

    Checked on the model, before any edit; all problems are reported in one
    VetError (nothing is queued):
        - a connection or override that is not accounted for, or a port of
          `module` that is not accounted for (each named);
        - a name in `ports`, `drop_ports`, `params`, `drop_params` that the
          instance does not have; a target, a `connect` or `unconnected` name
          that `module` does not have; an input port in `unconnected`;
        - two things landing on one port or one parameter;
        - `set_params` naming something that is not an overridable value
          parameter of `module`;
        - `module` has a non-ANSI port list, or either module has an interface
          port.
    Checked on the tried design (step 2 above), at the call even inside a
    group, for every path of the instance; refusal takes the edit back:
        - a kept or renamed port must have the same direction and the same
          width on both modules. The message names both ports (in quotes)
          and both widths (or directions).
        - a parameter that both modules have under the same (or a renamed)
          name, that the instance does not override and `set_params` does not
          set, must resolve to the same value as before. The message names it.
    These checks are made before the comparison of step 3; when one fails, the
    refusal's first line names it (slang's own diagnostics, such as
    PortWidthExpand, do not name the port).
    The handle stays usable after a swap: `.paths` and the operations follow
    the instance as it is in the edited design.

    Text. The result is that of these steps, each by the rule given, in this
    order; existing bytes are never reformatted:
        1. the module name is replaced;
        2. renamed connections and overrides: only the name is replaced; the
           blanks before "(" stay;
        3. dropped connections and overrides are removed (list rule below);
           an override list that becomes empty is removed whole, from the end
           of the module name through its ")";
        4. `set_params`, then `connect`, then `unconnected`, each in the
           order given, by the placement rules of `set_param` and `connect`.
    When the instance already instantiates `module`, only this comparison is
    made (the maps describe the old module and are not looked at): skipped
    when every entry of `connect` and `set_params` is there with the same
    text, DriftError naming the entry that differs or is missing otherwise.
    DriftError also when `was` is given and the instance instantiates neither
    `was` nor `module`; the message names the module it does instantiate.
    Requires: module-changed on every path (exact). Permits: any change at
    those paths and below them.
    Post-condition: the instance instantiates `module` in the final design.
    Log: OpRecord("replace_module", "<parent>.<instance>", status).


Removals
--------
All are checked operations. "Line rule": when the removed text leaves only
blanks on the line(s) it occupied, those lines go too, with one line break.

    Module.remove_instance(name, module=None)
        Removes the instantiation statement (line rule). Skipped when the
        module has no instance of that name. DriftError when `module` is given
        and the instance instantiates something else. VetError when the
        statement declares several instances.
        Requires: instance-removed on every path (exact). Permits: anything
        below those paths.

    Module.remove_wire(name)
        Removes the declaration of a module-level net or variable (line rule).
        Skipped when nothing of that name is declared. VetError when the name
        is a port or anything other than a net or variable, or when the
        declaration declares several names.

    Module.remove_assign(target, was=None)
        Removes the continuous assignment to `target` (line rule). Skipped
        when there is none. DriftError when `was` is given and the right-hand
        side differs (whitespace-insensitive). VetError when several
        assignments have that target, or the statement holds several.

    Module.remove_port(name)
        Removes the port from an ANSI port list, and the connection to it at
        every instance of the module (they cannot exist without each other).
        Skipped when the module has no such port. VetError when the port that
        follows it takes its direction or type from it (its own text starts
        with its name, as `y1` in `output logic [3:0] y0, y1`), and for every
        instance whose connection list cannot be edited.
        Requires: port-removed on every path. Permits: connection-changed for
        that port on every path.

    Module.remove_param(name)
        The same for a parameter of the parameter port list and the override
        of it at every instance. VetError for a parameter declared in the body.
        Requires: param-changed on every path.

    List rule, for an item of a port, parameter, connection or override list:
        - not the last item: the text from the item's start to the start of
          the next item is removed;
        - the last of several: the comma that follows the previous item is
          removed, and the item is removed with the blanks before it (line
          rule);
        - the only item: the item is removed (line rule).
    An instance's override list that becomes empty is removed whole (as in
    replace_module). A module's parameter port list that becomes empty is
    removed whole, from the end of the module's name through its ")", when
    only blanks separate the name from "#"; otherwise "#()" stays.

    Log targets: the module name for module-level removals except
    remove_instance, which logs "<module>.<instance>".
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hdl_vet.cli import main
from hdl_vet.ops import DriftError, Vet, VetError
from hdl_vet.output import write_tree
from hdl_vet.session import Session, SessionError
from hdl_vet.slang_adapter import load
from vetkit import make_design, tree_bytes

# A second small design: one child with a spare port and a spare parameter.
CHILD = (
    "module child #(\n"
    "  parameter int W      = 4,\n"
    "  parameter int UNUSED = 1\n"
    ") (\n"
    "  input  logic         clk,\n"
    "  input  logic [W-1:0] d,\n"
    "  input  logic         spare_i,\n"
    "  output logic [W-1:0] q,\n"
    "  output logic         dbg_o\n"
    ");\n"
    "  always_ff @(posedge clk) q <= d;\n"
    "  assign dbg_o = d[0];\n"
    "endmodule\n"
)
PARENT = (
    "module parent (\n"
    "  input  logic       clk,\n"
    "  input  logic [3:0] a,\n"
    "  output logic [3:0] y\n"
    ");\n"
    "  logic dbg;\n"
    "  child #(\n"
    "    .W     (4),\n"
    "    .UNUSED(7)\n"
    "  ) u_c (\n"
    "    .clk    (clk),\n"
    "    .d      (a),\n"
    "    .spare_i(1'b0),\n"
    "    .q      (y),\n"
    "    .dbg_o  (dbg)\n"
    "  );\n"
    "endmodule\n"
)

# Cells a script may add. Each is a drop-in for `leaf` unless its name says otherwise.
CELLS = {
    "cell_leaf.sv": (
        "module cell_leaf (\n"
        "  input  logic       CK,\n"
        "  input  logic [3:0] D,\n"
        "  output logic [3:0] Q\n"
        ");\n"
        "  always_ff @(posedge CK) Q <= D;\n"
        "endmodule\n"
    ),
    "wide_leaf.sv": (
        "module wide_leaf (\n"
        "  input  logic       clk,\n"
        "  input  logic [7:0] d,\n"
        "  output logic [3:0] q\n"
        ");\n"
        "  always_ff @(posedge clk) q <= d[3:0];\n"
        "endmodule\n"
    ),
    "backwards_leaf.sv": (
        "module backwards_leaf (\n"
        "  input  logic       clk,\n"
        "  output logic [3:0] d,\n"
        "  output logic [3:0] q\n"
        ");\n"
        "  assign d = '0;\n"
        "  assign q = '0;\n"
        "endmodule\n"
    ),
    "deep_leaf.sv": (
        "module deep_leaf #(\n"
        "  parameter int WIDTH = 4,\n"
        "  parameter int DEPTH = 5\n"
        ") (\n"
        "  input  logic             clk,\n"
        "  input  logic [WIDTH-1:0] d,\n"
        "  output logic [WIDTH-1:0] q\n"
        ");\n"
        "  always_ff @(posedge clk) q <= d;\n"
        "endmodule\n"
    ),
    "scan_leaf.sv": (
        "module scan_leaf (\n"
        "  input  logic       clk,\n"
        "  input  logic [3:0] d,\n"
        "  input  logic       scan_en,\n"
        "  output logic [3:0] q,\n"
        "  output logic       ready\n"
        ");\n"
        "  always_ff @(posedge clk) if (!scan_en) q <= d;\n"
        "  assign ready = 1'b1;\n"
        "endmodule\n"
    ),
    "second_leaf.sv": "module leaf (input logic clk);\nendmodule\n",
    "broken.sv": "module broken (input logic clk;\nendmodule\n",
    "includer.sv": '`include "cell_defs.svh"\nmodule includer (input logic clk);\nendmodule\n',
    "cell_defs.svh": "`define CELL_W 4\n",
    "warns.sv": (
        "module warns (\n"
        "  input  logic       clk,\n"
        "  input  logic [3:0] d,\n"
        "  output logic [3:0] q\n"
        ");\n"
        "  logic [1:0] narrow;\n"
        "  assign narrow = d;\n"
        "  always_ff @(posedge clk) q <= {2'b0, narrow};\n"
        "endmodule\n"
    ),
}


@pytest.fixture
def kit(tmp_path: Path):
    return make_design(tmp_path / "in")


@pytest.fixture
def vet(kit):
    return Vet(Session(kit.filelist, top="top"))


@pytest.fixture
def cells(tmp_path: Path) -> Path:
    d = tmp_path / "cells"
    d.mkdir()
    for name, text in CELLS.items():
        (d / name).write_bytes(text.encode())
    return d


@pytest.fixture
def family(tmp_path: Path):
    """The child/parent design: (Vet, child path, parent path)."""
    root = tmp_path / "fam"
    (root / "rtl").mkdir(parents=True)
    child, parent = root / "rtl" / "child.sv", root / "rtl" / "parent.sv"
    child.write_bytes(CHILD.encode())
    parent.write_bytes(PARENT.encode())
    (root / "design.f").write_bytes(b"rtl/child.sv\nrtl/parent.sv\n")
    return Vet(Session(root / "design.f", top="parent")), child.resolve(), parent.resolve()


def edits(diff: str) -> list[str]:
    return [
        line
        for line in diff.splitlines()
        if line[:1] in "+-" and not line.startswith(("+++", "---")) and "Modified by hdl-vet" not in line
    ]


def statuses(vet: Vet) -> list[tuple[str, str, str]]:
    return [(r.op, r.target, r.status) for r in vet.log]


def kinds(result) -> set[tuple[str, str, str]]:
    return {(c.kind, c.path, c.name) for c in result.changes}


def untouched(vet: Vet, *paths: Path) -> bool:
    return all(vet.text(p) == p.read_bytes() for p in paths)


L0 = "  leaf #(.WIDTH(4)) u_l0 (.clk(clk), .d(a), .q(y0));"
SPARE = "  leaf u_spare (.clk(clk), .d(spare_d), .q(spare_q));"


# --- adapter: dangling and unused ----------------------------------------------------


def test_dangling_and_unused_are_separate_from_the_diagnostics(tmp_path: Path):
    src = (
        "module m (input logic clk, input logic a, output logic y, output logic z);\n"
        "  logic v;\n"
        "  wire  n;\n"
        "  logic set_only;\n"
        "  assign set_only = a;\n"
        "  assign y = v & n;\n"
        "endmodule\n"
    )
    (tmp_path / "m.sv").write_bytes(src.encode())
    (tmp_path / "m.f").write_bytes(b"m.sv\n")
    d = load(tmp_path / "m.f", top="m")
    before = d.diagnostics()
    dangling = {(g.code, g.message) for g in d.dangling()}
    assert {c for c, _ in dangling} == {"UndrivenNet", "UndrivenPort", "UnassignedVariable"}
    assert any("'z'" in m for _, m in dangling)
    assert any("'v'" in m for _, m in dangling)
    assert any("'n'" in m for _, m in dangling)
    unused = {(g.code, g.message) for g in d.unused()}
    assert ("UnusedPort" in {c for c, _ in unused}) and any("'clk'" in m for _, m in unused)
    assert any(c == "UnusedButSetVariable" and "'set_only'" in m for c, m in unused)
    assert all(g.path == (tmp_path / "m.sv").resolve() and g.line is not None for g in d.dangling())
    assert d.diagnostics() == before
    assert not {g.code for g in before} & {c for c, _ in dangling | unused}


def test_a_clean_design_has_nothing_dangling(kit):
    d = load(kit.filelist, top="top")
    # `spare_q` is an implicit net driven by u_spare and read by nothing: unused, not dangling.
    assert d.dangling() == []


# --- session: checkpoint and rollback ------------------------------------------------


def test_rollback_restores_text_queue_design_and_expectations(kit):
    s = Session(kit.filelist, top="top")
    data = s.text(kit.mid)
    at = data.index(b"u_l0")
    s.splice(kit.mid, at, at + 4, b"u_first")
    cp = s.checkpoint()
    s.refresh()
    at = s.text(kit.mid).index(b"u_l1")
    s.splice(kit.mid, at, at + 4, b"u_second")
    s.expect("instance-added", "top.u_mid.u_second")
    assert [i.name for i in s.design.module("mid").instances] == ["u_first", "u_second"]

    s.rollback(cp)
    assert s.text(kit.mid) == data  # the first splice is queued again, not applied
    assert [i.name for i in s.loaded.module("mid").instances] == ["u_l0", "u_l1"]
    assert [i.name for i in s.design.module("mid").instances] == ["u_first", "u_l1"]
    s.expect("instance-removed", "top.u_mid.u_l0", exact=True)
    s.expect("instance-added", "top.u_mid.u_first", exact=True)
    r = s.finish()
    assert r.ok, r.report()  # the expectation made after the checkpoint is gone


def test_rollback_drops_an_added_file(kit, cells):
    s = Session(kit.filelist, top="top")
    cp = s.checkpoint()
    s.add_file(cells / "cell_leaf.sv")
    assert list(s.added) == ["hdl_vet_added/cell_leaf.sv"]
    assert "cell_leaf" in s.design.modules()
    s.rollback(cp)
    assert s.added == {}
    assert "cell_leaf" not in s.design.modules()
    r = s.finish()
    assert r.ok and r.added == {}


def test_an_added_file_cannot_be_spliced(kit, cells):
    s = Session(kit.filelist, top="top")
    s.add_file(cells / "cell_leaf.sv")
    with pytest.raises(SessionError, match="cell_leaf.sv"):
        s.splice(cells / "cell_leaf.sv", 0, 0, b"// x\n")


# --- add_file ---------------------------------------------------------------------------


def test_add_file_makes_its_modules_available(vet, kit, cells):
    vet.add_file(cells / "cell_leaf.sv")
    assert statuses(vet) == [("add_file", "cell_leaf.sv", "applied")]
    assert "cell_leaf" in vet.design.modules()
    assert vet.session.added == {"hdl_vet_added/cell_leaf.sv": CELLS["cell_leaf.sv"].encode()}
    vet.add_file(cells / "cell_leaf.sv")
    vet.add_file(kit.alt_leaf)  # already a source of the design
    assert [s for _, _, s in statuses(vet)] == ["applied", "skipped", "skipped"]
    assert list(vet.session.added) == ["hdl_vet_added/cell_leaf.sv"]


def test_add_file_takes_a_relative_path_from_the_current_directory(vet, cells, monkeypatch):
    monkeypatch.chdir(cells.parent)
    vet.add_file("cells/cell_leaf.sv")
    assert list(vet.session.added) == ["hdl_vet_added/cell_leaf.sv"]


@pytest.mark.parametrize(
    ("name", "needle"),
    [
        ("missing.sv", "missing.sv"),
        ("second_leaf.sv", "duplicate definition"),  # the design already has a module `leaf`
        ("broken.sv", "expected"),  # syntax error
        ("includer.sv", "cell_defs.svh"),  # a header that is not a file of the design
    ],
)
def test_add_file_refusals_leave_nothing_behind(vet, kit, cells, name, needle):
    with pytest.raises(VetError, match=needle) as e:
        vet.add_file(cells / name)
    assert str(e.value).startswith("add_file")
    assert vet.log == [] and vet.session.added == {}
    assert name.removesuffix(".sv") not in vet.design.modules() or name == "second_leaf.sv"
    r = vet.finish()
    assert r.ok and r.files == {} and r.added == {}


def test_two_added_files_cannot_share_a_name(vet, cells, tmp_path: Path):
    other = tmp_path / "other" / "cell_leaf.sv"
    other.parent.mkdir()
    other.write_bytes(b"module other_cell (input logic clk);\nendmodule\n")
    vet.add_file(cells / "cell_leaf.sv")
    with pytest.raises(VetError, match="cell_leaf.sv"):
        vet.add_file(other)
    assert list(vet.session.added) == ["hdl_vet_added/cell_leaf.sv"]


def test_an_input_tree_with_the_added_folder_is_refused(kit, cells):
    (kit.root / "hdl_vet_added").mkdir()
    vet = Vet(Session(kit.filelist, top="top"))
    with pytest.raises(VetError, match="hdl_vet_added"):
        vet.add_file(cells / "cell_leaf.sv")


def test_an_added_file_nothing_instantiates_fails_sign_off(vet, cells):
    vet.add_file(cells / "cell_leaf.sv")
    r = vet.finish()
    assert not r.ok
    assert r.failed_checks == ["add_file cell_leaf.sv: none of its modules is instantiated"]


def test_warnings_inside_an_added_file_are_not_new_diagnostics(vet, cells):
    vet.add_file(cells / "warns.sv")  # holds a WidthTruncate of its own
    vet.module("mid").instance("u_l0").replace_module("warns", drop_params=["WIDTH"])
    r = vet.finish()
    assert r.ok, r.report()
    assert r.new_diagnostics == []


# --- replace_module -----------------------------------------------------------------------


def test_replace_module_with_one_already_in_the_design(vet, kit):
    vet.module("mid").instance("u_l0").replace_module("alt_leaf", ports={"d": "din"}, drop_params=["WIDTH"])
    assert statuses(vet) == [("replace_module", "mid.u_l0", "applied")]
    r = vet.finish()
    assert r.ok, r.report()
    assert edits(r.diff) == ["-" + L0, "+  alt_leaf u_l0 (.clk(clk), .din(a), .q(y0));"]
    assert ("module-changed", "top.u_mid.u_l0", "") in kinds(r)
    assert {c.path for c in r.changes} == {"top.u_mid.u_l0"}
    assert list(r.files) == [kit.mid] and r.added == {}


def test_replace_module_with_an_added_cell_and_renamed_ports(vet, kit, cells):
    vet.add_file(cells / "cell_leaf.sv")
    vet.module("mid").instance("u_l1").replace_module(
        "cell_leaf", ports={"clk": "CK", "d": "D", "q": "Q"}, drop_params=["WIDTH", "DEPTH"]
    )
    r = vet.finish()
    assert r.ok, r.report()
    assert edits(r.diff) == [
        "-  leaf #(.WIDTH(4), .DEPTH(3)) u_l1 (",
        "-    .clk (clk),",
        "-    .d   (a),",
        "-    .q   (y1)",
        "+  cell_leaf u_l1 (",
        "+    .CK (clk),",
        "+    .D   (a),",
        "+    .Q   (y1)",
    ]
    assert r.added == {"hdl_vet_added/cell_leaf.sv": CELLS["cell_leaf.sv"].encode()}
    assert list(r.files) == [kit.mid]


def test_everything_must_be_accounted_for(vet, kit):
    inst = vet.module("mid").instance("u_l0")
    with pytest.raises(VetError) as e:
        inst.replace_module("alt_leaf")
    msg = str(e.value)
    assert msg.startswith("replace_module mid.u_l0")
    assert "'d'" in msg and "'WIDTH'" in msg and "'din'" in msg  # all problems in one message
    with pytest.raises(VetError, match="'din'"):
        inst.replace_module("alt_leaf", drop_ports=["d"], drop_params=["WIDTH"])  # input left open
    with pytest.raises(VetError, match="'WIDTH'"):
        inst.replace_module("alt_leaf", ports={"d": "din"})
    assert vet.log == [] and untouched(vet, kit.mid)


@pytest.mark.parametrize(
    ("kwargs", "needle"),
    [
        ({"ports": {"dd": "din"}}, r"'dd'.*did you mean 'd'"),
        ({"ports": {"d": "dinn"}}, r"'dinn'.*did you mean 'din'"),
        ({"ports": {"d": "din", "clk": "din"}}, "'din'"),
        ({"ports": {"d": "din"}, "connect": {"din": "a"}}, "'din'"),
        ({"ports": {"d": "din"}, "unconnected": ["clk"]}, "'clk'"),
        ({"ports": {"d": "din"}, "set_params": {"NOPE": 1}}, "'NOPE'"),
        ({"ports": {"d": "din"}, "drop_ports": ["nope"]}, "'nope'"),
        ({"ports": {"d": "din"}, "drop_params": ["WIDTH", "DEPTH"]}, "'DEPTH'"),  # u_l0 has no DEPTH override
    ],
)
def test_bad_maps_are_refused_before_any_edit(vet, kit, kwargs, needle):
    kwargs.setdefault("drop_params", ["WIDTH"])
    with pytest.raises(VetError, match=needle):
        vet.module("mid").instance("u_l0").replace_module("alt_leaf", **kwargs)
    assert vet.log == [] and untouched(vet, kit.mid)
    assert vet.session.reloads == 0


def test_unknown_module_suggests_one(vet):
    with pytest.raises(VetError, match=r"'alt_leaff'.*did you mean 'alt_leaf'"):
        vet.module("mid").instance("u_l0").replace_module("alt_leaff")


def test_a_port_of_another_width_is_refused_and_taken_back(vet, kit, cells):
    vet.add_file(cells / "wide_leaf.sv")
    with pytest.raises(VetError, match=r"'d'.*(4.*8|8.*4)") as e:
        vet.module("mid").instance("u_l0").replace_module("wide_leaf", drop_params=["WIDTH"])
    assert str(e.value).startswith("replace_module mid.u_l0")
    assert statuses(vet) == [("add_file", "wide_leaf.sv", "applied")]
    assert untouched(vet, kit.mid)
    assert vet.design.module("mid").instance("u_l0").module == "leaf"
    # The script can carry on: a correct swap of the other cell still works.
    vet.add_file(cells / "cell_leaf.sv")
    vet.module("mid").instance("u_l0").replace_module(
        "cell_leaf", ports={"clk": "CK", "d": "D", "q": "Q"}, drop_params=["WIDTH"]
    )
    r = vet.finish()
    assert not r.ok  # only because wide_leaf.sv was added and never used
    assert r.failed_checks == ["add_file wide_leaf.sv: none of its modules is instantiated"]
    assert r.unexpected_changes == [] and r.new_diagnostics == []


def test_a_port_of_another_direction_is_refused(vet, kit, cells):
    vet.add_file(cells / "backwards_leaf.sv")
    with pytest.raises(VetError, match=r"'d'.*(input.*output|output.*input)"):
        vet.module("mid").instance("u_l0").replace_module("backwards_leaf", drop_params=["WIDTH"])
    assert untouched(vet, kit.mid)


def test_a_default_that_would_change_the_value_in_use_is_refused(vet, kit, cells):
    vet.add_file(cells / "deep_leaf.sv")
    inst = vet.module("mid").instance("u_l0")  # overrides WIDTH only; leaf's DEPTH is 2, deep_leaf's is 5
    with pytest.raises(VetError, match="'DEPTH'"):
        inst.replace_module("deep_leaf")
    assert untouched(vet, kit.mid)
    inst.replace_module("deep_leaf", set_params={"DEPTH": 2})
    r = vet.finish()
    assert r.ok, r.report()
    assert edits(r.diff) == ["-" + L0, "+  deep_leaf #(.WIDTH(4), .DEPTH(2)) u_l0 (.clk(clk), .d(a), .q(y0));"]


def test_an_overridden_parameter_carries_over(vet, cells):
    vet.add_file(cells / "deep_leaf.sv")
    vet.module("mid").instance("u_l1").replace_module("deep_leaf")  # .WIDTH(4), .DEPTH(3) are kept
    r = vet.finish()
    assert r.ok, r.report()
    assert edits(r.diff) == ["-  leaf #(.WIDTH(4), .DEPTH(3)) u_l1 (", "+  deep_leaf #(.WIDTH(4), .DEPTH(3)) u_l1 ("]


def test_new_ports_are_connected_or_named_unconnected(vet, kit, cells):
    vet.add_file(cells / "scan_leaf.sv")
    inst = vet.module("mid").instance("u_l0")
    with pytest.raises(VetError) as e:
        inst.replace_module("scan_leaf", drop_params=["WIDTH"])
    assert "'scan_en'" in str(e.value) and "'ready'" in str(e.value)
    with pytest.raises(VetError, match="'scan_en'"):  # an input cannot be left unconnected
        inst.replace_module("scan_leaf", drop_params=["WIDTH"], unconnected=["scan_en", "ready"])
    inst.replace_module("scan_leaf", drop_params=["WIDTH"], connect={"scan_en": "1'b0"}, unconnected=["ready"])
    r = vet.finish()
    assert r.ok, r.report()
    assert edits(r.diff) == ["-" + L0, "+  scan_leaf u_l0 (.clk(clk), .d(a), .q(y0), .scan_en(1'b0), .ready());"]


def test_replace_module_skip_and_drift(vet, kit, cells):
    vet.add_file(cells / "scan_leaf.sv")
    inst = vet.module("mid").instance("u_l0")
    args = {"drop_params": ["WIDTH"], "connect": {"scan_en": "1'b0"}, "unconnected": ["ready"]}
    with pytest.raises(DriftError, match="'leaf'"):
        inst.replace_module("scan_leaf", was="alt_leaf", **args)
    inst.replace_module("scan_leaf", was="leaf", **args)
    inst.replace_module("scan_leaf", was="leaf", **args)
    assert [s for _, _, s in statuses(vet)] == ["applied", "applied", "skipped"]
    with pytest.raises(DriftError, match="scan_en"):
        inst.replace_module("scan_leaf", connect={"scan_en": "1'b1"}, unconnected=["ready"])
    r = vet.finish()
    assert r.ok, r.report()


def test_replace_module_in_a_shared_parent_needs_the_count(tmp_path: Path, cells):
    kit = make_design(tmp_path / "shared")
    kit.top.write_bytes(
        kit.top.read_bytes().replace(
            b"  leaf u_spare", b"  mid u_mid2 (.clk(clk), .a(a), .y0(), .y1());\n  leaf u_spare"
        )
    )
    vet = Vet(Session(kit.filelist, top="top"))
    with pytest.raises(VetError, match="instances=2"):
        vet.module("mid").instance("u_l0").replace_module("alt_leaf", ports={"d": "din"}, drop_params=["WIDTH"])
    vet.module("mid", instances=2).instance("u_l0").replace_module(
        "alt_leaf", ports={"d": "din"}, drop_params=["WIDTH"]
    )
    r = vet.finish()
    assert r.ok, r.report()
    assert {p for k, p, _ in kinds(r) if k == "module-changed"} == {"top.u_mid.u_l0", "top.u_mid2.u_l0"}


def test_the_swapped_instance_can_be_edited_further(vet, cells):
    vet.add_file(cells / "scan_leaf.sv")
    inst = vet.module("mid").instance("u_l0")
    inst.replace_module("scan_leaf", drop_params=["WIDTH"], connect={"scan_en": "1'b0"}, unconnected=["ready"])
    vet.module("mid").add_port("scan_en_i", "input")
    inst.connect("scan_en", "scan_en_i", was="1'b0")
    vet.module("top").instance("u_mid").connect("scan_en_i", "1'b0")
    r = vet.finish()
    assert r.ok, r.report()
    assert "+  scan_leaf u_l0 (.clk(clk), .d(a), .q(y0), .scan_en(scan_en_i), .ready());" in edits(r.diff)


def test_a_module_defined_in_an_added_file_is_not_editable(vet, cells):
    vet.add_file(cells / "scan_leaf.sv")
    vet.module("mid").instance("u_l0").replace_module(
        "scan_leaf", drop_params=["WIDTH"], connect={"scan_en": "1'b0"}, unconnected=["ready"]
    )
    with pytest.raises((VetError, SessionError), match="scan_leaf"):
        vet.module("scan_leaf").add_port("extra_i", "input")


# --- removals ------------------------------------------------------------------------------


def test_remove_an_instance_nothing_depends_on(vet, kit):
    vet.module("top").remove_instance("u_spare", module="leaf")
    assert statuses(vet) == [("remove_instance", "top.u_spare", "applied")]
    assert any(n.startswith("remove_instance top.u_spare") and "spare_d" in n for n in vet.notes)
    r = vet.finish()
    assert r.ok, r.report()
    assert edits(r.diff) == ["-" + SPARE]
    assert kinds(r) == {("instance-removed", "top.u_spare", "")}


def test_remove_instance_skip_and_drift(vet, kit):
    top = vet.module("top")
    top.remove_instance("u_gone")
    with pytest.raises(DriftError, match="'leaf'"):
        top.remove_instance("u_spare", module="mid")
    assert statuses(vet) == [("remove_instance", "top.u_gone", "skipped")]
    assert untouched(vet, kit.top)


def test_removing_a_driver_that_is_still_read_is_refused(vet, kit):
    with pytest.raises(VetError, match="y0") as e:
        vet.module("mid").remove_instance("u_l0")
    assert str(e.value).startswith("remove_instance mid.u_l0")
    assert vet.log == [] and vet.notes == [] and untouched(vet, kit.mid)
    r = vet.finish()
    assert r.ok and r.files == {}


def test_a_group_removes_a_driver_and_ties_the_signal_off(vet, kit):
    mid = vet.module("mid")
    with vet.group():
        mid.remove_instance("u_l0")
        mid.add_assign("y0", "'0")
    assert statuses(vet) == [("remove_instance", "mid.u_l0", "applied"), ("add_assign", "mid", "applied")]
    r = vet.finish()
    assert r.ok, r.report()
    assert edits(r.diff) == ["-" + L0, "+  assign y0 = '0;"]
    assert kinds(r) == {("instance-removed", "top.u_mid.u_l0", "")}


def test_a_group_that_leaves_something_dangling_is_taken_back_whole(vet, kit):
    top = vet.module("top")
    top.add_port("early_i", "input")  # before the group: stays
    with pytest.raises(VetError, match="y0") as e:
        with vet.group():
            top.add_port("late_i", "input")
            vet.module("mid").remove_instance("u_l0")
    assert str(e.value).startswith("group:")
    assert statuses(vet) == [("add_port", "top", "applied")]
    assert untouched(vet, kit.mid)
    r = vet.finish()
    assert r.ok, r.report()
    assert [line for line in edits(r.diff) if line.startswith("+")] == [
        "+  output logic [3:0] y1,",
        "+  input  logic early_i",
    ]


def test_an_exception_inside_a_group_takes_the_group_back(vet, kit):
    with pytest.raises(RuntimeError, match="stop"):
        with vet.group():
            vet.module("top").remove_instance("u_spare")
            raise RuntimeError("stop")
    assert vet.log == [] and vet.notes == [] and untouched(vet, kit.top)
    with pytest.raises(VetError, match="u_nope"):
        with vet.group():
            vet.module("top").remove_instance("u_spare")
            vet.module("top").instance("u_nope")
    assert vet.log == [] and untouched(vet, kit.top)


def test_groups_nest_into_the_outer_one(vet, kit):
    mid = vet.module("mid")
    with vet.group():
        with vet.group():
            mid.remove_instance("u_l0")  # dangling here, fine: the outer group is not over
        mid.add_assign("y0", "'0")
    assert vet.finish().ok


def test_remove_assign_wire_and_instance_step_by_step(vet, kit):
    top = vet.module("top")
    with pytest.raises(VetError, match="spare_d"):
        top.remove_assign("spare_d")  # u_spare still reads it
    with pytest.raises(VetError, match="spare_d"):
        top.remove_wire("spare_d")  # still assigned and read
    top.remove_instance("u_spare")
    with pytest.raises(DriftError, match="spare_d"):
        top.remove_assign("spare_d", was="b")
    top.remove_assign("spare_d", was="a")
    top.remove_wire("spare_d")
    top.remove_wire("spare_d")
    top.remove_assign("spare_d")
    assert statuses(vet) == [
        ("remove_instance", "top.u_spare", "applied"),
        ("remove_assign", "top", "applied"),
        ("remove_wire", "top", "applied"),
        ("remove_wire", "top", "skipped"),
        ("remove_assign", "top", "skipped"),
    ]
    r = vet.finish()
    assert r.ok, r.report()
    assert edits(r.diff) == ["-  logic [3:0] spare_d;", "-  assign spare_d = a;", "-" + SPARE]


def test_remove_wire_refuses_ports_and_unknown_kinds(vet):
    with pytest.raises(VetError, match="'a'"):
        vet.module("top").remove_wire("a")
    with pytest.raises(VetError, match="'u_mid'"):
        vet.module("top").remove_wire("u_mid")


def test_remove_an_unused_port_and_its_connection(family):
    vet, child, parent = family
    vet.module("child").remove_port("spare_i")
    vet.module("child").remove_port("spare_i")
    assert statuses(vet) == [("remove_port", "child", "applied"), ("remove_port", "child", "skipped")]
    r = vet.finish()
    assert r.ok, r.report()
    assert edits(r.diff) == ["-  input  logic         spare_i,", "-    .spare_i(1'b0),"]
    assert ("port-removed", "parent.u_c", "spare_i") in kinds(r)
    assert kinds(r) <= {("port-removed", "parent.u_c", "spare_i"), ("connection-changed", "parent.u_c", "spare_i")}
    assert sorted(r.files) == sorted([child, parent])


def test_remove_a_port_that_is_still_used_is_refused(family):
    vet, child, parent = family
    with pytest.raises(VetError, match="dbg_o"):
        vet.module("child").remove_port("dbg_o")  # `assign dbg_o = ...` still names it
    with pytest.raises(VetError, match="dbg_o"):
        vet.module("child").remove_assign("dbg_o")  # the output would be undriven
    with pytest.raises(VetError, match="'q'|'y'"):
        vet.module("child").remove_port("q")  # driven inside; `y` of parent would be undriven
    assert vet.log == [] and untouched(vet, child, parent)


def test_a_group_removes_the_last_port_with_its_driver(family):
    vet, _child, _parent = family
    c = vet.module("child")
    with vet.group():
        c.remove_assign("dbg_o")
        c.remove_port("dbg_o")
    assert any(n.startswith("group:") and "'dbg'" in n for n in vet.notes)  # parent's `dbg` is now unused
    r = vet.finish()
    assert r.ok, r.report()
    assert edits(r.diff) == [
        "-  output logic [W-1:0] q,",
        "-  output logic         dbg_o",
        "+  output logic [W-1:0] q",
        "-  assign dbg_o = d[0];",
        "-    .q      (y),",
        "-    .dbg_o  (dbg)",
        "+    .q      (y)",
    ]
    assert ("port-removed", "parent.u_c", "dbg_o") in kinds(r)


def test_remove_a_parameter_and_its_override(family):
    vet, _child, _parent = family
    c = vet.module("child")
    with pytest.raises(VetError, match="'W'"):
        c.remove_param("W")  # the port widths use it
    c.remove_param("UNUSED")
    assert statuses(vet) == [("remove_param", "child", "applied")]
    r = vet.finish()
    assert r.ok, r.report()
    assert edits(r.diff) == [
        "-  parameter int W      = 4,",
        "-  parameter int UNUSED = 1",
        "+  parameter int W      = 4",
        "-    .W     (4),",
        "-    .UNUSED(7)",
        "+    .W     (4)",
    ]
    assert kinds(r) == {("param-changed", "parent.u_c", "UNUSED")}


def test_a_port_another_port_takes_its_type_from_is_refused(vet, kit):
    with pytest.raises(VetError, match="y1"):
        vet.module("mid").remove_port("y0")  # `output logic [3:0] y0, y1`
    assert vet.session.reloads == 0 and untouched(vet, kit.mid)


def test_removals_in_a_shared_module_need_the_count(vet):
    with pytest.raises(VetError, match="3"):
        vet.module("leaf").remove_param("DEPTH")
    vet.module("leaf", instances=3).remove_param("DEPTH")
    r = vet.finish()
    assert r.ok, r.report()
    assert edits(r.diff) == [
        "-  parameter int WIDTH = 4,",
        "-  parameter int DEPTH = 2",
        "+  parameter int WIDTH = 4",
        "-  leaf #(.WIDTH(4), .DEPTH(3)) u_l1 (",
        "+  leaf #(.WIDTH(4)) u_l1 (",
    ]
    assert kinds(r) == {("param-changed", p, "DEPTH") for p in ("top.u_mid.u_l0", "top.u_mid.u_l1", "top.u_spare")}


def test_parameter_lists_that_become_empty_are_removed(tmp_path: Path):
    root = tmp_path / "one"
    root.mkdir()
    (root / "one.sv").write_bytes(
        b"module one #(parameter int P = 1) (input logic clk);\n"
        b"endmodule\n"
        b"module holder (input logic clk);\n"
        b"  one #(.P(2)) u_one (.clk(clk));\n"
        b"endmodule\n"
    )
    (root / "one.f").write_bytes(b"one.sv\n")
    vet = Vet(Session(root / "one.f", top="holder"))
    vet.module("one").remove_param("P")
    r = vet.finish()
    assert r.ok, r.report()
    assert edits(r.diff) == [
        "-module one #(parameter int P = 1) (input logic clk);",
        "+module one (input logic clk);",
        "-  one #(.P(2)) u_one (.clk(clk));",
        "+  one u_one (.clk(clk));",
    ]


def test_an_empty_group_does_nothing(vet):
    with vet.group():
        pass
    assert vet.log == [] and vet.session.reloads == 0
    assert vet.finish().ok


def test_remove_instance_refuses_a_statement_with_several_instances(tmp_path: Path):
    kit = make_design(tmp_path / "multi")
    kit.top.write_bytes(
        kit.top.read_bytes().replace(
            b"  leaf u_spare (.clk(clk), .d(spare_d), .q(spare_q));",
            b"  leaf u_spare (.clk(clk), .d(spare_d), .q(spare_q)), u_twin (.clk(clk), .d(spare_d), .q());",
        )
    )
    vet = Vet(Session(kit.filelist, top="top"))
    with pytest.raises(VetError, match="several"):
        vet.module("top").remove_instance("u_twin")


# --- output tree and command line ------------------------------------------------------------


def swap_script(cells: Path) -> str:
    return (
        "from pathlib import Path\n"
        "def apply(vet):\n"
        f"    vet.add_file(Path({str(cells)!r}) / 'cell_leaf.sv')\n"
        "    vet.module('mid').instance('u_l0').replace_module(\n"
        "        'cell_leaf', ports={'clk': 'CK', 'd': 'D', 'q': 'Q'}, drop_params=['WIDTH'])\n"
        "    vet.module('top').remove_instance('u_spare')\n"
    )


def test_write_tree_copies_added_files_and_lists_them(vet, kit, cells, tmp_path: Path):
    vet.add_file(cells / "cell_leaf.sv")
    vet.module("mid").instance("u_l0").replace_module(
        "cell_leaf", ports={"clk": "CK", "d": "D", "q": "Q"}, drop_params=["WIDTH"]
    )
    r = vet.finish()
    assert r.ok, r.report()
    out = write_tree(vet.session, r, tmp_path / "out")
    tree = tree_bytes(out)
    assert tree["hdl_vet_added/cell_leaf.sv"] == CELLS["cell_leaf.sv"].encode()
    assert tree["design.f"].decode().splitlines() == [
        "rtl/leaf.sv",
        "rtl/alt_leaf.sv",
        "rtl/mid.sv",
        "rtl/top.sv",
        "hdl_vet_added/cell_leaf.sv",
    ]
    manifest = json.loads(tree["hdl-vet.json"])
    assert list(manifest["added"]) == ["hdl_vet_added/cell_leaf.sv"]
    assert len(manifest["added"]["hdl_vet_added/cell_leaf.sv"]["sha256"]) == 64
    assert sorted(manifest["changed"]) == ["rtl/mid.sv"]
    # The written tree is a design of its own.
    d = load(out / "design.f", top="top")
    assert [g for g in d.diagnostics() if g.severity == "error"] == []
    assert d.snapshot()["top.u_mid.u_l0"].module == "cell_leaf"
    assert (cells / "cell_leaf.sv").read_bytes() == CELLS["cell_leaf.sv"].encode()


def test_the_manifest_lists_no_added_files_when_there_are_none(vet, tmp_path: Path):
    vet.module("top").remove_instance("u_spare")
    r = vet.finish()
    out = write_tree(vet.session, r, tmp_path / "out")
    assert json.loads((out / "hdl-vet.json").read_bytes())["added"] == {}


def test_command_line_reports_added_files_and_notes(kit, cells, tmp_path: Path, capsys):
    script = tmp_path / "swap.py"
    script.write_text(swap_script(cells), encoding="utf-8")
    out = tmp_path / "out"
    code = main(["apply", "-f", str(kit.filelist), "--top", "top", "-s", str(script), "-o", str(out)])
    captured = capsys.readouterr()
    assert code == 0, captured.err
    lines = captured.out.splitlines()
    assert "3 applied, 0 skipped" in lines
    assert "changed rtl/mid.sv" in lines and "changed rtl/top.sv" in lines
    assert "added hdl_vet_added/cell_leaf.sv" in lines
    assert any(line.startswith("note: remove_instance top.u_spare") for line in lines)
    assert (out / "hdl_vet_added" / "cell_leaf.sv").is_file()


def test_command_line_reports_a_refused_swap_and_writes_nothing(kit, cells, tmp_path: Path, capsys):
    script = tmp_path / "bad.py"
    script.write_text(
        "from pathlib import Path\n"
        "def apply(vet):\n"
        f"    vet.add_file(Path({str(cells)!r}) / 'wide_leaf.sv')\n"
        "    vet.module('mid').instance('u_l0').replace_module('wide_leaf', drop_params=['WIDTH'])\n",
        encoding="utf-8",
    )
    out = tmp_path / "out"
    code = main(["apply", "-f", str(kit.filelist), "--top", "top", "-s", str(script), "-o", str(out)])
    captured = capsys.readouterr()
    assert code == 2
    assert "script bad.py: replace_module mid.u_l0" in captured.err
    assert "Traceback" not in captured.err
    assert not out.exists()
