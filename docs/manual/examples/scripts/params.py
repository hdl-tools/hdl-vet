def apply(vet):
    top = vet.module("soc_top")
    top.add_param("DEPTH", 2)
    top.instance("u_rst_sync").set_param("STAGES", "DEPTH", was=2)
