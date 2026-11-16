def apply(vet):
    top = vet.module("soc_top")
    path = top.path
    text = vet.text(path)
    start = text.index(b"module soc_top")
    vet.splice(path, start, start, b'`include "../../cells/tech_rst_sync.sv"\n')
