def apply(vet):
    top = vet.module("soc_top")
    path = top.path
    text = vet.text(path)
    start = text.index(b"module soc_top")
    include = b'`include "../../cells/tech_rst_sync.sv"\n'
    vet.splice(path, start, start, include, waiver="the cell is read, nothing uses it")
