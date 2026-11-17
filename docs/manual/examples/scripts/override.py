def apply(vet):
    top = vet.module("soc_top")
    top.add_port("bypass_en_i", "input")
    top.override_net("rst_sync_n", "rst_ni", enable="bypass_en_i")
