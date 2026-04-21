# Wingman: Project Status & AI Evolution Roadmap

**Current Date:** April 20, 2026
**Current Version:** v1.6.3
**Current Phase:** Phase 2 complete — working toward Phase 3 (Behavior Trees)

---

## Part 1: What Is Wingman Today?

### Current Architecture

**Wingman is a game automation bot with OCR-based perception, a formal state machine, and full unattended operation:**

```
Wingman Bot
├── Game Automation
│   ├── Hotkey Input System (U/Y/X/M/P/End keys)
│   ├── Mission Execution (J20, Loiter)
│   ├── Flight Control (nose_up, padlock, afterburner, flares, fire)
│   └── Auto-restart / auto-click / auto-launch / eject-and-dive logic
├── Perception (EasyOCR, CPU-only)
│   ├── Named crop regions in config.yaml (percentage coordinates)
│   ├── "RESPAWN"           → cancel mission + restart
│   ├── "INCOMING"          → deploy flares
│   ├── "CLICK TO CONTINUE" → GAME_END_B → click through
│   ├── "GOOD LUCK"         → launch mission
│   ├── Health OCR          → restart on death
│   ├── Ammo OCR            → eject-and-dive when missiles empty
│   ├── Enemy proximity OCR → awareness during combat
│   └── Lobby popup OCR     → dismiss overlays automatically
└── Infrastructure
    ├── Formal FSM (6 states, transitions library, thread-safe)
    ├── Thread Pool (3 parallel OCR workers, non-blocking)
    └── Offline calibration tooling (no live game needed)
```

### Current Capabilities

| Feature | Status |
|---------|--------|
| Full unattended match loop (`m` key) | ✅ Working |
| J20 and Loiter missions | ✅ Working |
| Formal FSM — 6 states (LOBBY / WAITING / STARTING / STARTING_STALLED / BATTLE / END) | ✅ Working |
| Game-starting stall detection + recovery | ✅ Working |
| Respawn detection + auto-restart | ✅ Working |
| Incoming missile detection + auto-flare | ✅ Working |
| Health monitoring — mission restart on death | ✅ Working |
| Ammo tracking — eject-and-dive when missiles empty | ✅ Working |
| Enemy proximity detection | ✅ Working |
| Search and destroy loop (auto-padlock + auto-fire) | ✅ Working |
| Lobby popup handling (7 popup types) | ✅ Working |
| "Click to Continue" auto-click at match end | ✅ Working |
| Named crop regions — offline calibration, no grid arithmetic | ✅ Working |
| CPU-only OCR — no GPU required | ✅ Working |

### Current Flow

```
User presses M (Unattended mode)
    ↓
[GAME_LOBBY] → scan for PLAY/READY → click play → GAME_WAITING
    ↓
[GAME_WAITING] → scan for CANCEL (matchmaking confirmed) → GAME_STARTING
    ↓
[GAME_STARTING] → scan for "GOOD LUCK" → GAME_BATTLE → launch mission_j20
    ↓
[GAME_BATTLE] mission running with parallel OCR:
    ├── "INCOMING" detected? → deploy flares immediately
    ├── Health = 0 / RESPAWN detected? → cancel → eject-and-dive → restart
    ├── Missiles empty? → cancel → eject-and-dive → wait for respawn
    └── "CLICK TO" detected? → GAME_END_B → click through results screen
    ↓
[GAME_END_B] → click to continue → click PLAY → GAME_LOBBY → loop
```

---

## Part 2: Evolution to AI-Driven System

### Phase 1–2: Complete ✅
**"Game Automation Bot with OCR-Based Perception"**

Everything in the current capabilities table is working. Perception was implemented via OCR rather than trained CV models — this was faster to build and sufficient for the game's UI.

**What's missing for "true AI":**
- ❌ No learning from experience
- ❌ No adaptive strategy — same mission sequence every time
- ❌ No image-based perception (health bar image, radar, missile lock indicator)

---

### Phase 3: Behavior Trees 🎯
**"AI that makes strategic decisions"**

**Goal:** Replace the hardcoded mission sequence with a behavior tree that chooses tactics based on current game state (health, ammo, enemy proximity).

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

**Current state:** ADR 024 written. Planned using the `py_trees` library. Health, ammo, and enemy proximity are already detected — the perception inputs exist. The remaining work is replacing the fixed mission sequence with a tree that reads them.

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

Fleet of bots with coordinated tactics — shared target priority, attack vector coordination, formation flying. Centralized training, decentralized execution.

**Estimated effort:** 100–150 hours additional
**AI level:** Multi-agent systems

---

## Part 3: Effort vs. Benefit

| Phase | Effort | Gameplay Benefit | Status |
|-------|--------|-----------------|--------|
| 1–2 (Automation + OCR Perception) | ✅ Done | Full unattended loop, all major detections | ✅ Complete |
| 3 (Behavior Trees) | 30–60h | Adapts tactics to situation | Planned |
| 4 (RL) | 60–120h | Learns from play | Future |
| 5 (DRL + Vision) | 120–200h | Expert autonomy from raw screenshots | Research |
| 6 (Multi-Agent) | 100–150h | Fleet tactics | Research |

**Practical recommendation:** Phase 3 (behavior trees) is the highest-leverage next step — the perception inputs already exist, it just needs the decision layer on top. Phases 4–6 are worthwhile for ML research but overkill for a game bot.

---

## Part 4: Current Bottlenecks & Next Steps

### OCR Performance

**Measured CPU-only, v1.5.1 baseline:**

| Metric | Value |
|--------|-------|
| Average cycle | ~3.25s |
| Best case | ~1.85s |
| Worst case | ~4.60s |

Three OCR workers run in parallel so wall-clock time = max of concurrent scans. Variance reflects CPU thread contention.

- **GPU path** is already implemented — enabling CUDA drops this to <200ms per cycle (10–15× improvement, no code changes needed)
- **Resolution reduction** is an alternative: 2–3× faster with minimal quality loss

### Next Steps

1. **Phase 3 — Behavior trees** (30–60h): perception inputs exist, decision layer needed
2. **Enable GPU** (zero code changes): significant OCR latency reduction if CUDA is available
3. **Phase 4+ RL**: save for research/ML portfolio work

---

## Summary: Wingman's AI Journey

### Today (Phase 1–2 complete)
```
User: "Press M once"
Bot: Clicks play → waits for matchmaking → waits for Good Luck → launches J20
     → deploys flares on INCOMING → handles health/ammo/respawn
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
