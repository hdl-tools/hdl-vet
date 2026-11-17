def apply(vet):
    top = vet.module("soc_top")
    top.override_net("rst_sync_n", "rst_ni", enable="en_i")  # en_i has two bits
