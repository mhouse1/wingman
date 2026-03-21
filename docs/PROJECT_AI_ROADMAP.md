# Wingman: Project Status & AI Evolution Roadmap

**Current Date:** March 20, 2026
**Current Version:** v1.5.1 ("Enable full unattended operation")
**Current Phase:** Phase 1-2 (Automation + Text-Based Perception)

---

## Part 1: What Is Wingman Today?

### Current Architecture

**Wingman is a game automation bot with OCR-based text perception and full unattended operation:**

```
Wingman Bot
├── Game Automation (80%)
│   ├── Hotkey Input System (U/Y/X/M/P keys)
│   ├── Mission Execution (J20, Loiter)
│   ├── Flight Control (nose_up, padlock, fire, flares)
│   └── Auto-restart / auto-click / auto-launch logic
├── AI Component (20%)
│   └── EasyOCR — 4 detection targets via 8x8 grid regions:
│       ├── "RESPAWN"           → region 44 (cancel + restart mission)
│       ├── "INCOMING"          → region 21 (deploy flares)
│       ├── "CLICK TO CONTINUE" → region 60 (auto-click play button)
│       └── "GOOD LUCK"         → region 16 (auto-launch mission)
└── Infrastructure Layer
    ├── Game State Machine (LOBBY / BATTLE / END / STARTING)
    ├── Thread Pool (3 parallel OCR workers)
    ├── Background Threading (non-blocking, zero-copy frames)
    └── 8x8 Grid Region Extraction (active, used for all detections)
```

### Current Capabilities

| Feature | Type | Status |
|---------|------|--------|
| Hotkey Missions (U/Y) | Automation | ✅ Working |
| Auto-cancel on Respawn | Automation + Rule | ✅ Working |
| Auto-restart after 4s | Automation + Timer | ✅ Working |
| Respawn Text Detection | AI (EasyOCR region 44) | ✅ Working |
| Flight Control Sequences | Automation | ✅ Working |
| Weapon & Flare Loop | Automation | ✅ Working |
| Incoming Missile Detection | AI (EasyOCR region 21) | ✅ Working (v1.4+) |
| Auto-flare on Incoming | Automation + Rule | ✅ Working (v1.4+) |
| "Click to Continue" Detection | AI (EasyOCR region 60) | ✅ Working |
| Auto-click Play Button | Automation + Rule | ✅ Working |
| "Good Luck" Detection | AI (EasyOCR region 16) | ✅ Working |
| Auto-launch Mission after Game Start | Automation + Rule | ✅ Working |
| Game State Machine (4 states) | Architecture | ✅ Working (v1.4.3) |
| Full Unattended Loop (M key) | Automation | ✅ Working (v1.5.1) |

### What Makes It NOT AI-Driven

1. **Hardcoded mission sequences:** Nose up 2s → Padlock → Fire → Flares (no learning)
2. **Rule-based respawn logic:** `if "RESPAWN" detected → cancel mission` (simple condition)
3. **No adaptation:** Bot runs same sequence every time, doesn't learn from outcomes
4. **No perception beyond text:** Can't see enemy distance, health, ammo, or radar — but does detect 4 distinct game UI text states via OCR
5. **Limited autonomous decisions:** Once started with M or U/Y, bot runs fully unattended — but initial trigger still requires user input
6. **Static strategy:** Same approach to every enemy/scenario (no variation)

### Current Flow

```
User presses M (Unattended mode) or U (Manual J20 start)
    ↓
[M path] Controller clicks play button → GAME_STARTING state
    ↓ OCR scans region 16 for "GOOD LUCK"
    ↓ Wait 10s after detection → auto-launch mission_j20
    ↓
Controller executes mission_j20()
    ↓
Sequence: nose_up → padlock → fire → flares → roll (loop)
    ↓ (parallel background OCR threads)
    ├── Region 21: "INCOMING" detected?
    │       → Deploy flares immediately (non-blocking)
    ├── Region 44: "RESPAWN" detected?
    │       → Cancel mission → Wait 4s → Restart last mission
    └── Region 60: "CLICK TO" detected? (GAME_END_B state)
            → Auto-click play button → GAME_LOBBY state
                    → Back to M path (fully unattended loop)
```

---

## Part 2: Evolution to AI-Driven System

### Phase 1-2: Current State ✅
**"Game Automation Bot with Text-Based Perception"**

