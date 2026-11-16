def apply(vet):
    with vet.scope("rst_sync"):
        vet.module("rst_sync").add_param("HOLD", 1)

    vet.module("soc_top").instance("u_rst_sync").set_param("HOLD", 2)
