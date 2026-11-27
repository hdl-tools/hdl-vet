"""The model of the modules and instances of a `Design`."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import pyslang

from hdl_vet.slang_adapter.diagnostics import driven_bases
from hdl_vet.slang_adapter.host import _DesignHost
from hdl_vet.slang_adapter.records import (
    AdapterError,
    AssignInfo,
    ConnInfo,
    DeclInfo,
    InstanceInfo,
    InstanceSite,
    ModuleInfo,
    ParamInfo,
    ParamOverride,
    PortInfo,
)
from hdl_vet.slang_adapter.text import _clean_text, _first_token, _items, _text, _tokens_of, tokens

_PROBE = "__hdlvet_probe"  # the helper module of `interface_ports`
_DIRECTIVES = {
    "IfDefDirective": "`ifdef",
    "IfNDefDirective": "`ifndef",
    "ElsIfDirective": "`elsif",
    "ElseDirective": "`else",
    "EndIfDirective": "`endif",
}
_GENERATE_KINDS = ("GenerateBlock", "IfGenerate", "LoopGenerate", "CaseGenerate")


def _conditional_directive(node: Any, skip_first: bool = True) -> str | None:
    """The first preprocessor conditional directive inside a parenthesised list.

    The trivia of the opening token lies before the list, so it does not count
    (`skip_first`); for an instance node the first token is the instance name.
    """
    found: list[str] = []
    seen_first = not skip_first

    def scan(n: Any) -> None:
        nonlocal seen_first
        if found or not isinstance(n, pyslang.parsing.Token):
            return
        if not seen_first:
            seen_first = True
            return
        for t in n.trivia:
            if t.kind == pyslang.parsing.TriviaKind.Directive:
                syn = t.syntax()
                name = _DIRECTIVES.get(str(syn.kind).rsplit(".", 1)[-1]) if syn is not None else None
                if name is not None:
                    found.append(name)
                    return

    node.visit(scan)
    return found[0] if found else None


def _in_generate(node: Any) -> bool:
    p = node.parent
    while p is not None:
        kind = str(p.kind).rsplit(".", 1)[-1]
        if kind in _GENERATE_KINDS:
            return True
        if kind in ("ModuleDeclaration", "InterfaceDeclaration", "ProgramDeclaration"):
            return False
        p = p.parent
    return False


def _other_names(syn: Any) -> frozenset[str]:
    """Names in the module's own scope besides ports, parameters, nets and instances."""
    sk = pyslang.syntax.SyntaxKind
    tk = pyslang.parsing.TokenKind
    names: set[str] = set()

    def block_names(block: Any) -> None:
        if block is None:
            return
        if block.kind == sk.IfGenerate:
            block_names(block.block)
            clause = block.elseClause
            block_names(None if clause is None else clause.clause)
        elif block.kind == sk.LoopGenerate:
            block_names(block.block)
        elif block.kind == sk.GenerateBlock:
            if block.beginName is not None:
                names.add(block.beginName.name.valueText)
            elif block.label is not None:
                names.add(block.label.name.valueText)

    def members(items: Any) -> None:
        for mem in items:
            if mem.kind == sk.TypedefDeclaration:
                names.add(mem.name.valueText)
            elif mem.kind in (sk.FunctionDeclaration, sk.TaskDeclaration):
                last = mem.prototype.name.getLastToken()
                names.add(last.valueText)
            elif mem.kind == sk.GenvarDeclaration:
                names.update(
                    t.valueText
                    for ident in _items(mem.identifiers)
                    for t in _tokens_of(ident)
                    if t.kind == tk.Identifier
                )
            elif mem.kind in (sk.IfGenerate, sk.LoopGenerate):
                block_names(mem)
            elif mem.kind == sk.GenerateRegion:
                members(mem.members)

    members(syn.members)
    return frozenset(names)


