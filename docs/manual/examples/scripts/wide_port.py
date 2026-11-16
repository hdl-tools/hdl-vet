def apply(vet):
    top = vet.module("soc_top")
    top.instance("u_periph").connect("en_i", "en_i[0]", was="en_i")
