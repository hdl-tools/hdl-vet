def apply(vet):
    top = vet.module("soc_top")
    top.add_port("status_o", "output", width=4)
    top.add_assign("status_o", "{irq, en_i}")
    top.instance("u_periph").disconnect("count1_o", was="count1_o")
