def apply(vet):
    top = vet.module("soc_top")
    path = top.path
    start = vet.text(path).index(b"soc_top")
    vet.splice(path, start, start + 7, b"soc_top_v2", old=b"soc_top")
