// Wingman Window Left — ADR 155 (supersedes ADR 057).
//
// Opens Wingman's game window at the top-left of its monitor's work area:
//   - the nested display's rootful Xwayland window, titled "Xwayland on :3"
//     with app_id org.freedesktop.Xwayland: the window on the operator's
//     desktop since ADR 099;
//   - the game's own window (WM_CLASS steam_app_0) when it runs on the session
//     display (NESTED=0).
//
// It also logs when that window stops being shown (HLDD 001, 2026-10-03): the
// game's picture drops to one frame a second for minutes at a time, and an
// exact one a second is what Xwayland does for a window the desktop is not
// drawing. Nothing outside the Shell can see whether the window is covered,
// minimised or on another workspace, so the Shell says so in the journal:
//
//   wingman-window-left: shown=no covered=100% minimised=no workspace=other focus=no above=code
//
// One line on each change, none while nothing changes. Read it with
// `journalctl --user -g wingman-window-left`.
//
// And it keeps the window drawn at full rate while it is hidden (ADR 099, V3):
// see _keepShown below.
//
// Installed and kept current by scripts/upgrade-linux.sh. GNOME Shell loads
// extension changes only at login on Wayland, so log out and in once after an
// update.

import Clutter from 'gi://Clutter';
import GLib from 'gi://GLib';
import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';

const NESTED_APP_ID = 'org.freedesktop.Xwayland';
const NESTED_TITLE_PREFIX = 'Xwayland on :';
const GAME_WM_CLASS = 'steam_app_0';

// Mutter can apply its own initial placement after the first frame (ADR 057,
// Known Issue), so the move is re-checked a few times, briefly. Short enough
// that an operator dragging the window afterwards is left alone.
const RECHECK_INTERVAL_MS = 250;
const RECHECK_COUNT = 4;

// How much of the window other windows hide is sampled on a grid, which is
// enough to tell "covered" from "peeking out" and costs nothing.
const COVER_GRID = 12;

export default class WingmanWindowLeftExtension extends Extension {
    enable() {
        this._sources = new Set();
        this._watched = new Map();      // target window -> its signal ids
        this._lastReport = new Map();   // target window -> last line logged
        this._copies = new Map();       // target window -> its one-pixel copy
        this._reportQueued = false;
        this._createdId = global.display.connect('window-created',
            (_display, metaWindow) => this._arm(metaWindow));
        this._restackedId = global.display.connect('restacked',
            () => this._queueReport());
        this._focusId = global.display.connect('notify::focus-window',
            () => this._queueReport());
        this._workspaceId = global.workspace_manager.connect('active-workspace-changed',
            () => this._queueReport());
    }

    disable() {
        if (this._createdId) {
            global.display.disconnect(this._createdId);
            this._createdId = null;
        }
        for (const [obj, name] of [[global.display, '_restackedId'],
            [global.display, '_focusId'], [global.workspace_manager, '_workspaceId']]) {
            if (this[name]) {
                obj.disconnect(this[name]);
                this[name] = null;
            }
        }
        for (const [metaWindow, ids] of this._watched) {
            for (const id of ids)
                metaWindow.disconnect(id);
        }
        this._watched.clear();
        this._lastReport.clear();
        for (const copy of this._copies.values())
            copy.destroy();
        this._copies.clear();
        for (const id of this._sources)
            GLib.source_remove(id);
        this._sources.clear();
    }

    // Follow a target window until it closes.
    _watch(metaWindow) {
        if (this._watched.has(metaWindow))
            return;
        const ids = ['notify::minimized', 'workspace-changed', 'position-changed', 'size-changed']
            .map(signal => metaWindow.connect(signal, () => this._queueReport()));
        ids.push(metaWindow.connect('unmanaged', () => {
            for (const id of ids)
                metaWindow.disconnect(id);
            this._watched.delete(metaWindow);
            this._lastReport.delete(metaWindow);
            this._copies.get(metaWindow)?.destroy();
            this._copies.delete(metaWindow);
            console.log('wingman-window-left: closed');
        }));
        this._watched.set(metaWindow, ids);
        this._keepShown(metaWindow);
        this._queueReport();
    }