def _disabled_names(syn: Any) -> dict[str, str]:
    """Identifiers in text a conditional directive disables inside `syn`, with the directive."""
    out: dict[str, str] = {}
    ident = pyslang.parsing.TokenKind.Identifier

    first = True

    def scan(n: Any) -> None:
        nonlocal first
        if not isinstance(n, pyslang.parsing.Token):
            return
        if first:  # the trivia of the first token lies before the module
            first = False
            return
        for t in n.trivia:
            if t.kind != pyslang.parsing.TriviaKind.Directive:
                continue
            node = t.syntax()
            disabled = getattr(node, "disabledTokens", None)
            if node is None or not disabled:
                continue
            kind = _DIRECTIVES.get(str(node.kind).rsplit(".", 1)[-1], "a conditional directive")
            expr = getattr(node, "expr", None)
            label = kind if expr is None else f"{kind} {str(expr).strip()}"
            for tok in disabled:
                if tok.kind == ident:
                    # valueText, not rawText: IEEE 1800 says an escaped identifier of simple-identifier characters
                    # IS that simple identifier, so '\hidden ' must key as 'hidden'.
                    out.setdefault(tok.valueText, label)

    syn.visit(scan)
    return out


def _generate_names(syn: Any) -> tuple[frozenset[str], frozenset[str]]:
    """Names declared, and base names assigned, inside the generate constructs of a module."""
    sk = pyslang.syntax.SyntaxKind
    decls: set[str] = set()
    assigned: set[str] = set()

    def scan(n: Any) -> None:
        if isinstance(n, pyslang.parsing.Token):
            return
        if n.kind in (sk.DataDeclaration, sk.NetDeclaration):
            decls.update(d.name.valueText for d in _items(n.declarators))
        elif n.kind == sk.ContinuousAssign:
            for a in _items(n.assignments):
                if a.kind == sk.AssignmentExpression:
                    assigned.update(driven_bases(_clean_text(a.left)))

    for mem in syn.members:
        if mem.kind in (sk.IfGenerate, sk.LoopGenerate, sk.CaseGenerate, sk.GenerateRegion):
            mem.visit(scan)
    return frozenset(decls), frozenset(assigned)


def _has_macro_tokens(node: Any, sm: Any) -> bool:
    found = False

    def scan(n: Any) -> None:
        nonlocal found
        if not found and isinstance(n, pyslang.parsing.Token) and sm.isMacroLoc(n.location):
            found = True

    node.visit(scan)
    return found


def _header_has_macros(hdr: Any, sm: Any) -> bool:
    """Macro tokens anywhere from the module name through the ";"."""
    found = False

    def scan(n: Any) -> None:
        nonlocal found
        if not found and isinstance(n, pyslang.parsing.Token) and sm.isMacroLoc(n.location):
            found = True

    for part in (hdr.name, hdr.imports, hdr.parameters, hdr.ports, hdr.semi):
        if part is None:
            continue
        if isinstance(part, pyslang.parsing.Token):
            scan(part)
        elif hasattr(part, "visit"):
            part.visit(scan)
        else:
            for item in part:
                item.visit(scan)
    return found


def _port_type_text(dtype: str) -> str:
    """`logic` plus the packed dimensions for an implicit or net-typed ANSI port header."""
    toks = tokens(dtype)
    if not toks or toks[0] in ("[", "signed", "unsigned"):
        return ("logic " + dtype).strip()
    return dtype


def _param_type_text(decl: Any) -> str:
    typ = getattr(decl, "type", None)
    return "" if typ is None else _clean_text(typ)


def _param_kind(decl: Any) -> str:
    if decl.keyword.kind == pyslang.parsing.TokenKind.LocalParamKeyword:
        return "local"
    return "type" if decl.kind == pyslang.syntax.SyntaxKind.TypeParameterDeclaration else "value"


def _connection(c: Any, sk: Any) -> ConnInfo:
    if c.kind == sk.EmptyPortConnection:
        p = c.getFirstToken().location.offset
        return ConnInfo(None, "", p, p)
    if c.kind == sk.NamedPortConnection:
        port = c.name.valueText
        if not c.openParen:  # `.name`: an empty token
            expr = port
        else:
            expr = "" if c.expr is None else _text(c.expr)
            return ConnInfo(
                port,
                expr,
                _first_token(c).location.offset,
                c.getLastToken().range.end.offset,
                c.openParen.location.offset,
            )
    elif c.kind == sk.WildcardPortConnection:
        port, expr = "*", ""
    else:  # OrderedPortConnection
        port, expr = None, "" if c.expr is None else _text(c.expr)
    return ConnInfo(port, expr, _first_token(c).location.offset, c.getLastToken().range.end.offset)


