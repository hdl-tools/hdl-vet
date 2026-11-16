def apply(vet):
    vet.expect("connection-changed", path="soc_top.u_rst_sync", name="test_i")
