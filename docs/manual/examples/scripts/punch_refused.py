from hdl_vet.ops import VetError


def apply(vet):
    to = "soc_top.u_periph.u_timer0"
    for direction, extra in (("input", {}), ("output", {"all_instances": True})):
        try:
            vet.punch_port("probe", direction, to=to, **extra)
        except VetError as error:
            print(error)
