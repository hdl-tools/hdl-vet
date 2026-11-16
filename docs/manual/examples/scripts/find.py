def apply(vet):
    top = vet.module("soc_top")
    print("soc_top paths:", top.paths)
    print("soc_top ports:", [port.name for port in top.info.ports])

    reset = top.instance("u_rst_sync")
    print("u_rst_sync is a", reset.info.module, "at", reset.paths)

    timer = vet.module("timer", instances=2)
    print("timer paths:", timer.paths)

    periph = vet.module("periph_wrap")
    for inst in periph.instances(module="timer", name="u_timer*", count=2):
        print(inst.target, "->", inst.paths)
