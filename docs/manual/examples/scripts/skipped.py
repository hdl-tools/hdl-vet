def apply(vet):
    top = vet.module("soc_top")
    top.add_port("test_en_i", "input")  # first.py has added it: skipped
    top.add_wire("spare", 4)  # new: applied