class _ModelOps(_DesignHost):
    """`modules`, `interfaces`, `module` and the model they are built from."""

    # --- source model --------------------------------------------------------

    def modules(self) -> list[str]:
        dk = pyslang.ast.DefinitionKind
        kinds = (dk.Module, dk.Interface, dk.Program)
        return sorted({d.name for d in self._comp.getDefinitions() if getattr(d, "definitionKind", None) in kinds})

    def interfaces(self) -> frozenset[str]:
        """Names of the interface definitions."""
        dk = pyslang.ast.DefinitionKind
        return frozenset(
            d.name for d in self._comp.getDefinitions() if getattr(d, "definitionKind", None) == dk.Interface
        )

    def interface_ports(self, name: str) -> frozenset[str]:
        """Names of the ports of module `name` that the compiler resolved to an interface (with or without a modport).

        The module need not be instantiated: it is elaborated as the top of a second compilation of the same
        syntax trees. Raises AdapterError when it cannot be elaborated.
        """
        if name in self._ifc_ports:
            return self._ifc_ports[name]
        if name == _PROBE:
            raise AdapterError(f"module {name!r}: the name is taken by the tool, rename the module")
        info = self.module(name)
        # A module with a parameter that has no default cannot be a top. A wrapper instantiates it with a legal
        # value for each such parameter (`1`, or `logic` for a type): the kind of a port does not depend on the value.
        # A value that fails a check of the body only adds a diagnostic of the probe, which nobody reads.
        bs = "\\"  # an escaped identifier takes any name
        given = ", ".join(
            f".{bs}{p.name} ({'logic' if info.param_kinds.get(p.name) == 'type' else '1'})"
            for p in info.params
            if p.default is None and info.param_kinds.get(p.name) != "local"
        )
        text = f"module {_PROBE};\n  {bs}{name} #({given}) __p ();\nendmodule\n"
        # An option bag of its own: the shared one is not touched (restoring its parameter overrides crashes pyslang).
        driver = pyslang.driver.Driver()
        driver.addStandardArgs()
        if not driver.parseCommandLine("slang -", pyslang.driver.CommandLineOptions()) or not driver.processOptions():
            raise AdapterError("slang rejected its default options")
        bag = driver.createOptionBag()
        opts = bag.compilationOptions
        opts.topModules = {_PROBE}
        bag.compilationOptions = opts
        comp = pyslang.ast.Compilation(bag)
        for tree in self._trees.values():
            comp.addSyntaxTree(tree)
        comp.addSyntaxTree(
            pyslang.syntax.SyntaxTree.fromText(text, self._sm, "probe.sv", f"__hdlvet_probe_{uuid.uuid4().hex}.sv")
        )
        root = comp.getRoot()
        found: frozenset[str] | None = None
        inst = None
        if root.topInstances:
            inst = root.topInstances[0].body.find("__p")
            if inst is not None:
                sym = pyslang.ast.SymbolKind
                found = frozenset(p.name for p in inst.body.portList if p.kind == sym.InterfacePort)
        del inst, root, comp  # the symbols go before their compilation
        if found is None:  # an answer that is not known is not "no interface port"
            raise AdapterError(f"module {name!r}: cannot be elaborated to read its ports")
        self._ifc_ports[name] = found
        return found

    def module(self, name: str) -> ModuleInfo:
        info = self._modules.get(name)
        if info is None:
            info = self._modules[name] = self._module_info(name)
        return info

    def _module_info(self, name: str) -> ModuleInfo:
        sk = pyslang.syntax.SyntaxKind
        found = self._comp.tryGetDefinition(name, self._comp.getRoot()).definition
        if found is None or found.syntax is None:
            raise AdapterError(f"no such module: {name}")
        syn = found.syntax
        first = _first_token(syn)
        path = self._buffer_path(self._original(first.location).buffer)
        if path is None:
            raise AdapterError(f"module {name}: declaration is not in a source file")

        def check(tok: Any, what: str) -> None:
            """Refuse items that come from an `include: their offsets are not ours."""
            origin = self._buffer_path(self._original(tok.location).buffer)
            if origin is not None and origin != path:
                raise AdapterError(
                    f"module {name}: {what} comes from included file {origin}; "
                    f"edit that file or the include directly, not the module's items"
                )

        hdr = syn.header
        ports, ports_close = self._ports_of(syn, hdr, check)
        params: list[ParamInfo] = []
        param_kinds: dict[str, str] = {}
        if hdr.parameters is not None:
            for decl in _items(hdr.parameters.declarations):
                kind = _param_kind(decl)
                declarators = _items(decl.declarators)
                for d in declarators:
                    param_kinds[d.name.valueText] = kind
                    check(d.getFirstToken(), f"parameter {d.name.valueText!r}")
                    init = getattr(d, "initializer", None) or getattr(d, "assignment", None)
                    value = getattr(init, "expr", None) or getattr(init, "type", None)
                    params.append(
                        ParamInfo(
                            name=d.name.valueText,
                            default=None if value is None else _text(value),
                            start=d.getFirstToken().location.offset,
                            end=d.getLastToken().range.end.offset,
                            type_text=_param_type_text(decl),
                            decl_start=decl.getFirstToken().location.offset,
                            decl_end=decl.getLastToken().range.end.offset,
                            decl_count=len(declarators),
                        )
                    )
        instances: list[InstanceInfo] = []
        plain = self._plain(syn)
        has_macros = False if plain else _has_macro_tokens(syn, self._sm)

        def visit(node: Any) -> None:
            if node.kind == sk.HierarchyInstantiation:
                check(_first_token(node), f"instantiation of {node.type.valueText!r}")
                instances.extend(self._instances_of(node, path))

        syn.visit(visit)
        decls, assigns = self._body_items(syn, param_kinds, hdr.parameters is None, path)
        gen_decls, gen_assigned = _generate_names(syn)
        return ModuleInfo(
            name=name,
            path=path,
            start=first.location.offset,
            end=syn.endmodule.range.end.offset,
            ports_close=ports_close,
            body_end=syn.endmodule.location.offset,
            ports=ports,
            params=params,
            instances=instances,
            has_macros=has_macros,
            name_end=hdr.name.range.end.offset,
            ports_open=None
            if hdr.ports is None or hdr.ports.kind != sk.AnsiPortList
            else hdr.ports.openParen.location.offset,
            params_open=None if hdr.parameters is None else hdr.parameters.hash.location.offset,
            params_close=None if hdr.parameters is None else hdr.parameters.closeParen.location.offset,
            port_list_kind="none" if hdr.ports is None else "ansi" if hdr.ports.kind == sk.AnsiPortList else "nonansi",
            header_end=hdr.semi.range.end.offset,
            body_indent=self._body_indent(syn),
            decls=decls,
            assigns=assigns,
            header_has_macros=False if plain else _header_has_macros(hdr, self._sm),
            param_kinds=param_kinds,
            other_names=_other_names(syn),
            imports_end=hdr.imports[-1].getLastToken().range.end.offset if len(hdr.imports) else None,
            ports_directive=None if plain or hdr.ports is None else _conditional_directive(hdr.ports),
            params_directive=None if plain or hdr.parameters is None else _conditional_directive(hdr.parameters),
            disabled_names={} if plain else _disabled_names(syn),
            generate_decls=gen_decls,
            generate_assigned=gen_assigned,
        )

    def _body_indent(self, syn: Any) -> str:
        """Indent of the line of the first body item (two spaces for an empty body)."""
        members = list(syn.members)
        if not members:
            return "  "
        loc = self._original(members[0].getFirstToken().location)
        text = str(self._sm.getSourceText(loc.buffer)).encode()
        off = loc.offset
        start = max(text.rfind(b"\n", 0, off), text.rfind(b"\r", 0, off)) + 1
        line = text[start:off]
        return line[: len(line) - len(line.lstrip(b" \t"))].decode()

    def _stmt_range(self, node: Any, path: Path) -> tuple[int, int]:
        """Byte range of a whole statement in `path`, or (-1, -1) when it is not plain text of that file."""
        first, last = node.getFirstToken(), node.getLastToken()
        if not first or not last:
            return -1, -1
        a, b = first.location, last.location
        if self._sm.isMacroLoc(a) or self._sm.isMacroLoc(b) or a.buffer != b.buffer:
            return -1, -1
        if self._buffer_path(a.buffer) != path:
            return -1, -1
        return a.offset, last.range.end.offset

    def _body_items(
        self, syn: Any, param_kinds: dict[str, str], body_params_overridable: bool, path: Path
    ) -> tuple[list[DeclInfo], list[AssignInfo]]:
        sk = pyslang.syntax.SyntaxKind
        decls: list[DeclInfo] = []
        assigns: list[AssignInfo] = []
        for mem in syn.members:
            if mem.kind in (sk.DataDeclaration, sk.NetDeclaration):
                is_net = mem.kind == sk.NetDeclaration
                base = _clean_text(mem.type)
                if is_net:
                    base = (mem.netType.valueText + " " + base).strip()
                s0, s1 = self._stmt_range(mem, path)
                names = _items(mem.declarators)
                for d in names:
                    dims = " ".join(_clean_text(x) for x in d.dimensions)
                    decls.append(
                        DeclInfo(
                            d.name.valueText,
                            "net" if is_net else "var",
                            (base + " " + dims).strip(),
                            d.getFirstToken().location.offset,
                            d.getLastToken().range.end.offset,
                            s0,
                            s1,
                            len(names),
                        )
                    )
            elif mem.kind == sk.ContinuousAssign:
                s0, s1 = self._stmt_range(mem, path)
                items = _items(mem.assignments)
                for a in items:
                    if a.kind != sk.AssignmentExpression:
                        continue
                    assigns.append(
                        AssignInfo(
                            _clean_text(a.left),
                            _clean_text(a.right),
                            a.getFirstToken().location.offset,
                            a.getLastToken().range.end.offset,
                            s0,
                            s1,
                            len(items),
                        )
                    )
            elif mem.kind == sk.ParameterDeclarationStatement:
                decl = mem.parameter
                kind = _param_kind(decl)
                if kind != "local" and not body_params_overridable:
                    kind = "local"
                for d in _items(decl.declarators):
                    param_kinds.setdefault(d.name.valueText, kind)
        return decls, assigns

    @staticmethod
    def _ports_of(syn: Any, hdr: Any, check: Any) -> tuple[list[PortInfo], int | None]:
        sk = pyslang.syntax.SyntaxKind
        tk = pyslang.parsing.TokenKind
        dirs: dict[Any, str] = {
            tk.InputKeyword: "input",
            tk.OutputKeyword: "output",
            tk.InOutKeyword: "inout",
            tk.RefKeyword: "ref",
        }
        ports: list[PortInfo] = []
        plist = hdr.ports
        if plist is None:
            return ports, None
        if plist.kind == sk.AnsiPortList:
            last = "input"
            last_type = ""
            for p in _items(plist.ports):
                header = getattr(p, "header", None)
                tok = getattr(header if header is not None else p, "direction", None)
                if header is not None and header.kind != sk.InterfacePortHeader:
                    dtype = _clean_text(header.dataType)
                    if (tok is not None and tok.kind in dirs) or dtype or getattr(header, "netType", None):
                        last_type = _port_type_text(dtype)
                    own_type = last_type
                else:
                    own_type = ""
                dims = getattr(getattr(p, "declarator", None), "dimensions", None)
                if header is not None and header.kind == sk.InterfacePortHeader:
                    direction = "interface"
                elif tok is not None and tok.kind in dirs:
                    direction = dirs[tok.kind]
                else:
                    direction = last  # `output logic a, b`: b shares a's declaration
                last = direction
                ident = p.declarator.name if hasattr(p, "declarator") else p.name
                start = _first_token(p)
                check(start, f"port {ident.valueText!r}")
                ports.append(
                    PortInfo(
                        ident.valueText,
                        direction,
                        start.location.offset,
                        p.getLastToken().range.end.offset,
                        own_type,
                        dims is not None and len(dims) > 0,
                    )
                )
            check(plist.closeParen, "the port list")
            return ports, plist.closeParen.location.offset
        declared: dict[str, str] = {}
        for mem in syn.members:
            if mem.kind == sk.PortDeclaration:
                tok = getattr(mem.header, "direction", None)
                direction = (
                    "interface"
                    if mem.header.kind == sk.InterfacePortHeader
                    else dirs.get(getattr(tok, "kind", None), "inout")
                )
                for d in _items(mem.declarators):
                    declared[d.name.valueText] = direction
        for p in _items(plist.ports):
            if p.kind == sk.ExplicitNonAnsiPort:
                ident = p.name
                # `.c(cc)`: the direction is declared for the inner name.
                inner = None if p.expr is None or p.expr.kind != sk.PortReference else p.expr.name
                direction = "inout" if inner is None else declared.get(inner.valueText, "inout")
            else:
                ident = p.getFirstToken()
                direction = declared.get(ident.valueText, "inout")
            check(p.getFirstToken(), f"port {ident.valueText!r}")
            ports.append(
                PortInfo(
                    ident.valueText,
                    direction,
                    p.getFirstToken().location.offset,
                    p.getLastToken().range.end.offset,
                )
            )
        return ports, None

    def _instances_of(self, node: Any, mod_path: Path) -> list[InstanceInfo]:
        sk = pyslang.syntax.SyntaxKind
        if node.type.valueText in self._udp_names():  # a user-defined primitive is not a module
            return []
        plain = self._plain(node)
        from_macro = False if plain else _has_macro_tokens(node, self._sm)
        module = node.type.valueText
        # An instantiation whose instance has no name (text an edit glued together) is not an instance.
        hier = [i for i in node.instances if i.kind == sk.HierarchicalInstance and i.decl is not None]
        try:
            sites: list[InstanceSite | None] = list(self._sites_of(node, plain))
        except AdapterError:
            sites = [None] * len(hier)
        overrides: list[ParamOverride] = []
        params_close: int | None = None
        params_open: int | None = None
        if node.parameters is not None:
            params_close = node.parameters.closeParen.location.offset
            params_open = node.parameters.hash.location.offset
            for a in _items(node.parameters.parameters):
                expr = getattr(a, "expr", None)
                overrides.append(
                    ParamOverride(
                        name=a.name.valueText if a.kind == sk.NamedParamAssignment else None,
                        value="" if expr is None else _text(expr),
                        start=a.getFirstToken().location.offset,
                        end=a.getLastToken().range.end.offset,
                        open=a.openParen.location.offset if a.kind == sk.NamedParamAssignment and a.openParen else None,
                    )
                )
        out: list[InstanceInfo] = []
        first = _first_token(node)
        in_generate = _in_generate(node)
        for hi, site in zip(hier, sites, strict=True):
            out.append(
                InstanceInfo(
                    name=hi.decl.name.valueText,
                    module=module,
                    path=site.path if site is not None else mod_path,
                    from_macro=from_macro or site is None,
                    stmt_start=site.start if site is not None else first.location.offset,
                    stmt_end=site.end if site is not None else node.getLastToken().range.end.offset,
                    module_start=node.type.location.offset,
                    module_end=node.type.range.end.offset,
                    start=hi.getFirstToken().location.offset,
                    end=hi.getLastToken().range.end.offset,
                    params_close=params_close,
                    ports_close=hi.closeParen.location.offset,
                    connections=[_connection(c, sk) for c in _items(hi.connections)],
                    param_overrides=list(overrides),
                    ports_open=hi.openParen.location.offset,
                    params_open=params_open,
                    in_generate=in_generate,
                    is_array=len(hi.decl.dimensions) > 0,
                    conn_directive=None if plain else _conditional_directive(hi, skip_first=False),
                    override_directive=None
                    if plain or node.parameters is None
                    else _conditional_directive(node.parameters),
                )
            )
        return out
