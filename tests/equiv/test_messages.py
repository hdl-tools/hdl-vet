"""What a message of the equivalence engine shows of the host's paths.

The engine gives the sandboxed Yosys a mount named `/hostfs_<top>` for each top-level
folder it needs. Yosys then names that mount in its diagnostics, so the engine turns it
back into the host path before the message reaches the user.

Nothing here needs Yosys: `_MOUNT_IN_TEXT` is a regular expression and
`hdl_vet.equiv` imports no Yosys at module import time.
"""

from __future__ import annotations

from hdl_vet.equiv import _MOUNT_IN_TEXT


def test_a_mount_in_a_message_reads_as_the_host_path():
    """The name of the top-level folder is kept.

    It used to be dropped, and worse: the replacement held a literal control byte, so
    `/hostfs_private/var/x.sv` reached the user as `/` then that byte then `/var/x.sv`.
    A terminal shows the byte as nothing, which is why it went unnoticed.
    """
    said = "/hostfs_private/var/rtl/top.sv:3: ERROR: syntax error"
    assert _MOUNT_IN_TEXT.sub(r"/\1", said) == "/private/var/rtl/top.sv:3: ERROR: syntax error"


def test_the_substitution_the_engine_uses_has_no_control_byte():
    """The engine's own source, so a byte like that cannot creep back in unseen."""
    from pathlib import Path

    import hdl_vet.equiv

    text = Path(hdl_vet.equiv.__file__).read_text(encoding="utf-8")
    offenders = [(n + 1, repr(line)) for n, line in enumerate(text.splitlines()) if any(ord(c) < 9 for c in line)]
    assert offenders == []


def test_every_top_level_folder_name_is_kept():
    for top in ("private", "Users", "tmp", "Volumes"):
        said = f"/hostfs_{top}/a/b.sv: note"
        assert _MOUNT_IN_TEXT.sub(r"/\1", said) == f"/{top}/a/b.sv: note"
