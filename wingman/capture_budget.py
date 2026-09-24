"""Cross-session disk budget for capture features.

Every screenshot feature caps itself per SESSION (crash_capture's
max_per_session, the HUD archive's max_files, ...), but those counters reset on
every launch and nothing ever deleted an old file or looked at free space.
Measured 2026-09-24: the target_tracking archive alone wrote 2.6 GB across five
sessions that morning, and the capture folders plus logs/ held about 19 GB.

This module is the safety net every write site shares:

- a **free-space floor** — below `min_free_gb`, `admit()` refuses and the
  capture is skipped (a missing debug frame is recoverable; a full disk takes
  the log, the stats JSON and the game down with it);
- a **per-directory budget** — before a write, the oldest matching files are
  deleted until the directory has room for one more.

`configure()` loads `capture_budget:` from config.yaml once at startup. Until
then the module defaults below apply, so tests and standalone tools that never
call it are still bounded.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

logger = logging.getLogger(__name__)

IMAGE_PATTERNS = ("*.png", "*.jpg", "*.jpeg")

# Module defaults — deliberately looser than config.yaml's floor so a small CI
# or cloud disk does not silently skip every capture a test expects.
_min_free_bytes: int = 1 * 1024 ** 3
_default_budget: tuple[int, int] = (1000, 2048 * 1024 ** 2)
_dir_budgets: dict[Path, tuple[int, int]] = {}
# Labels already warned about hitting the floor — one WARNING per label per
# session, DEBUG after, so a full disk does not also flood the log.
_floor_warned: set[str] = set()
# Directories already pruned at INFO this session. Once a folder sits at its
# budget every capture deletes its oldest file or two; measured 2026-09-24
# 08:41-08:56 that was 58 identical INFO lines in 14 minutes (4.4% of all
# INFO). The first prune per folder (the catch-up, and the "now rolling"
# signal) stays INFO; the steady roll after it is DEBUG.
_pruned_dirs: set[Path] = set()


def _budget_from(cfg: dict, fallback: tuple[int, int]) -> tuple[int, int]:
    files = int(cfg.get("max_files", fallback[0]))
    mb = cfg.get("max_mb")
    return files, (fallback[1] if mb is None else int(float(mb) * 1024 ** 2))


def configure(config: dict) -> None:
    """Load `capture_budget:` from the full config mapping."""
    global _min_free_bytes, _default_budget
    cfg = config.get("capture_budget", {}) or {}
    _min_free_bytes = int(float(cfg.get("min_free_gb", _min_free_bytes / 1024 ** 3)) * 1024 ** 3)
    _default_budget = _budget_from(cfg.get("default", {}) or {}, _default_budget)
    _dir_budgets.clear()
    for path, budget in (cfg.get("dirs", {}) or {}).items():
        _dir_budgets[Path(path).resolve()] = _budget_from(budget or {}, _default_budget)
    _floor_warned.clear()
    _pruned_dirs.clear()
    logger.info("Capture budget: floor %.1f GB free, default %d files / %.0f MB per dir, "
                "%d dir override(s)", _min_free_bytes / 1024 ** 3, _default_budget[0],
                _default_budget[1] / 1024 ** 2, len(_dir_budgets))


def section_budget(config: dict, key: str, fallback: tuple[int, int]) -> tuple[int, int]:
    """(max_files, max_bytes) for a named `capture_budget:` subsection such as
    `rotated_logs` or `session_video`."""
    cfg = (config.get("capture_budget", {}) or {}).get(key, {}) or {}
    return _budget_from(cfg, fallback)


def _free_bytes(directory: Path) -> "int | None":
    # The target directory may not exist yet (first capture of a session);
    # disk_usage needs a real path, so measure the nearest existing ancestor.
    probe = directory.resolve()
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    try:
        return shutil.disk_usage(probe).free
    except OSError as e:
        logger.debug("Capture budget: cannot measure free space at %s: %s", probe, e)
        return None


def prune(directory, patterns=IMAGE_PATTERNS, max_files: int = 0,
          max_bytes: int = 0, reserve: int = 0) -> tuple[int, int]:
    """Delete the oldest files in `directory` matching `patterns` until at most
    `max_files - reserve` files and `max_bytes` bytes remain. 0 = unlimited.

    Non-recursive: only files directly in `directory` are counted or deleted,
    so a hand-curated subfolder is never touched. Returns (files deleted,
    bytes freed).
    """
    directory = Path(directory)
    if (max_files <= 0 and max_bytes <= 0) or not directory.is_dir():
        return 0, 0
    if isinstance(patterns, str):
        patterns = (patterns,)
    entries = []
    seen = set()
    for pattern in patterns:
        for p in directory.glob(pattern):
            if p in seen:
                continue
            seen.add(p)
            try:
                st = p.stat()
            except OSError:
                continue            # deleted by a concurrent writer — fine
            if p.is_file():
                entries.append((st.st_mtime, st.st_size, p))
    entries.sort()
    count = len(entries)
    total = sum(size for _, size, _ in entries)
    file_limit = max(max_files - reserve, 0) if max_files > 0 else None
    deleted = freed = 0
    for _, size, p in entries:
        over_files = file_limit is not None and count > file_limit
        over_bytes = max_bytes > 0 and total > max_bytes
        if not (over_files or over_bytes):
            break
        try:
            p.unlink(missing_ok=True)
        except OSError as e:
            logger.warning("Capture budget: could not delete %s: %s", p, e)
            continue
        count -= 1
        total -= size
        deleted += 1
        freed += size
    if deleted:
        key = directory.resolve()
        log = logger.debug if key in _pruned_dirs else logger.info
        _pruned_dirs.add(key)
        log("Capture budget: pruned %d oldest file(s) (%.1f MB) from %s",
            deleted, freed / 1024 ** 2, directory)
    return deleted, freed


def admit(directory, label: str, patterns=IMAGE_PATTERNS) -> bool:
    """Call right before writing one capture into `directory`.

    Returns False when the write must be skipped (free space below the floor).
    Otherwise prunes the directory's oldest `patterns` files so it stays within
    its budget after the write, and returns True. Never raises — a budget
    problem must not take down the tick loop that asked.
    """
    try:
        directory = Path(directory)
        free = _free_bytes(directory)
        if free is not None and free < _min_free_bytes:
            log = logger.debug if label in _floor_warned else logger.warning
            _floor_warned.add(label)
            log("Capture budget: %s skipped — %.1f GB free, floor is %.1f GB",
                label, free / 1024 ** 3, _min_free_bytes / 1024 ** 3)
            return False
        max_files, max_bytes = _dir_budgets.get(directory.resolve(), _default_budget)
        prune(directory, patterns, max_files, max_bytes, reserve=1)
    except Exception as e:
        logger.warning("Capture budget: check failed for %s (%s: %s) — allowing write",
                       label, type(e).__name__, e)
    return True
