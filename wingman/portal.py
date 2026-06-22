"""XDG Desktop Portal ScreenCast session for PipeWire capture on GNOME Wayland.

Called once at Wingman startup. Uses a restore token to skip the GNOME
screen-share dialog on subsequent runs. The returned bus reference must be
kept alive (held by the caller) for the portal session — and therefore the
PipeWire node — to remain valid.
"""
import json
import logging
import os

from gi.repository import GLib, Gio

logger = logging.getLogger(__name__)

TOKEN_FILE = os.path.expanduser("~/.config/wingman/pw_restore_token.json")


def _load_restore_token():
    if os.path.exists(TOKEN_FILE):
        try:
            with open(TOKEN_FILE) as f:
                return json.load(f).get("restore_token")
        except Exception:
            pass
    return None


def _save_restore_token(token):
    if token:
        os.makedirs(os.path.dirname(TOKEN_FILE), exist_ok=True)
        with open(TOKEN_FILE, "w") as f:
            json.dump({"restore_token": token}, f)


def acquire_screencast_node():
    """Run the portal ScreenCast sequence and return (node_id, bus).

    On first call: shows the GNOME screen-share dialog (select monitor → Share).
    On subsequent calls: restore token skips the dialog automatically.

    The returned bus object must be kept alive by the caller. When it is
    garbage-collected the portal session closes and the PipeWire node disappears.

    Raises RuntimeError if the portal sequence fails.
    """
    restore_token = _load_restore_token()
    if restore_token:
        logger.info("portal: restore token found — dialog will be skipped")
    else:
        logger.info("portal: no restore token — GNOME screen-share dialog will appear; select your monitor and click Share")

    loop = GLib.MainLoop()
    state: dict = {}

    bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    # :1.416 → 1_416  (portal uses this as the path segment)
    sender = bus.get_unique_name().replace(":", "").replace(".", "_")

    tok = [0]

    def nt():
        tok[0] += 1
        return f"wm{tok[0]}"

    def sub(handle_path, cb):
        bus.signal_subscribe(
            None, "org.freedesktop.portal.Request", "Response",
            handle_path, None, Gio.DBusSignalFlags.NO_MATCH_RULE, cb,
        )

    def call(method, args):
        return bus.call_sync(
            "org.freedesktop.portal.Desktop",
            "/org/freedesktop/portal/desktop",
            "org.freedesktop.portal.ScreenCast",
            method, args, None, Gio.DBusCallFlags.NONE, 120_000, None,
        )

    # ── Step 1: CreateSession ────────────────────────────────────────────────
    t1 = nt()
    req1 = f"/org/freedesktop/portal/desktop/request/{sender}/{t1}"

    def on_create(conn, sndr, obj, iface, sig, params):
        resp, res = params
        if resp != 0:
            state["error"] = f"CreateSession failed (response={resp})"
            loop.quit()
            return
        session = res["session_handle"]
        logger.debug("portal: session created: %s", session)

        # ── Step 2: SelectSources ────────────────────────────────────────────
        t2 = nt()
        req2 = f"/org/freedesktop/portal/desktop/request/{sender}/{t2}"
        sel_opts = {
            "handle_token": GLib.Variant("s", t2),
            "types": GLib.Variant("u", 1),          # 1=Monitor (window type not supported by DXVK/Wine)
            "multiple": GLib.Variant("b", False),
            "cursor_mode": GLib.Variant("u", 2),    # 2=embedded cursor
            "persist_mode": GLib.Variant("u", 2),   # 2=persistent (save restore token)
        }
        if restore_token:
            sel_opts["restore_token"] = GLib.Variant("s", restore_token)

        def on_select(conn, sndr, obj, iface, sig, params):
            resp, _ = params
            if resp != 0:
                state["error"] = f"SelectSources failed (response={resp})"
                loop.quit()
                return

            # ── Step 3: Start ────────────────────────────────────────────────
            t3 = nt()
            req3 = f"/org/freedesktop/portal/desktop/request/{sender}/{t3}"

            def on_start(conn, sndr, obj, iface, sig, params):
                resp, res = params
                if resp != 0:
                    state["error"] = f"Start failed (response={resp})"
                    loop.quit()
                    return
                streams = res.get("streams", [])
                if not streams:
                    state["error"] = "Portal returned no streams"
                    loop.quit()
                    return
                node_id, _props = streams[0]
                state["node_id"] = int(node_id)
                state["restore_token"] = res.get("restore_token")
                logger.debug("portal: node_id=%d", state["node_id"])
                loop.quit()

            sub(req3, on_start)
            try:
                call("Start", GLib.Variant("(osa{sv})", (
                    session, "", {"handle_token": GLib.Variant("s", t3)},
                )))
            except Exception as exc:
                state["error"] = f"Start raised: {exc}"
                loop.quit()

        sub(req2, on_select)
        try:
            call("SelectSources", GLib.Variant("(oa{sv})", (session, sel_opts)))
        except Exception as exc:
            state["error"] = f"SelectSources raised: {exc}"
            loop.quit()

    sub(req1, on_create)
    call("CreateSession", GLib.Variant("(a{sv})", ({
        "handle_token": GLib.Variant("s", t1),
        "session_handle_token": GLib.Variant("s", nt()),
    },)))

    loop.run()

    if "error" in state:
        raise RuntimeError(f"Portal ScreenCast failed: {state['error']}")

    _save_restore_token(state.get("restore_token"))
    return state["node_id"], bus


if __name__ == "__main__":
    import sys
    print("Requesting GNOME window-share permission via XDG Desktop Portal…")
    print("A dialog will appear — select the MetalStorm window and click Share.")
    try:
        node_id, _bus = acquire_screencast_node()
        print(f"\nSetup complete. PipeWire node_id={node_id}")
        print(f"Restore token saved to {TOKEN_FILE}")
        print("You will not see this dialog again unless the token is deleted.")
    except RuntimeError as e:
        print(f"\nSetup failed: {e}", file=sys.stderr)
        sys.exit(1)
