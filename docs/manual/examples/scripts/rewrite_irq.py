def apply(vet):
    vet.module("soc_top").replace_expr("irq_o", "irq[0] | irq[1]", was="|irq")
