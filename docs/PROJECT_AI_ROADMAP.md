# Wingman: Project Status & AI Evolution Roadmap

**Current Date:** August 17, 2026
**Current Version:** v1.8.3
**Current Phase:** Phase 3 in progress — behavior tree **active** in live sessions; multi-agent squad coordination begins during Phase 3, not after Phase 5

---

## Part 1: What Is Wingman Today?

> **Snapshot note:** Part 1 was written at v1.6.6 (May 2026) and describes the Phase 1–2 baseline. For current capabilities see the README; the Phase 3 section below is kept current.

### Current Architecture

**Wingman is a game automation bot with OCR-based perception, a formal state machine, and full unattended operation:**

```
Wingman Bot
├── Game Automation
│   ├── Hotkey Input System (U/Y/X/M/P/V/B/End/Backspace keys)
│   ├── Mission Execution (J20, J20 Target-Painting Mode, Loiter)
│   ├── Flight Control (nose_up, padlock, afterburner, flares, fire)
│   ├── Auto-restart / auto-click / auto-launch / eject-and-dive logic
│   └── Manual Takeover (GAME_BATTLE_MANUAL — suppresses auto-restart)
├── Perception (EasyOCR, CPU-only, 13-worker thread pool)
│   ├── Named crop regions in config.yaml (percentage coordinates)
│   ├── "RESPAWN"           → cancel mission + restart
│   ├── "INCOMING"          → deploy flares immediately
│   ├── "CLICK TO CONTINUE" → GAME_END_B → click through
│   ├── "GOOD LUCK"         → wait 13s, launch mission
│   ├── Health OCR          → spike-filter ceiling, restart on death
│   ├── Ammo OCR            → eject-and-dive when missiles empty
│   ├── Enemy proximity OCR → awareness during combat
│   └── Lobby popup OCR     → dismiss overlays automatically (8 popup types incl. SILVER)
└── Infrastructure
    ├── Formal FSM (7 states, transitions library, thread-safe)
    ├── 13-worker ThreadPoolExecutor (parallel OCR per crop, non-blocking)
    ├── Dedicated lobby quick-scan background thread (independent of main loop)
    ├── Dedicated background OCR thread for GAME_BATTLE continuous perception
    └── Offline calibration tooling (no live game needed)
```

### Current Capabilities

| Feature | Status |
|---------|--------|
| Full unattended match loop (`M` key) | ✅ Working |
| J20 and Loiter missions | ✅ Working |
| J20 target-painting mode (configurable) | ✅ Working |
| Formal FSM — 7 states (LOBBY / WAITING / STARTING / STARTING_STALLED / BATTLE / BATTLE_MANUAL / END) | ✅ Working |
| GAME_BATTLE_MANUAL — maneuver key triggers manual takeover, suppresses auto-restart | ✅ Working |
| Game-starting stall detection + recovery | ✅ Working |
| GAME_STARTING health-scan fallback (alive detected before Good Luck → launch immediately) | ✅ Working |
| Respawn detection + auto-restart | ✅ Working |
| Incoming missile detection + auto-flare | ✅ Working |
| Health monitoring — spike-filter ceiling, mission restart on death | ✅ Working |
| Health ceiling reset on respawn (False→True alive transition) | ✅ Working |
| Ammo tracking — eject-and-dive when missiles empty | ✅ Working |
| Enemy proximity detection | ✅ Working |
| Search and destroy loop (auto-padlock + auto-fire) | ✅ Working |
| Lobby popup handling (8 popup types incl. SILVER medal) | ✅ Working |
| "Click to Continue" auto-click at match end | ✅ Working |
| Named crop regions — offline calibration, no grid arithmetic | ✅ Working |
| CPU-only OCR — no GPU required | ✅ Working |
| Parallel OCR — 13 pre-warmed workers, wall-clock ~1.0–1.4s per GAME_BATTLE cycle | ✅ Working |

### Current Flow

```
User presses M (Unattended mode)
    ↓
[GAME_LOBBY] → lobby quick-scan thread detects PLAY/READY → click play → GAME_WAITING
    ↓
[GAME_WAITING] → scan for CANCEL (matchmaking confirmed) → GAME_STARTING
    ↓
[GAME_STARTING] → press J20 key every 5s → scan for "GOOD LUCK"
    ├── Good Luck detected → wait 13s → GAME_BATTLE → launch mission_j20
    └── game_battle_alive detected (10s gate) → GAME_BATTLE → launch mission_j20 immediately
    ↓
[GAME_BATTLE] mission running with parallel OCR (background thread):
    ├── "INCOMING" detected? → deploy flares immediately
    ├── Health dead→alive transition? → reset ceiling → restart mission
    ├── RESPAWN text visible? → cancel → eject-and-dive → wait for health alive
    ├── Missiles empty? → cancel → eject-and-dive → wait for respawn
    ├── Maneuver key pressed manually? → GAME_BATTLE_MANUAL (auto-restart suppressed)
    └── "CLICK TO" detected? → GAME_END_B → click through results screen
    ↓
[GAME_END_B] → click to continue → click PLAY → GAME_LOBBY → loop
```

