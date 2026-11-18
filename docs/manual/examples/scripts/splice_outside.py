def apply(vet):
    timer = vet.module("timer", instances=2)
    path = timer.path
    start = vet.text(path).index(b"module timer")
    vet.splice(path, start, start, b"`timescale 1ns/1ps\n")