**What is working:**
- ✅ Full respawn detection + auto-restart (text-based)
- ✅ Incoming missile detection + auto-flare (text-based)
- ✅ Game state machine (LOBBY / BATTLE / END / STARTING)
- ✅ Full unattended loop — auto-click, auto-launch, auto-restart
- ✅ Mission hotkey system (U/Y/M)
- ✅ Threading-based parallel OCR (3 workers, non-blocking)

**Time to implement:** Done (estimated 40 hours of dev)

**What's missing for "true AI":**
- ❌ No learning from experience
- ❌ No adaptive strategy
- ❌ No perception beyond text (health, ammo, enemy distance, radar)

---

### Phase 2: Computer Vision Perception 📊
**"AI that sees more of the game"**

**Goal:** Detect game state beyond text — health, ammo, enemy proximity

**Already implemented (partial Phase 2):**
- ✅ Incoming missile text detection → auto-flare (region 21)
- ✅ Game state gating → OCR disabled in LOBBY to save CPU

**Remaining Phase 2 work (est. 20-40 hours):**

1. **Health Detection**
   - Train lightweight model on health bar screenshots
   - Detect: Critical health (<25%), Low (25-50%), Medium (50-75%), High (>75%)
   - Use: Trigger evasive maneuvers or retreat behavior

2. **Enemy Proximity Detection**
   - Radar analysis (detect red/enemy markers)
   - Ground range to target
   - Use: Adjust firing distance, decide when to engage

3. **Ammo Status Recognition**
   - OCR ammo counter
   - Detect: Out of ammo, low ammo, full
   - Use: Trigger resupply mode when low/empty

4. **UI State Detection**
   - Detect missile lock indicator (visual, not audio-based)
   - Throttle position
   - Use: Advanced evasion tactics

**Code Structure:**
```python
class GameStateAnalyzer:
    def analyze_frame(self, frame):
        return {
            'is_respawning': self._detect_respawn(frame),
            'health': self._detect_health(frame),           # NEW
            'ammo': self._detect_ammo(frame),               # NEW
            'enemy_distance': self._detect_distance(frame), # NEW
            'threat_level': self._detect_threats(frame),    # NEW
        }

class GameBot:
    def execute_mission(self):
        while mission_active:
            state = analyzer.analyze_frame()
            
            # Make decisions based on state
            if state['health'] < 25:
                self.execute_evasion()  # NEW
            if state['ammo'] == 0:
                self.rtb()  # Return to base (NEW)
            elif state['threat_level'] == 'high':
                self.aggressive_maneuvers()  # NEW
            else:
                self.normal_attack()
```

**AI Level:** Still mostly deterministic (if/else logic based on perception)

**Training Required:** 
- Collect 500-1000 labeled screenshots for each condition
- Train small CV models (~2-4 hours GPU training)

---

### Phase 3: Decision Tree / Behavior Trees 🎯
**"AI that makes strategic decisions"**

**Goal:** Instead of hardcoded sequences, bot chooses optimal tactics based on state

**Implementation (est. 30-60 hours):**

1. **Decision Logic**
   - Health low? → Execute evasion behavior
   - Enemy far? → Close distance
   - Enemy close? → Attack
   - Outnumbered? → Defensive posture
   - No ammo? → RTB

2. **Behavior Trees** (Structured decision-making)
   ```
   Root: Execute Mission
   ├─ If Respawning
   │  └─ Cancel and Wait
   ├─ If Health Critical (<25%)
   │  ├─ Deploy Flares
   │  ├─ Nose Down (reduce exposure)
   │  └─ Turn Away from Threat
   ├─ If No Ammo
   │  └─ RTB
   └─ If Healthy & Armed
       ├─ If Enemy Close (<5km)
       │  └─ Aggressive Attack
       └─ If Enemy Far
           └─ Close Distance
   ```

3. **Tactical Variations**
   - Different approaches per enemy type
   - Weather/altitude considerations
   - Formation awareness

**Code Structure:**
```python
class BehaviorTree:
    def execute(self, state):
        if state['is_respawning']:
            return self.respawn_recovery()
        
        if state['health'] < 0.25:
            return self.emergency_evasion(state)
        
        if state['ammo'] == 0:
            return self.rtb()
        
        # Choose tactic based on situation
        if state['enemy_distance'] < 5:
            return self.close_range_dogfight(state)
        else:
            return self.closing_attack(state)
```

**AI Level:** Task planning (what to do), not yet learning

**Training Required:** None (pure logic), just scenario testing

