"""The functions that load a design and read parameter names."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pyslang

from hdl_vet.filelist import Filelist, read_filelist
from hdl_vet.slang_adapter.design import Design
from hdl_vet.slang_adapter.loading import LoadCache, _Loader, _Shared, _Unsafe
from hdl_vet.slang_adapter.model import _param_kind
from hdl_vet.slang_adapter.records import AdapterError
from hdl_vet.slang_adapter.text import _items


def load(
    filelist: Path | Filelist,
    top: str | None = None,
    overrides: Mapping[Path, bytes] | None = None,
    base: Path | None = None,
    *,
    check_overrides: bool = True,
    cache: LoadCache | None = None,
    param_overrides: Mapping[str, str] | None = None,
) -> Design:
    """Load a design.

    `filelist` is a path (read now) or an already parsed `Filelist`. With
    `check_overrides`, an override for a file that is neither a source nor an
    included file is an error. `cache` carries what stays valid from one load
    of a session to the next (see LoadCache): file-system answers and parse trees.
    `param_overrides` gives values to parameters of `top` (like the tool option `-G`); it needs no `cache`.
    """
    if param_overrides and (cache is not None or top is None):
        raise ValueError("param_overrides needs a top module and no cache")
    fl = filelist if isinstance(filelist, Filelist) else read_filelist(filelist, base=base)
    if cache is None:
        ov = {Path(k).resolve(): bytes(v) for k, v in (overrides or {}).items()}
        # Even one load asks for the same header many times; a private cache
        # covers that. Nothing of it outlives the load.
        use = LoadCache(reuse=False)
    else:
        cache.bind(fl)
        cache.bind_top(top)
        ov = {cache.resolved(Path(k)): bytes(v) for k, v in (overrides or {}).items()}
        use = cache

    shared = use._shared if use.reuse else None
    if shared is not None and (shared.unsafe or shared.grown() or not shared.headers_unchanged(ov, use)):
        shared = None  # not provably safe (or too big) to reuse: start over
    for _attempt in (0, 1):
        reused = shared is not None
        if shared is None:
            shared = _Shared(fl, top, param_overrides)
            if use.reuse:
                use._shared = shared
        try:
            return _load(fl, top, ov, check_overrides, use, shared, strict=reused)
        except _Unsafe:
            shared = use._shared = None  # redo the load on a fresh state
        except BaseException as e:
            # Entries are stored only once whole, so a refusal before any registration
            # (bad UTF-8, top not found) leaves the state valid; anything else may
            # have left a half-registered header or version behind.
            if shared.dirty or not isinstance(e, AdapterError):
                use._shared = None
                if reused and not isinstance(e, AdapterError):
                    shared = None  # the reused state may be why (slang remembers failed lookups)
                    continue
            raise
    raise AssertionError("a fresh state cannot be unsafe")  # pragma: no cover


def _load(
    fl: Filelist,
    top: str | None,
    ov: dict[Path, bytes],
    check_overrides: bool,
    cache: LoadCache,
    shared: _Shared,
    strict: bool = False,
) -> Design:
    loader = _Loader(fl, ov, shared, cache, strict)
    bag = shared.bag
    trees = []
    for src in fl.files:
        tree = loader.reusable(src)
        trees.append(tree if tree is not None else loader.tree(src, bag))
    if shared.initial is None:
        shared.initial = shared.nbuf
    used = shared.included(fl.files)
    for header in sorted(used & shared.bad.keys()):
        raise AdapterError(f"{header}: not valid UTF-8, refusing to load ({shared.bad[header]})")
    if check_overrides:
        for key in ov:
            if key not in fl.files and key not in used:
                raise AdapterError(f"{key}: override is neither a source nor an included file")

    comp = pyslang.ast.Compilation(bag)
    for tree in trees:
        comp.addSyntaxTree(tree)
    if top is not None and not comp.getRoot().topInstances:
        raise AdapterError(f"top module {top!r} not found")
    return Design(fl, loader, shared, dict(zip(fl.files, trees, strict=True)), comp, used)


def check_expression(text: str) -> str | None:
    """None when `text` reads as ONE expression of its own, else the reason it does not.

    The text is put into `assign x = e ? TEXT : k;` and parsed. It must parse without a diagnostic as one
    assignment whose then-branch is the whole text. A comment is refused too.
    """
    if "//" in text or "/*" in text:
        return "it has a comment"
    tree = pyslang.syntax.SyntaxTree.fromText(f"module _p;\nassign _x = _e ? {text} : _k;\nendmodule\n")
    reason = "it cannot be read as one expression"
    if list(tree.diagnostics):
        return reason
    root: Any = tree.root
    members = list(root.members)
    assigns = list(members[0].assignments) if len(members) == 1 else []
    if len(assigns) != 1:
        return reason
    expr = assigns[0].right
    if expr.kind != pyslang.syntax.SyntaxKind.ConditionalExpression:
        return reason
    if str(expr.left).strip() != text.strip() or str(expr.right).strip() != "_k":
        return reason
    return None


def value_identifiers(text: str) -> frozenset[str]:
    """The names that the expression `text` writes (also the first name of a select or a path).

    Read from the syntax tree, not from the text. An escaped name is given without its backslash.
    """
    tree = pyslang.syntax.SyntaxTree.fromText(f"module _p;\nassign _x = _e ? {text} : _k;\nendmodule\n")
    kinds = (pyslang.syntax.SyntaxKind.IdentifierName, pyslang.syntax.SyntaxKind.IdentifierSelectName)
    found: set[str] = set()

    def visit(node: Any) -> None:
        if getattr(node, "kind", None) in kinds:
            found.add(str(node.identifier.valueText))

    tree.root.visit(visit)
    return frozenset(found - {"_x", "_e", "_k"})


def parameter_names(
    files: Sequence[Path | str],
    incdirs: Sequence[Path | str],
    defines: Mapping[str, str | None],
    module: str,
) -> frozenset[str]:
    """Names of the parameters of `module` that an instance (or a tool option) can override.

    A `localparam` and a type parameter are not in the answer. Raises AdapterError when the
    design cannot be loaded or has no such module.
    """
    paths = [Path(f).resolve() for f in files]
    root = paths[0].parent if paths else Path.cwd()
    listing = Filelist(paths, [Path(d).resolve() for d in incdirs], dict(defines), root)
    design = load(listing, module)
    found = design._comp.tryGetDefinition(module, design._comp.getRoot()).definition
    if found is None or found.syntax is None:
        raise AdapterError(f"module {module!r} not found")
    # The syntax is read as it is, also where an `include` brought a declaration in: only the names count here.
    header = found.syntax.header
    declarations = []
    if header.parameters is not None:
        declarations = list(_items(header.parameters.declarations))
    else:  # without a parameter port list, the parameters of the body can be overridden
        declarations = [
            m.parameter
            for m in _items(found.syntax.members)
            if m.kind == pyslang.syntax.SyntaxKind.ParameterDeclarationStatement
        ]
    return frozenset(
        d.name.valueText for decl in declarations if _param_kind(decl) == "value" for d in _items(decl.declarators)
    )


def instance_parameters(
    files: Sequence[Path | str],
    incdirs: Sequence[Path | str],
    defines: Mapping[str, str | None],
    module: str,
    params: Mapping[str, str],
    names: frozenset[str],
) -> dict[str, dict[str, str]]:
    """The parameter values of each instance of a module of `names` below `module`, by hierarchical path.

    `params` gives values to the parameters of `module`, as the tool option `-G` does. Raises AdapterError
    when the design cannot be loaded.
    """
    paths = [Path(f).resolve() for f in files]
    root = paths[0].parent if paths else Path.cwd()
    listing = Filelist(paths, [Path(d).resolve() for d in incdirs], dict(defines), root)
    return load(listing, module, param_overrides=params).parameters_of(names)
