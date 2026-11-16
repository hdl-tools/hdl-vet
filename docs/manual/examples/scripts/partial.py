def apply(vet):
    vet.module("periph_wrap").instance("u_timer1").disconnect("irq_o", was="irq_o[1]")
