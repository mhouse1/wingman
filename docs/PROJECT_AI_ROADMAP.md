# Wingman: Project Status & AI Evolution Roadmap

**Current Date:** February 21, 2026  
**Current Phase:** Phase 1 (Game Automation with Limited AI)

---

## Part 1: What Is Wingman Today?

### Current Architecture

**Wingman is a game automation bot with a single AI component:**

```
Wingman Bot
├── Game Automation (95%)
│   ├── Hotkey Input System (U/Y/X keys)
│   ├── Mission Execution (J20, Loiter)
│   ├── Flight Control (nose_up, padlock, fire, flares)
│   └── Respawn Detection (Rule-based trigger)
├── AI Component (5%)
│   └── EasyOCR (Detects "RESPAWN" text in screenshots)
└── Optimization Layer
    ├── Frame Caching (5s cooldown)
    ├── Background Threading (non-blocking analysis)
    └── Grid Region Extraction (attempted, abandoned)
```

### Current Capabilities

| Feature | Type | Status |
|---------|------|--------|
| Hotkey Missions (U/Y) | Automation | ✅ Working |
| Auto-cancel on Respawn | Automation + Rule | ✅ Working |
| Auto-restart after 5s | Automation + Timer | ✅ Working |
| Respawn Text Detection | AI (EasyOCR) | ✅ Working |
| Flight Control Sequences | Automation | ✅ Working |
| Weapon & Flare Loop | Automation | ✅ Working |

### What Makes It NOT AI-Driven

1. **Hardcoded mission sequences:** Nose up 2s → Padlock → Fire → Flares (no learning)
2. **Rule-based respawn logic:** `if "RESPAWN" detected → cancel mission` (simple condition)
3. **No adaptation:** Bot runs same sequence every time, doesn't learn from outcomes
4. **No perception beyond text:** Can't see enemy distance, health, ammo, radar
5. **No autonomous decisions:** User triggers missions with hotkeys; bot executes script
6. **Static strategy:** Same approach to every enemy/scenario (no variation)

### Current Flow

```
User presses U (Start J20)
    ↓
Controller executes mission_j20()
    ↓
Sequence: nose_up → padlock → fire → flares → roll (loop)
    ↓
Analyzer.analyze_frame() checks for "RESPAWN"
    ↓
If "RESPAWN" detected:
    - Cancel missions
    - Wait 5s
    - Restart last mission
    ↓
If no respawn: Continue mission loop
```

---

## Part 2: Evolution to AI-Driven System

### Phase 1: Current State ✅
**"Game Automation Bot with Basic AI"**

**What you have:**
- ✅ Full respawn detection (text-based)
- ✅ Mission hotkey system
- ✅ Auto-restart on respawn
- ✅ Performance optimized (threading + caching)

**Time to implement:** Done (estimated 40 hours of dev)

**What's missing for "true AI":**
- ❌ No learning from experience
- ❌ No adaptive strategy
- ❌ No perception of game state beyond text

---

### Phase 2: Computer Vision Perception 📊
**"AI that sees more of the game"**

**Goal:** Detect game state beyond just "RESPAWN" text

**Implementation (est. 20-40 hours):**

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
   - Use: Trigger RTB (return to base) when low

4. **UI State Detection**
   - Detect missile lock (beeping)
   - Lock warning indicators
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
| 1 (Current) | ✅ Done | Basic automation | 5% |
| 2 (Perception) | 20-40h | Better tactics | 30% |
| 3 (Behavior Trees) | 30-60h | Adapts to situations | 50% |
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
- Do Phase 2 (perception) → Time: 20-40h, Impact: High
- Add Phase 3 (behavior trees) → Time: 30-60h, Impact: Medium-High
- **Skip Phase 4-6 unless researching ML**

**If goal is research/learning:**
- Do all phases in sequence
- Document each step
- Publish findings

---

## Part 4: Current Bottlenecks & Next Steps

### Hardware Bottlenecks
- **Current:** CPU-only OCR (2.6s per 5s)
- **Solution 1:** Enable GPU (10-50x faster) → Immediate 20-40h speedup
- **Solution 2:** Reduce resolution → 2-3x faster, minimal quality loss

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

### Today (Phase 1)
```
User: "Press U for J20 mission"
Bot: Execute hardcoded sequence, detect respawn text, restart
Result: Repeatable automation with text detection
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

## Recommendation

**Current Status:** Excellent foundation for automation. Ready for Phase 2 if desired.

**Next Priority:** Phase 2 (Perception) would give the most practical gameplay improvement with reasonable effort (20-40 hours, high user-visible impact).

**Save Phase 4-6 for:** Research projects, academic papers, or technology exploration—they're overkill for a game bot but valuable for ML portfolio.
