def apply(vet):
    periph = vet.module("periph_wrap")
    with vet.group():
        periph.remove_instance("u_timer1")
        periph.remove_port("count1_o")
