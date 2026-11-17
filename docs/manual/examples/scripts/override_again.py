def apply(vet):
    top = vet.module("soc_top")
    top.override_net("rst_sync_n", "rst_ni", enable="bypass_en_i")  # override.py has done it: skipped
