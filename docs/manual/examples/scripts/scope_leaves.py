def apply(vet):
    with vet.scope("periph_wrap"):
        vet.module("periph_wrap").add_wire("busy")
        vet.module("soc_top").add_wire("busy")
