def apply(vet):
    with vet.scope("periph_wrap"):
        timer = vet.module("timer", instances=2)  # both timers change
        timer.add_wire("armed")
        timer.add_assign("armed", "en_i & ~irq_o")
        vet.punch_port("dbg_i", "input", to="periph_wrap.u_timer0", all_instances=True)

    top = vet.module("soc_top")
    top.add_port("dbg_i", "input")
    top.instance("u_periph").connect("dbg_i", "dbg_i")
