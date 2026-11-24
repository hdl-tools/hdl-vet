def apply(vet):
    top = vet.module("soc_top")
    path = top.path
    text = vet.text(path)

    # Tie test_i to 1'b1 instead of 1'b0. The sign-off must see exactly this change.
    # It is a change of logic, so the splice has a waiver.
    start = text.index(b"1'b0")
    vet.splice(path, start, start + 4, b"1'b1", old=b"1'b0", waiver="covered by tb_rst")
    vet.expect("connection-changed", path="soc_top.u_rst_sync", name="test_i")

    # Three synchroniser stages instead of two. The sign-off may see this change.
    start = text.index(b"STAGES(2)") + len(b"STAGES(")
    vet.splice(path, start, start + 1, b"3", old=b"2", waiver="covered by tb_rst")
    vet.permit("param-changed", path="soc_top.u_rst_sync", name="STAGES")