---

### Phase 4: Reinforcement Learning 🧠
**"AI that learns from experience"**

**Goal:** Bot learns to improve its strategy through gameplay

**Implementation (est. 60-120 hours):**

1. **Reward System**
   - +Points for: Kills, survival, mission completion, fuel efficiency
   - -Points for: Deaths, damage taken, friendly fire, inefficient maneuvers
   
2. **Learning Loop**
   ```
   Play Mission → Observe Outcome → Calculate Reward → Update Policy
   ```

3. **Policy Gradient (Q-Learning or PPO)**
   - Learns: "In situation X, action Y yields ~Z reward"
   - Adapts: Changes tactics based on 1000s of missions

4. **State Representation**
   ```python
   state = {
       'alt': altitude,           # 0-40k feet
       'speed': airspeed,         # 0-600 knots
       'enemy_dist': distance,    # 0-100km
       'enemy_alt': alt_diff,     # -20k to +20k
       'my_health': health,       # 0-100%
       'enemy_health': health,    # 0-100%
       'ammo_count': ammo,        # 0-N
       'fuel': fuel_pct,          # 0-100%
       'num_enemies': count,      # 1-8
   }
   ```

5. **Action Space**
   ```
   Actions: [altitude_adjust, speed_adjust, heading_change, fire, deploy_flares, ...]
   ```

**Code Example:**
```python
class RLAgent:
    def __init__(self):
        self.model = PPOPolicy()  # Neural network policy
        self.replay_buffer = []
    
    def select_action(self, state):
        action = self.model(state)  # Neural network decides
        return action
    
    def learn_from_mission(self, trajectory, reward):
        # trajectory = [(state, action), ...]
        self.replay_buffer.append((trajectory, reward))
        if len(self.replay_buffer) > 32:
            self.train_on_batch()
    
    def train_on_batch(self):
        # Update policy based on past performance
        loss = self.model.compute_loss(self.replay_buffer)
        self.model.optimize(loss)
```

**AI Level:** True machine learning (learns optimal behavior)

**Training Required:** 
- 1000-10,000 missions (50-100 hours gameplay)
- GPU recommended (4-8 hours training time)
- Simulation environment helps (10x faster)

---

### Phase 5: Deep Reinforcement Learning + Vision 🚀
**"Advanced AI that sees and learns"**

**Goal:** Combine visual perception with learned decision-making

**Implementation (est. 120-200 hours):**

1. **End-to-End Learning**
   - Input: Raw screenshots
   - Output: Flight control commands
   - Model learns to extract features AND decide actions

2. **Vision Backbone + RL Head**
   ```python
   class VisionRL:
       def __init__(self):
           self.vision_model = ResNet50()  # Extract features from screenshots
           self.policy_head = PPOPolicy()   # RL policy on extracted features
       
       def select_action(self, screenshot):
           features = self.vision_model(screenshot)
           action = self.policy_head(features)
           return action
   ```

3. **Learning from Raw Data**
   - Learns to recognize threats in images
   - Learns terrain/weather effects visually
   - No manual feature engineering needed

4. **Simulation Environment**
   - Synthetic mission scenarios
   - Millions of training iterations
   - 100x faster than real gameplay

**AI Level:** Deep learning + reinforcement learning (expert-level autonomy)

**Training Required:**
- Tens of thousands of missions
- GPU cluster (weeks of training)
- Or cloud training (fast iteration)

---

### Phase 6: Multi-Agent & Swarm Tactics 👥
**"Cooperative AI for multiplayer scenarios"**

**Goal:** Fleet of AI bots with coordinated tactics

**Implementation (est. 100-150 hours additional):**

1. **Communication Protocol**
   - Bots share target priority
   - Coordinate attack vectors
   - Cover each other

2. **Multi-Agent RL**
   - Each bot has own policy
   - Centralized training, decentralized execution
   - Learn formation flying, mutual support

3. **Emergent Behavior**
   - No hardcoded formations
   - Bots learn coordinated tactics
   - Adapt to enemy numbers

**AI Level:** Multi-agent systems (advanced autonomy)

---

## Part 3: Cost-Benefit Analysis

### Effort vs. Benefit

