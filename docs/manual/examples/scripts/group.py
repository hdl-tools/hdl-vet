def apply(vet):
    top = vet.module("soc_top")
    with vet.group():
        top.remove_instance("u_rst_sync")
        top.instance("u_periph").connect("rst_ni", "rst_ni", was="rst_sync_n")
        top.remove_wire("rst_sync_n")
