def apply(vet):
    top = vet.module("soc_top")
    top.add_port("bypass_en_i", "input")
    top.override_net("rst_sync_n", "rst_ni", enable="bypass_en_i")
    path = top.path  # this loads the queued edits, so that the text shows them
    old = b"bypass_en_i ? rst_ni : rst_sync_n_pre"
    start = vet.text(path).index(old)
    vet.splice(path, start, start + len(old), b"bypass_en_i ? rst_sync_n_pre : rst_ni", old=old)
