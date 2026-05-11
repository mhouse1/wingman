# Wingman: Project Status & AI Evolution Roadmap

**Current Date:** May 10, 2026
**Current Version:** v1.6.6
**Current Phase:** Phase 2 complete — Phase 3 (Behavior Trees) planned; ADR 024 written

---

## Part 1: What Is Wingman Today?

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

### Phase 3: Behavior Trees 🎯
**"AI that makes strategic decisions"**

**Goal:** Replace the hardcoded mission sequence with a behavior tree that chooses tactics
based on current game state (health, ammo, enemy proximity).

**What this looks like:**
```
Root: Execute Mission
├─ If Respawning
│  └─ Cancel and Wait
├─ If Health Critical
│  ├─ Deploy Flares
│  └─ Nose Down (reduce exposure)
├─ If Missiles Empty
│  └─ Eject and Dive
└─ If Healthy & Armed
    ├─ If Enemy Close → Aggressive Attack
    └─ If Enemy Far  → Close Distance
```

**Current state:** ADR 024 written (Draft). Planned using the `py_trees` library. Health,
ammo, and enemy proximity are already detected — the perception inputs exist. ADR 028 (enemy
quadrant detection and nose orientation, Draft) is the next perception building block that
feeds Phase 3 tactics. The remaining work is replacing the fixed mission sequence with a tree.

**Estimated effort:** 30–60 hours
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

### Phase 6: Multi-Agent & Swarm Tactics 👥
**"Cooperative AI for multiplayer scenarios"**

Fleet of bots with coordinated tactics — shared target priority, attack vector coordination,
formation flying. Centralized training, decentralized execution.

**Estimated effort:** 100–150 hours additional
**AI level:** Multi-agent systems

---

## Part 3: Effort vs. Benefit

| Phase | Effort | Gameplay Benefit | Status |
|-------|--------|-----------------|--------|
| 1–2 (Automation + OCR Perception) | ✅ Done | Full unattended loop, all major detections | ✅ Complete |
| 3 (Behavior Trees) | 30–60h | Adapts tactics to situation | Planned (ADR 024 written) |
| 4 (RL) | 60–120h | Learns from play | Future |
| 5 (DRL + Vision) | 120–200h | Expert autonomy from raw screenshots | Research |
| 6 (Multi-Agent) | 100–150h | Fleet tactics | Research |

**Practical recommendation:** Phase 3 (behavior trees) is the highest-leverage next step —
the perception inputs already exist, ADR 028 (enemy quadrant detection) will add the final
spatial input it needs. Phases 4–6 are worthwhile for ML research but overkill for a game bot.

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

1. **Phase 3 — Behavior trees** (30–60h): ADR 024 written; ADR 028 (enemy quadrant) the remaining perception input
2. **ADR 031 — Round-end histogram** (Draft): implement in-process OCR timing summary on GAME_LOBBY entry
3. **ADR 028 — Enemy quadrant detection** (Draft): spatial awareness for Phase 3 tactics
4. **Enable GPU** (zero code changes): significant OCR latency reduction if CUDA is available
5. **Phase 4+ RL**: save for research/ML portfolio work

---

## Summary: Wingman's AI Journey

### Today (Phase 1–2 complete, v1.6.6)
```
User: "Press M once"
Bot: Clicks play → waits for matchmaking → waits for Good Luck (or alive fallback)
     → launches J20 (or target-painting variant)
     → deploys flares on INCOMING → handles health/ammo/respawn
     → respects manual takeover (BATTLE_MANUAL state)
     → clicks through match end → loops indefinitely
```

### Phase 3 (Behavior Trees)
```
Bot logic: "Missiles empty → eject. Health critical → evade. Enemy close → attack."
Result: Situational tactics instead of a fixed sequence
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
