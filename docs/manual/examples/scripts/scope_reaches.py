def apply(vet):
    with vet.scope("periph_wrap"):
        periph = vet.module("periph_wrap")
        periph.add_wire("peek")
        periph.add_assign("peek", "soc_top.u_rst_sync.rst_no")
