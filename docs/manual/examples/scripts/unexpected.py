def apply(vet):
    top = vet.module("soc_top")
    path = top.path
    start = vet.text(path).index(b"1'b0")
    vet.splice(path, start, start + 4, b"1'b1", old=b"1'b0")