---

## Part 2: Evolution to AI-Driven System

### Phase 1–2: Complete ✅
**"Game Automation Bot with OCR-Based Perception"**

Everything in the current capabilities table is working. Perception was implemented via OCR
rather than trained CV models — this was faster to build and sufficient for the game's UI.
The infrastructure layer has matured significantly: 13 pre-warmed OCR workers, a formal FSM,
a dedicated lobby quick-scan thread, and a health ceiling spike filter.

**What's missing for "true AI":**
- ❌ No learning from experience
- ❌ No adaptive strategy — same mission sequence every time regardless of game state
- ❌ No image-based perception (radar image, missile lock indicator, fuel state)

---

### Phase 3: Behavior Trees 🎯 — in progress (active)
**"AI that makes strategic decisions"**

**Goal:** Replace the hardcoded mission sequence with a behavior tree that chooses tactics
based on current game state (health, ammo, enemy proximity, altitude, incoming threats).

**Current state (v1.8.3):** The tree (ADR 024, `py_trees`) runs **active** in every live
session. Each tick freezes an `AnalyzerSnapshot` and a priority selector picks the tactic:

- **Eject** — missiles-empty eject-and-dive on the debounced ammo verdict (ADR 056/069)
- **MissileEvade** — evasive manoeuvre on incoming detection (ADR 070); live A/B evidence:
  90% vs 68% ten-second survival with the evade on (n=122 engagements)
- **Climb** — terrain avoidance and closed-loop climb-to-operating-altitude, including the
  mission-start prologue (ADR 073)
- **Engage** — minimap ring-engage geometry: steer toward contacts, orbit when merged
  (ADR 024 3.1a, ADR 028)
- **Disengage / AttackSupport / Idle / RespawnWait** — supporting tactics and
  selection-only states

The J20 mission has been rewritten from a hardcoded maneuver script to this tactic-driven
model; the remaining open-loop pieces (afterburner cadence, the fixed mission window) are
conversion candidates. New tactics enter through the **shadow-first pipeline** proven by
ADR 073: selection-only logging against live data first, actuation only after the shadow
evidence holds up, then live A/B validation via per-engagement survival stats (ADR 055/070).

**Squad coordination starts here, not after Phase 5:** each Wingman instance can hold a
role (aggressive, loiter, target-painting, support) as a tactic configuration of the same
tree — the first multi-agent work is multiple instances flying complementary roles during
Phase 3 (see Multi-Agent Track below).

**AI level:** Task planning (what to do), not yet learning

---

### Phase 4: Reinforcement Learning 🧠
**"AI that learns from experience"**

**Goal:** Bot learns to improve its strategy through gameplay.

1. **Reward System** — +points for kills/survival, −points for deaths/damage
2. **Learning Loop** — Play → Observe Outcome → Calculate Reward → Update Policy
3. **Policy Gradient (Q-Learning or PPO)** — learns "in situation X, action Y yields ~Z reward"

```python
class RLAgent:
    def select_action(self, state):
        return self.model(state)  # neural network decides

    def learn_from_mission(self, trajectory, reward):
        self.replay_buffer.append((trajectory, reward))
        if len(self.replay_buffer) > 32:
            self.train_on_batch()
```

**Estimated effort:** 60–120 hours + 1000+ mission iterations
**AI level:** True machine learning (learns optimal behavior)

---

### Phase 5: Deep Reinforcement Learning + Vision 🚀
**"Advanced AI that sees and learns"**

**Goal:** End-to-end learning from raw screenshots — no manual feature engineering.

```python
class VisionRL:
    def select_action(self, screenshot):
        features = self.vision_model(screenshot)  # ResNet/ViT backbone
        return self.policy_head(features)         # RL policy on extracted features
```

**Estimated effort:** 120–200 hours + GPU cluster
**AI level:** Deep learning + RL (expert-level autonomy)

---

### Multi-Agent Track: Squad & Swarm Tactics 👥 — during and after Phase 3
**"Cooperative AI for multiplayer scenarios"**

Not a terminal phase — a parallel track that **begins during Phase 3** and deepens through
Phases 4–5:

