"""Is a Wine prefix being served by a live wineserver?

Asked before anything edits a prefix's `user.reg`. wineserver holds the registry
in memory and writes the whole file back when it saves, so an edit made to the
file while a server is up is silently lost. On 2026-10-11 a settings sync into a
new account prefix vanished this way. The guard then in use looked for the
prefix path in process command lines, which never hold it: wineserver's is the
path of its own binary, and the game's is a Windows path.

A wineserver works from `/tmp/.wine-<uid>/server-<dev>-<inode>`, named after the
device and inode of the prefix directory, so its working directory says which
prefix it serves. Standard library only.
"""

import os
import time

_PROC = "/proc"


def prefix_in_use(prefix, proc: str = _PROC) -> bool:
    """True while a wineserver is serving `prefix`.

    A wineserver that cannot be attributed to a prefix counts as serving this
    one: a wrong "free" loses the caller's edit, a wrong "in use" only delays it.
    """
    try:
        st = os.stat(prefix)
    except OSError:
        return False              # a prefix that is not there is not being served
    want = f"server-{st.st_dev:x}-{st.st_ino:x}"
    try:
        entries = os.listdir(proc)
    except OSError:
        return True
    for entry in entries:
        if not entry.isdigit():
            continue
        try:
            with open(f"{proc}/{entry}/comm", "r", encoding="utf-8") as fh:
                if fh.read().strip() != "wineserver":
                    continue
            cwd = os.readlink(f"{proc}/{entry}/cwd")
        except (FileNotFoundError, ProcessLookupError):
            continue              # the process exited between listdir and open
        except OSError:
            return True
        if os.path.basename(cwd) == want:
            return True
    return False


def wait_until_free(prefix, timeout_s: float,
                    clock=time.monotonic, sleep=time.sleep) -> bool:
    """Wait up to `timeout_s` for the prefix's wineserver to exit. True once free.

    wineserver outlives the game by a few seconds, so a caller that runs just
    after the game was stopped waits here instead of giving up on "in use".
    """
    deadline = clock() + max(0.0, timeout_s)
    while prefix_in_use(prefix):
        if clock() >= deadline:
            return False
        sleep(0.5)
    return True
