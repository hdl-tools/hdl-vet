def apply(vet):
    vet.punch_port("bypass_en_i", "input", to="soc_top.u_rst_sync")
    vet.module("rst_sync").override_net("rst_no", "rst_ni", enable="bypass_en_i")
    vet.module("soc_top").override_net("irq_o", "1'b0", enable="bypass_en_i")
