def apply(vet):
    top = vet.module("soc_top")
    with vet.group():
        top.remove_assign("irq_o", was="|irq")
        top.remove_port("irq_o")
    vet.module("periph_wrap").remove_param("ID")
