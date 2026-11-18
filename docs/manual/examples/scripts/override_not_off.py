def apply(vet):
    vet.module("rst_sync").add_port("bypass_en_i", "input")
    vet.module("soc_top").instance("u_rst_sync").connect("bypass_en_i", "1'b1")
    vet.module("rst_sync").override_net("rst_no", "rst_ni", enable="bypass_en_i")
