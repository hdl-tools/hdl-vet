def apply(vet):
    top = vet.module("soc_top")
    top.add_port("bypass_en_i", "input")
    top.override_net("irq_o", "1'b0", enable="bypass_en_i", waiver="the interrupt is masked in test mode")
