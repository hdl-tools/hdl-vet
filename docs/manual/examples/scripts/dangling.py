def apply(vet):
    top = vet.module("soc_top")
    top.instance("u_periph").disconnect("irq_o", was="irq")
