"""Present timed replay screenshots on desktop for ADR045 live lane.

Displays each scheduled screenshot inside the configured capture region so
Wingman's normal monitor capture path can OCR real on-screen pixels.
"""

from __future__ import annotations

import argparse
import ctypes
from pathlib import Path
import tkinter as tk

import yaml
from mss import mss


def _load_steps(path_config: Path, path_name: str) -> list[dict]:
    payload = yaml.safe_load(path_config.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"Invalid path config format: {path_config}")
    steps = payload.get(path_name)
    if not isinstance(steps, list) or not steps:
        raise ValueError(f"Path '{path_name}' missing or empty in {path_config}")
    return sorted(steps, key=lambda s: float(s.get("injection_time_s", 0.0)))


def _region_from_config(config_path: Path) -> tuple[int, int, int, int]:
    cfg = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    region_cfg = cfg["region"]
    monitor_index = int(cfg.get("monitor", 1))

    with mss() as sct:
        monitors = sct.monitors
        if monitor_index < 1 or monitor_index >= len(monitors):
            raise ValueError(f"Monitor index {monitor_index} out of range")
        mon = monitors[monitor_index]

    left = int(mon["left"] + int(region_cfg["left"]))
    top = int(mon["top"] + int(region_cfg["top"]))
    width = int(region_cfg["width"])
    height = int(region_cfg["height"])
    return left, top, width, height


def main() -> int:
    parser = argparse.ArgumentParser(description="Present timed screenshots on screen")
    parser.add_argument("--config", default="wingman/config.yaml", help="Wingman config path")
    parser.add_argument("--path-config", required=True, help="Path YAML containing scheduled steps")
    parser.add_argument("--path", required=True, help="Path key to present")
    parser.add_argument(
        "--screenshot-dir",
        default="test_screenshots/integration_test",
        help="Directory containing source screenshots",
    )
    parser.add_argument("--grace-s", type=float, default=8.0, help="Hold final frame duration before exit")
    args = parser.parse_args()

    config_path = Path(args.config)
    path_config = Path(args.path_config)
    screenshot_dir = Path(args.screenshot_dir)

    steps = _load_steps(path_config, args.path)
    left, top, width, height = _region_from_config(config_path)

    # Align tkinter coordinates to physical pixels so window placement matches
    # mss capture coordinates when Windows display scaling is enabled.
    try:
        if hasattr(ctypes, "windll"):
            try:
                ctypes.windll.shcore.SetProcessDpiAwareness(2)
            except Exception:
                ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

    root = tk.Tk()
    root.title("Wingman ADR045 Live Presenter")
    root.overrideredirect(True)
    root.geometry(f"{width}x{height}+{left}+{top}")
    root.resizable(False, False)
    root.attributes("-topmost", True)
    root.lift()
    try:
        root.tk.call("tk", "scaling", 1.0)
    except Exception:
        pass

    print(
        f"Presenter window geometry: left={left} top={top} width={width} height={height}",
        flush=True,
    )

    label = tk.Label(root)
    label.pack(fill="both", expand=True)

    black = tk.PhotoImage(width=width, height=height)
    label.configure(image=black)
    label.image = black

    cached_images: dict[str, tk.PhotoImage] = {}

    def _show_image(path: Path) -> None:
        photo = cached_images.get(str(path))
        if photo is None:
            if not path.exists():
                raise FileNotFoundError(f"Screenshot missing: {path}")
            photo = tk.PhotoImage(file=str(path))
            cached_images[str(path)] = photo
        label.configure(image=photo)
        label.image = photo
        print(f"Presenter displayed: {path.name}", flush=True)

    for step in steps:
        at_ms = int(max(0.0, float(step.get("injection_time_s", 0.0))) * 1000.0)
        screenshot_name = step.get("screenshot_name")
        if not screenshot_name:
            raise ValueError("Step missing screenshot_name")
        frame_path = screenshot_dir / str(screenshot_name)
        root.after(at_ms, lambda p=frame_path: _show_image(p))

    last_step_s = float(steps[-1].get("injection_time_s", 0.0))
    close_ms = int((last_step_s + max(0.0, float(args.grace_s))) * 1000.0)
    root.after(close_ms, root.destroy)
    print(f"Presenter schedule loaded: {len(steps)} steps, grace={args.grace_s}s", flush=True)

    root.mainloop()
    print("Presenter finished", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