| Phase | Effort | Gameplay Benefit | AI Maturity |
|-------|--------|-----------------|-------------|
| 1-2 (Current) | ✅ Done | Full unattended automation + text perception | 20% |
| 2 remainder (Perception) | 20-40h | Health/ammo/distance awareness | 35% |
| 3 (Behavior Trees) | 30-60h | Adapts to situations | 55% |
| 4 (RL) | 60-120h | Learns from play | 80% |
| 5 (DRL + Vision) | 120-200h | Expert autonomy | 95% |
| 6 (Multi-Agent) | 100-150h | Fleet tactics | 99% |

### Diminishing Returns

- **Phase 1-2:** High impact (user perceives big changes)
- **Phase 3:** Medium impact (better decision-making)
- **Phase 4:** High impact again (truly adaptive)
- **Phase 5-6:** Niche improvements (overkill for single-player)

### Practical Recommendation

**If goal is gameplay improvement:**
- **First: Enable GPU** → Zero code changes, immediate 10-15x OCR speedup (see Bottlenecks)
- Complete Phase 2 remainder (health/ammo/distance) → Time: 20-40h, Impact: High
- Add Phase 3 (behavior trees) → Time: 30-60h, Impact: Medium-High
- **Skip Phase 4-6 unless researching ML**

**If goal is research/learning:**
- Do all phases in sequence
- Document each step
- Publish findings

---

## Part 4: Current Bottlenecks & Next Steps

### Hardware Bottlenecks

**Measured OCR performance (v1.5.1, CPU-only, from live session log 2026-03-20):**

| Metric | Respawn OCR | Incoming OCR | Total (wall clock) |
|--------|------------|--------------|-------------------|
| Average | 2.63s | 3.07s | 3.25s |
| Best | 1.63s | 1.82s | 1.85s |
| Worst | 3.78s | 4.60s | 4.60s |

Respawn and incoming OCR run in parallel (3 threads), so wall clock = max(respawn, incoming).
Extract and Submit overhead = 0.00s — all cost is EasyOCR inference.
High variance (1.85–4.60s) reflects CPU thread contention when both workers are active simultaneously.

- **Current:** CPU-only OCR averaging **3.25s per cycle**, worst case **4.6s**
- **Solution 1:** Enable GPU → expected <200ms per cycle (10-15x improvement, no code changes required — GPU path already implemented in v1.5.0, just needs CUDA available)
- **Solution 2:** Reduce capture resolution → 2-3x faster, minimal detection quality loss

### Data Collection Bottleneck (Phase 2+)
- **Current:** No labeled dataset for health, ammo, distance detection
- **Required:** 500-1000 screenshots per condition
- **Time:** 8-16 hours (manual labeling or automated region extraction)

### Training Bottleneck (Phase 4+)
- **Current:** No RL infrastructure
- **Required:** Reward function design, episode logging, model saving
- **Time:** 20-40 hours infrastructure, then 1000+ mission iterations

---

## Summary: Wingman's AI Journey

### Today (Phase 1-2)
```
User: "Press M once"
Bot: Clicks play → waits for Good Luck → launches J20 → deploys flares on INCOMING
     → cancels and restarts on RESPAWN → clicks play again on match end → loops forever
Result: Full unattended operation across multiple matches, OCR-driven text perception
```

### Phase 2 (Perception)
```
Bot sees: "Health <25%, Enemy 10km away, 3 missiles incoming"
Bot adapts: Switch from attack to evasion
Result: Situational awareness, smoother gameplay
```

### Phase 3 (Behavior Trees)
```
Bot logic: "If critical health → Deploy flares → Nose down → Turn away"
Result: Intelligent decision-making, feels like real pilot tactics
```

### Phase 4 (Reinforcement Learning)
```
After 1000 missions: "I learned tight turns at low altitude work better"
Bot improves: Adjusts strategy based on past performance
Result: Continuously improving skill level
```

### Phase 5 (Deep RL + Vision)
```
Bot sees screenshot → Extracts features → Decides action → Learns from outcome
Result: Expert-level unsupervised learning from raw game data
```

---

## Roadmap

**Current Status:** Phase 1-2 complete. Full unattended operation working. OCR running CPU-only at 3.25s average / 4.6s worst case.

**Highest-impact next step (zero code changes):** Enable GPU for EasyOCR — the GPU path is already implemented in v1.5.0, it just needs CUDA available. Expected improvement: 3.25s → <200ms per cycle.

**Next feature priority:** Phase 2 remainder (health/ammo/distance detection) — 20-40h, high user-visible impact.

**Save Phase 4-6 for:** Research projects, academic papers, or technology exploration—they're overkill for a game bot but valuable for ML portfolio.
