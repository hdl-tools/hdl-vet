def apply(vet):
    top = vet.module("soc_top")
    top.add_wire("tmp")
    top.remove_wire("tmp")