- **During Phase 3:** multiple Wingman instances flying complementary behavior-tree roles
  (aggressive, loiter, target-painting, support). The behavior tree is what makes this
  practical: a role is a tactic configuration of the same tree, so coordination starts as
  role assignment, not new AI machinery.
- **After Phase 3 (with Phases 4–5):** shared target priority, attack vector coordination,
  formation flying, learned coordinated policies — centralized training, decentralized
  execution.

**Estimated effort:** 100–150 hours across the track
**AI level:** Multi-agent systems

---

## Part 3: Effort vs. Benefit

| Phase | Effort | Gameplay Benefit | Status |
|-------|--------|-----------------|--------|
| 1–2 (Automation + OCR Perception) | ✅ Done | Full unattended loop, all major detections | ✅ Complete |
| 3 (Behavior Trees) | 30–60h | Adapts tactics to situation | 🎯 In progress — tree active, evade + climb validated live |
| Multi-Agent Track (during/after Phase 3) | 100–150h | Squad roles, then fleet tactics | Starts during Phase 3 |
| 4 (RL) | 60–120h | Learns from play | Future |
| 5 (DRL + Vision) | 120–200h | Expert autonomy from raw screenshots | Research |

**Practical recommendation:** Phase 3 delivered on its promise — the perception inputs were
already in place and the active tree now carries live-validated tactics (evade, climb, eject,
engage geometry). The next leverage points are finishing the tactic conversion (energy /
afterburner discipline via a shadow-first spike) and starting the multi-agent track with
role-configured instances of the same tree. Phases 4–5 remain worthwhile for ML research but
are not required for effective squad play.

---

## Part 4: Current Bottlenecks & Next Steps

### OCR Performance

**Measured CPU-only, v1.6.5–v1.6.6 (13-worker pool):**

| Metric | Value |
|--------|-------|
| Typical GAME_BATTLE cycle (5 crops in parallel) | ~1.0–1.4s |
| Fast cycle (warm workers, light frame) | ~0.2–0.35s |
| Worst case (cold start or CPU contention) | ~1.5s |

Wall-clock time = max of concurrent crop scans (5 crops: respawn, incoming, health, flares, missiles). Workers are pre-warmed at startup with 13 dummy tasks to avoid cold-start latency in GAME_BATTLE.

- **GPU path** is already implemented — enabling CUDA drops this to <200ms per cycle (5–7× improvement, no code changes needed)
- **Resolution reduction** is an alternative for CPU-only: 2–3× faster with minimal quality loss

### Known Open Items (tracked in CR-009)

| Item | Risk | Status |
|------|------|--------|
| INCOMING crop region bleeds "TURNTOBATTLE" / cockpit text | Medium | Open — needs screenshot calibration |
| Health ceiling ratchet within a life | Low | Monitor — respawn reset now in place |

### Next Steps

1. **Phase 3 — finish the tactic conversion**: energy/afterburner discipline via a
   shadow-first spike; retire the remaining fixed mission window (ADR 024/073 pipeline)
2. **Multi-agent track — squad pilot**: two instances flying complementary behavior-tree
   roles (first concrete step of the multi-agent track, during Phase 3)
3. **Enable GPU** (zero code changes): significant OCR latency reduction if CUDA is available
4. **Phase 4+ RL**: save for research/ML portfolio work

---

## Summary: Wingman's AI Journey

### Phase 1–2 baseline (v1.6.6)
```
User: "Press M once"
Bot: Clicks play → waits for matchmaking → waits for Good Luck (or alive fallback)
     → launches J20 (or target-painting variant)
     → deploys flares on INCOMING → handles health/ammo/respawn
     → respects manual takeover (BATTLE_MANUAL state)
     → clicks through match end → loops indefinitely
```

### Phase 3 (Behavior Trees) — where we are now (v1.8.3)
```
Bot logic: "Incoming missile → evade. Below safe altitude → climb. Missiles empty → eject.
            Contacts on minimap → engage geometry. Otherwise → support."
Result: Situational tactics instead of a fixed sequence — live-validated per-engagement
        (evade: 90% vs 68% ten-second survival, n=122)
```

### Multi-Agent Track (starting during Phase 3)
```
Two or more instances: one aggressive, one loiter/support — each a role configuration
of the same behavior tree
Result: Squad behavior emerges from role assignment before any learning is involved
```

### Phase 4 (Reinforcement Learning)
```
After 1000 missions: "I learned tight turns at low altitude work better here"
Result: Continuously improving strategy from experience
```

### Phase 5 (Deep RL + Vision)
```
Bot sees raw screenshot → extracts features → decides action → learns from outcome
Result: Expert-level unsupervised learning with no manual feature engineering
```
