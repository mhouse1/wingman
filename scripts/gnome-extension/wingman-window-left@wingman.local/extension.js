// Wingman Window Left — ADR 155 (supersedes ADR 057).
//
// Opens Wingman's game window at the top-left of its monitor's work area:
//   - the nested display's rootful Xwayland window, titled "Xwayland on :3"
//     with app_id org.freedesktop.Xwayland: the window on the operator's
//     desktop since ADR 099;
//   - the game's own window (WM_CLASS steam_app_0) when it runs on the session
//     display (NESTED=0).
//
// Installed and kept current by scripts/upgrade-linux.sh. GNOME Shell loads
// extension changes only at login on Wayland, so log out and in once after an
// update.

import GLib from 'gi://GLib';
import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';

const NESTED_APP_ID = 'org.freedesktop.Xwayland';
const NESTED_TITLE_PREFIX = 'Xwayland on :';
const GAME_WM_CLASS = 'steam_app_0';

// Mutter can apply its own initial placement after the first frame (ADR 057,
// Known Issue), so the move is re-checked a few times, briefly. Short enough
// that an operator dragging the window afterwards is left alone.
const RECHECK_INTERVAL_MS = 250;
const RECHECK_COUNT = 4;

export default class WingmanWindowLeftExtension extends Extension {
    enable() {
        this._sources = new Set();
        this._createdId = global.display.connect('window-created',
            (_display, metaWindow) => this._arm(metaWindow));
    }

    disable() {
        if (this._createdId) {
            global.display.disconnect(this._createdId);
            this._createdId = null;
        }
        for (const id of this._sources)
            GLib.source_remove(id);
        this._sources.clear();
    }

    _isTarget(metaWindow) {
        const wmClass = metaWindow.get_wm_class() ?? '';
        if (wmClass === GAME_WM_CLASS || wmClass === NESTED_APP_ID)
            return true;
        return (metaWindow.get_title() ?? '').startsWith(NESTED_TITLE_PREFIX);
    }

    _later(delayMs, fn) {
        const id = delayMs > 0
            ? GLib.timeout_add(GLib.PRIORITY_DEFAULT, delayMs, () => {
                this._sources.delete(id);
                fn();
                return GLib.SOURCE_REMOVE;
            })
            : GLib.idle_add(GLib.PRIORITY_DEFAULT_IDLE, () => {
                this._sources.delete(id);
                fn();
                return GLib.SOURCE_REMOVE;
            });
        this._sources.add(id);
    }

    // `first-frame` is a signal of the window's ACTOR, not of the MetaWindow.
    // ADR 057 connected it on the MetaWindow, so the move never ran. A Wayland
    // window's actor may not exist yet inside window-created, so wait one idle.
    // The target check also waits for the first frame: a Wayland client's title
    // and app_id are not guaranteed to be set when the window is created.
    _arm(metaWindow) {
        this._later(0, () => {
            const actor = metaWindow.get_compositor_private();
            if (!actor)
                return;
            const id = actor.connect('first-frame', () => {
                actor.disconnect(id);
                if (this._isTarget(metaWindow))
                    this._place(metaWindow, RECHECK_COUNT);
            });
        });
    }

    _place(metaWindow, rechecksLeft) {
        if (!metaWindow.get_compositor_private())
            return; // closed before a recheck came round
        const area = metaWindow.get_work_area_current_monitor();
        const frame = metaWindow.get_frame_rect();
        if (frame.x !== area.x || frame.y !== area.y) {
            metaWindow.move_frame(true, area.x, area.y);
            console.log(`wingman-window-left: moved "${metaWindow.get_title()}" ` +
                `from ${frame.x},${frame.y} to ${area.x},${area.y}`);
        }
        if (rechecksLeft > 0)
            this._later(RECHECK_INTERVAL_MS, () => this._place(metaWindow, rechecksLeft - 1));
    }
}