    // ADR 099 V3. The desktop tells a window "your frame was shown" only while
    // it is drawing that window, and Xwayland waits a second for each frame of
    // a window that is not told. Behind another window the game fell to one
    // frame a second (measured 2026-10-04 11:27 and 12:29). Mutter makes an
    // exception for a window that has a copy of itself on the desktop, which is
    // how the overview keeps its thumbnails moving (mutter 50.1,
    // meta_surface_actor_wayland_is_view_primary: has_mapped_clones). So hold
    // one: a single pixel in the corner of the screen, see-through, taking no
    // input. It has to be on the stage and not hidden to count. It does not
    // have to be seen.
    _keepShown(metaWindow) {
        const actor = metaWindow.get_compositor_private();
        if (!actor || this._copies.has(metaWindow))
            return;
        try {
            const copy = new Clutter.Clone({
                source: actor, x: 0, y: 0, width: 1, height: 1, opacity: 0, reactive: false,
            });
            Main.layoutManager.uiGroup.add_child(copy);
            this._copies.set(metaWindow, copy);
            console.log(`wingman-window-left: holding a one-pixel copy of "${metaWindow.get_title()}" ` +
                `so it is drawn at full rate while hidden (mapped=${copy.mapped ? 'yes' : 'no'})`);
        } catch (e) {
            console.log(`wingman-window-left: could not hold a copy of the window: ${e}`);
        }
    }

    // Many signals arrive together (a restack and a focus change, say); one
    // look at the windows after they settle covers them all.
    _queueReport() {
        if (this._reportQueued || this._watched.size === 0)
            return;
        this._reportQueued = true;
        this._later(0, () => {
            this._reportQueued = false;
            for (const metaWindow of this._watched.keys()) {
                try {
                    this._report(metaWindow);
                } catch (e) {
                    console.log(`wingman-window-left: could not read the window state: ${e}`);
                }
            }
        });
    }

    _report(metaWindow) {
        const active = global.workspace_manager.get_active_workspace();
        const onActive = metaWindow.is_on_all_workspaces() || metaWindow.get_workspace() === active;
        const frame = metaWindow.get_frame_rect();
        // Windows above the target, bottom to top, that the desktop is drawing.
        const stack = global.get_window_actors().map(actor => actor.meta_window);
        const above = stack.slice(stack.indexOf(metaWindow) + 1).filter(other =>
            other && !other.minimized &&
            (other.is_on_all_workspaces() || other.get_workspace() === active));
        let covered = 0;
        const coverers = new Set();
        for (let i = 0; i < COVER_GRID; i++) {
            for (let j = 0; j < COVER_GRID; j++) {
                const x = frame.x + (i + 0.5) * frame.width / COVER_GRID;
                const y = frame.y + (j + 0.5) * frame.height / COVER_GRID;
                const over = above.find(other => {
                    const r = other.get_frame_rect();
                    return x >= r.x && x < r.x + r.width && y >= r.y && y < r.y + r.height;
                });
                if (over) {
                    covered++;
                    coverers.add(over.get_wm_class() ?? '?');
                }
            }
        }
        const pct = Math.round(100 * covered / (COVER_GRID * COVER_GRID));
        const shown = !metaWindow.minimized && onActive && pct < 100;
        const line = `shown=${shown ? 'yes' : 'no'} covered=${pct}% ` +
            `minimised=${metaWindow.minimized ? 'yes' : 'no'} ` +
            `workspace=${onActive ? 'active' : 'other'} ` +
            `focus=${global.display.focus_window === metaWindow ? 'yes' : 'no'} ` +
            `above=${[...coverers].join(',') || '-'}`;
        if (this._lastReport.get(metaWindow) === line)
            return;
        this._lastReport.set(metaWindow, line);
        console.log(`wingman-window-left: ${line}`);
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
                if (this._isTarget(metaWindow)) {
                    this._place(metaWindow, RECHECK_COUNT);
                    this._watch(metaWindow);
                }
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
