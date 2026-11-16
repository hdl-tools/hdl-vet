def apply(vet):
    top = vet.module("soc_top")
    top.add_port("test_en_i", "input")
    top.instance("u_rst_sync").connect("test_i", "test_en_i", was="1'b0")
