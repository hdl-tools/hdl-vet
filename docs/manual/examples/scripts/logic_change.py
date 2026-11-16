def apply(vet):
    timer = vet.module("timer", instances=2)
    path = timer.path
    old = b"count_o + 1'b1"
    start = vet.text(path).index(old)
    vet.splice(path, start, start + len(old), b"count_o + 2'd2", old=old)
