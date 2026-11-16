def apply(vet):
    vet.punch_port("sync_rst_o", "output", to="soc_top.u_rst_sync", signal="rst_no")
    vet.punch_port("tick_i", "input", to="soc_top.u_periph.u_timer0", all_instances=True)
