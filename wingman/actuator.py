"""The single place wingman injects a key. CR-018-09 Phase 1.

Before this, five press paths in controller.py called the keyboard backend
directly, each applying its own subset of the guarantees a press needs. Phase 1
moves every raw press and release behind this object WITHOUT changing what any
of them does, following ADR 139's "replicate, do not fix":

- ``focus_gate`` is ADR 098's focus guard. The sites that pressed through
  ``_press_key`` pass True; ``_climb_key``, the eject sequence's unwatched keys
  and the missile-evade burner never had it and pass False.
- Simulate-mode intents, the echo bracket and release grace for watched keys,
  and the SAF-001 refusal stay with their callers for now.
- Errors propagate exactly as the backend raised them, so each caller's own
  handling is unchanged. The one exception is ``release_all``, which is the
  loop both sweeps already ran.

Phase 2 unifies the gates here, one key at a time, each with a live check.

The object also records which owner holds which key. For most keys nothing acts
on that: a DEBUG line notes each release that drops a key another owner pressed
(the re-press race CR-018-09 describes), and the takeover log reports the snapshot.

Phase 2 (2026-09-27) makes the record authoritative for ``leased_keys``, starting
with AFTERBURNER_KEY, one key at a time as the review asks. For a leased key an
owner's release lifts the key only when no other owner that may hold it right now
still does, so a climb ending no longer drops the burn that cruise is holding. Who
may hold it is the controller's policy table (``set_hold_policy``), and
``reevaluate`` lifts the key the moment that policy turns against every holder.
"""

import logging
import threading

logger = logging.getLogger(__name__)


class Actuator:
    """Every key wingman presses or releases goes through one of these methods.

    ``keyboard`` and ``may_inject`` are called on every use rather than read
    once, so a test that swaps ``controller.keyboard_module`` or the focus guard
    still reaches every press.
    """

    def __init__(self, keyboard, may_inject, *, leased_keys=()):
        self._keyboard = keyboard
        self._may_inject = may_inject
        self._leased = frozenset(leased_keys)
        self._may_hold = lambda _key, _owner: True
        self._held: "dict[str, set[str]]" = {}
        self._lock = threading.Lock()

    def set_hold_policy(self, may_hold) -> None:
        """``may_hold(key, owner) -> bool``: may this owner's lease count now."""
        self._may_hold = may_hold

    def press(self, key: str, owner: str, *, focus_gate: bool) -> bool:
        """Press ``key`` for ``owner``. True if the key was actually pressed.

        A focus-gated press that the guard refuses returns False and presses
        nothing; the caller's hold and release proceed as if it had (ADR 098:
        gate the injection, never the control flow).
        """
        kb = self._keyboard()
        if kb is None:
            return False
        if focus_gate and not self._may_inject("key"):
            return False
        kb.press(key)
        with self._lock:
            self._held.setdefault(key, set()).add(owner)
        return True

    def release(self, key: str, owner: str) -> None:
        """Release ``key``. Never gated: a gated release is a latched key.

        For a leased key this ends ``owner``'s lease, and the key goes up only
        when no remaining owner may hold it right now.
        """
        kb = self._keyboard()
        if kb is None:
            return
        if key in self._leased:
            with self._lock:
                owners = self._held.get(key, set())
                owners.discard(owner)
                if not owners:
                    self._held.pop(key, None)
                remaining = sorted(owners)
            holding = [o for o in remaining if self._may_hold(key, o)]
            if holding:
                logger.debug("Actuator: %s released %r; still held for %s",
                             owner, key, ", ".join(holding))
                return
            kb.release(key)
            return
        kb.release(key)
        with self._lock:
            owners = self._held.pop(key, set())
        others = owners - {owner}
        if others:
            logger.debug("Actuator: %s released %r while %s held it",
                         owner, key, ", ".join(sorted(others)))

    def reevaluate(self, key: str) -> None:
        """Lift a leased key at once when every holder must now yield.

        Called when a condition in the policy changes (an emergency climb
        starting), so the key does not wait for each holder's next tick to let
        go. The leases stay: a holder re-presses when it may again.
        """
        kb = self._keyboard()
        if kb is None or key not in self._leased:
            return
        with self._lock:
            owners = sorted(self._held.get(key, ()))
        if owners and not any(self._may_hold(key, o) for o in owners):
            kb.release(key)
            logger.info("Actuator: %r released: its holders (%s) all yield now",
                        key, ", ".join(owners))

    def tap(self, key: str, owner: str) -> None:  # noqa: ARG002 - owner names the writer at the call site
        """Press and release ``key`` in one backend call."""
        kb = self._keyboard()
        if kb is None:
            return
        kb.press_and_release(key)

    def release_all(self, keys, context: str, latch_note: str) -> None:
        """Release every key in ``keys``. A failed release is logged and the sweep
        continues, because the next key may be the one that is actually stuck."""
        kb = self._keyboard()
        if kb is None:
            return
        for key in keys:
            try:
                kb.release(key)
            except Exception:
                logger.error("%s release of %r failed — %s", context, key, latch_note)
        with self._lock:
            self._held.clear()

    def held(self) -> "dict[str, tuple[str, ...]]":
        """Keys this process pressed and has not released, with their owners."""
        with self._lock:
            return {k: tuple(sorted(v)) for k, v in self._held.items()}
