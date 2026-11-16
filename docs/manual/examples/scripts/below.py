def apply(vet):
    periph = vet.module("periph_wrap")
    path = periph.path
    text = vet.text(path)

    # Give the first timer the other enable bit.
    start = text.index(b"en_i[0]")
    vet.splice(path, start, start + 7, b"en_i[1]", old=b"en_i[0]")

    # This changes u_periph.u_timer0, below u_periph. Allow every change below u_periph.
    vet.module("soc_top").instance("u_periph").allow_changes_below()
