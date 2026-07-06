# Anchor-policy RL: literature review and approach audit

**Scope.** This report surveys the scientific literature relevant to the
Vanchor-NG anchor (station-keeping / virtual anchor) learning experiment, then
audits our actual code against it and gives prioritized, concrete
recommendations. It closes with a verdict on our empirical single-thruster
energy finding.

**What we built (the system under review).**

- `experiments/anchor_policy/policy.py` — a ~650-param numpy tanh-MLP
  (8→24→16→2), no torch, Raspberry-Pi-deployable, serialised to JSON.
- `experiments/anchor_policy/env.py` — the station-keeping task over the **real**
  Fossen 3-DOF physics (`src/vanchor/sim/fossen.py`) plus the gust
  (Ornstein–Uhlenbeck) and slow weather-wander pipeline. Body-frame
  observations; reward `-(dist) - 0.08·thrust² - 0.6·(outside 5 m watch circle)`.
- `experiments/anchor_policy/scenarios.py` — domain randomization over
  wind/current/gusts/weather-wander, boat (mass, hull tracking, mount, motor
  power) and start condition; deterministic per integer seed; held-out
  validation set.
- `experiments/anchor_policy/train.py` — OpenAI-ES (antithetic, rank-normalised,
  Adam ascent), multiprocessing, common-random-numbers per generation,
  validation logging + "best-so-far" checkpoint.
- Baseline to beat: the existing PID `AnchorHoldMode`
  (`src/vanchor/controller/modes.py`) — a PD on distance-to-mark with an idle
  dead-band, a "recover" floor, reverse-thrust backing, and an *optional*
  feed-forward that points the bow into an estimated drift.

A verification note throughout: citations marked **[V]** were verified by
fetching the abstract/landing page; **[S]** were confirmed from search-result
metadata (title/authors/venue/DOI consistent across indexers) but the page
itself was not opened (mostly ScienceDirect/MDPI 403s). All URLs are real;
nothing was fabricated. Where a DOI or author is a best-reconstruction it is
flagged inline.

---

## 1. Executive summary

**Top findings.**

1. **Our overall recipe is well-supported.** ES + a tiny policy + a fast
   deterministic Fossen sim + body-frame relative observations + domain
   randomization is, almost line-for-line, the configuration the literature
   says works for low-dimensional marine control (Salimans 2017; Mania ARS 2018;
   Rajeswaran 2017; Peng 2018; the NTNU gym-auv line). We are not off the map.
2. **The biggest gap is partial observability.** Wind and current are
   unmeasured, yet our policy is a **memoryless MLP**. The dominant result across
   sim-to-real robotics (Peng 2018; RMA, Kumar 2021; Lee 2020; Heess 2015;
   Neural-Fly, O'Connell 2022) is that a **recurrent or short observation-history
   policy implicitly identifies the disturbance online**, and a memoryless policy
   cannot. This is our highest-leverage change.
3. **The science backs our single-thruster energy finding — but only the
   *conditional* version.** Penalizing actuation in a quadratic-style reward
   provably/empirically biases the steady-state hold off-target (Ng–Harada–Russell
   1999; Wang 2024 steady-state-error), and it is worse for underactuated craft
   that cannot null all disturbance directions cheaply. **However**, the claim
   "energy must therefore stay high" is *false* in general: weather-optimal
   positioning (Fossen & Strand 2001) and dead-band/area-keeping (Navico virtual-
   anchor patent; energy-aware DP) reduce energy substantially by *changing the
   objective* (weathervane heading + tolerance zone) rather than by penalizing
   thrust against a hard setpoint.
4. **Evaluation has no classical baseline and no sim-to-real check.** We log
   within-%/mean-dist/energy on a held-out set (good), but we never score the
   existing PID `AnchorHoldMode` on the *same* scenarios, and never validate the
   dt=0.1→0.05 physics-equivalence or any reality gap. Both are cheap to add and
   are the standard the DP-RL literature compares against (PID and NMPC).
5. **A couple of concrete code risks**: the Adam bias-correction counter resets
   on resume (transient over-large steps after every restart), and the action-rate
   is not penalized even though `prev_action` is already in the observation —
   action-rate penalties are the literature's preferred way to get smoothness
   *without* the accuracy hit of an energy penalty (CAPS, Mysore 2021).

**Top 5 recommendations (prioritized).**

1. **Add memory to infer the disturbance.** Either (a) a small GRU/LSTM over the
   existing observation, or (b) the cheap, ES-friendly middle ground: stack a
   short observation history (e.g. last 4–8 steps) and/or feed an explicit
   running drift estimate (we *already compute one* in `AnchorHoldMode._update_drift`
   — feed that signal in). Expect better holds in wind/current and a likely
   energy reduction because the policy can anticipate rather than chase.
   (Peng 2018 [V]; Kumar 2021 RMA [V]; Heess 2015 [V]; Neural-Fly 2022 [V].)
2. **Add an action-rate (smoothness) penalty, not a bigger energy penalty.**
   `prev_action` is in the obs, so `λ·‖aₜ − aₜ₋₁‖²` is free to add. This is the
   literature-blessed route to lower thrust/servo wear that does *not* bias the
   steady-state hold (CAPS, Mysore 2021 [V]) — directly addressing the failure
   mode we already observed with energy penalties.
3. **Benchmark against the PID `AnchorHoldMode` (and ideally a small NMPC) on the
   identical validation scenarios.** This is the missing baseline every DP-RL
   paper includes (PID is the incumbent; NMPC the strong one). Without it we
   can't claim the learned policy is better — only that it holds ~80% / ~6 m.
4. **Test weather-optimal / dead-band behaviour as an explicit lever for energy.**
   The single-thruster energy cost is reducible by letting the bow weathervane
   into the resultant load and idling inside a tolerance band (Fossen & Strand
   2001 [S]; Wang 2022 underactuated area-keeping [S]; Navico patent US10809725B2
   [V]). Try a reward/observation that *encourages* settling bow-to-disturbance
   rather than penalizing thrust, and compare energy.
5. **Harden the ES/eval protocol:** fix the Adam bias-correction-on-resume bug;
   raise `K_TRAIN` (6 scenarios/candidate is high-variance for a 9-m/s-wide
   randomization); add the dt=0.1↔0.05 physics-equivalence check the env
   docstring promises; and consider ADR-style curriculum widening rather than
   day-one full-width randomization (Akkaya 2019 [V]; Mehta 2019 [V]).

---

## 2. Annotated literature review

### 2.1 RL / ES for marine vehicle control (DP, station-keeping, tracking)

**Dynamic positioning / station-keeping with deep RL.**

- **Øvereng, Nguyen & Hamre (2021), "Dynamic Positioning using Deep
  Reinforcement Learning," *Ocean Engineering* 235:109433.** [S]
  https://www.sciencedirect.com/science/article/pii/S0029801821008398 — PPO agent
  that jointly does motion control *and* thrust allocation on a digital twin with
  no prior dynamics; a multivariate-Gaussian reward smooths the objective.
  Performance competitive with classical DP. The closest "DP-via-RL" analogue to
  our task, and the template reward structure (position error + small-actuation).
- **DRL controller for DP of a USV (2023), *Computers & Electrical
  Engineering*.** [S] https://doi.org/10.1016/j.compeleceng.2023.108858 — a
  second independent DP-via-RL data point; reports improved positioning vs
  conventional control (open the DOI to confirm exact authors/margins).
- **Enhancing USV Station Keeping via Improved Maximum-Entropy DRL (2024), IEEE
  ICUS.** [S] https://ieeexplore.ieee.org/document/10839758/ — an improved SAC
  ("ISAC") for USV station-keeping; relevant for the exploration-vs-holding angle.
- **Anderlini, Parker & Thomas (2019), "Docking Control of an AUV Using RL,"
  *Applied Sciences* 9(17):3456.** [V] https://www.mdpi.com/2076-3417/9/17/3456 —
  DDPG vs DQN for precise AUV docking (a position/heading-hold task); learned
  policies ~5 orders of magnitude faster at deployment than optimal control.

**Tracking / path-following (the adjacent, larger marine-RL literature).**

- **Martinsen & Lekkas (2018), "Straight-Path Following for Underactuated Marine
  Vessels using Deep RL," IFAC-PapersOnLine 51(29).** [S]
  https://doi.org/10.1016/j.ifacol.2018.09.502 — DDPG beats line-of-sight
  guidance; foundational underactuated-vessel RL paper. Curved-path extension via
  transfer learning across three hulls (OCEANS 2018, DOI 10.1109/OCEANS.2018.8604829) [S].
- **Wang et al. (2023), "DRL-Based Tracking Control of an ASV in Natural Waters,"
  ICRA 2023.** [V] https://arxiv.org/abs/2302.08100 — trained in sim with wind,
  waves, currents and non-ideal actuators; transfers to a **real river** with
  **35.5% lower tracking error than nonlinear MPC**. The strongest real-water RL
  result and a model for our sim-to-real and baseline plan.
- **Zhang, Pan & Reppa (2020), "Model-Reference RL Control of ASVs with
  Uncertainties," arXiv:2003.13839.** [V] https://arxiv.org/abs/2003.13839 — RL
  compensates a nominal-model baseline controller, with stability guarantees: the
  "RL + classical" pattern.
- **Carlucho et al. (2018), "Adaptive low-level control of AUVs using deep RL,"
  *Robotics and Autonomous Systems* 107:71–86.** [S]
  https://www.sciencedirect.com/science/article/abs/pii/S0921889018301519 — DDPG
  learns low-level thruster control directly from interaction.

**Fossen-style 3-DOF RL environments (closest to ours).**

- **Meyer et al. (2019), "Taming an ASV … using deep RL," arXiv:1912.08578;
  code: gym-auv.** [V] https://arxiv.org/abs/1912.08578,
  https://github.com/EivMeyer/gym-auv — a **3-DOF surge/sway/yaw maneuvering-
  model Gym env** with PPO; the canonical open Fossen-style RL environment, very
  close to our `AnchorEnv`.
- **Larsen et al. (2021), "Comparing DRL Algorithms' Ability to Safely Navigate
  …," *Frontiers in Robotics and AI* 8.** [S]
  https://www.frontiersin.org/articles/10.3389/frobt.2021.738113/full —
  head-to-head PPO/DDPG/TD3 on the gym-auv 3-DOF env.
- **Havenstrøm, Rasheed & San (2020/21), "DRL Controller for 3D Path Following
  and Collision Avoidance by AUVs," *Frontiers* 7:566037 / arXiv:2006.09792.** [V]
  https://arxiv.org/abs/2006.09792 — **curriculum learning** with PPO and a
  body-frame line-of-sight relative-error observation (directly relevant to both
  our observation design and a possible curriculum).

**Evolution Strategies / neuroevolution (our training method).**

- **Salimans, Ho, Chen, Sidor & Sutskever (2017), "Evolution Strategies as a
  Scalable Alternative to RL," arXiv:1703.03864.** [V]
  https://arxiv.org/abs/1703.03864 — the OpenAI-ES paper we implement: antithetic
  perturbations, rank normalisation, common-random-numbers, near-linear
  parallelism; invariant to action frequency and delayed reward. Validates our
  exact algorithm choice for a fast deterministic sim.
- **Such et al. (2017), "Deep Neuroevolution," arXiv:1712.06567.** [V]
  https://arxiv.org/abs/1712.06567 — gradient-free GAs competitive with DQN/A3C/ES
  on Atari/locomotion; "following the gradient is not always best."
- **Lehman et al. (2018), "ES Is More Than Just a Finite-Difference
  Approximator," arXiv:1712.06568.** [V] https://arxiv.org/abs/1712.06568 — ES
  optimises expected reward over a *population*, so it finds parameter regions
  robust to perturbation; ES policies are more robust to parameter noise than
  policy-gradient ones. Useful for deployment robustness of a tiny policy.
- **Conti et al. (2018), NS-ES/NSR-ES, NeurIPS 2018, arXiv:1712.06560.** [V]
  https://arxiv.org/abs/1712.06560 — plain ES can stick in local optima on
  *deceptive/sparse* rewards; our dense smooth reward largely avoids that regime.
- **Hansen (2016), "The CMA Evolution Strategy: A Tutorial," arXiv:1604.00772.**
  [V] https://arxiv.org/abs/1604.00772 — the standard alternative ES; relevant if
  OpenAI-ES plateaus (CMA-ES adapts a full covariance, good for ~650 params).
- **Pagliuca, Milano & Nolfi (2019), arXiv:1912.05239.** [V]
  https://arxiv.org/abs/1912.05239 — OpenAI-ES "outperforms or equals" other
  neuro-ES on continuous control; **caveat: rewards tuned for gradient RL are not
  necessarily good for ES**, so fair comparisons must tune the reward per method.

### 2.2 Reward shaping & the energy-vs-accuracy tradeoff

- **Ng, Harada & Russell (1999), "Policy Invariance Under Reward
  Transformations," ICML 1999.** [S]
  https://people.eecs.berkeley.edu/~russell/papers/icml99-shaping.pdf — the
  theory: only *potential-based* shaping `F = γΦ(s′) − Φ(s)` leaves the optimal
  policy unchanged. **Our distance term ≈ a potential** (pull toward the anchor —
  benign), but our **energy and watch-circle terms are NOT potential-based**:
  they intentionally change the objective, so by this theorem they *do* shift the
  optimum (toward less thrust). That is exactly the mechanism by which a strong
  energy penalty degrades the hold.
- **Wang, Zheng & Lin (2024), "Steady-State Error Compensation for RL with
  Quadratic Rewards," arXiv:2402.09075.** [V] https://arxiv.org/abs/2402.09075 —
  the cleanest mechanistic result: **quadratic rewards penalizing control effort
  produce significant steady-state error** (the effort term biases the policy off
  the setpoint). Direct theoretical support for our empirical finding.
- **Mysore et al. (2021), "Regularizing Action Policies for Smooth Control"
  (CAPS), ICRA 2021, arXiv:2012.06644.** [V] https://arxiv.org/abs/2012.06644 —
  temporal+spatial **smoothness regularization** cut real-quadrotor power ~80%
  while staying flight-worthy. Key lesson for us: get smoothness/lower actuation
  via an **action-rate regularizer**, not a heavy energy reward term — avoids the
  accuracy hit.
- **Boré et al. (2025), "Toward 6-DOF AUV Energy-Aware Position Control based on
  Deep RL," arXiv:2502.17742.** [V] https://arxiv.org/abs/2502.17742 — an
  energy-aware reward yields **~30% less power at slightly lower positioning
  performance**: a quantified, *modest* tradeoff when weighted carefully (not the
  catastrophic collapse we saw with aggressive weights).
- **Energy-efficient ("Green") NMPC for DP (2022), *Applied Ocean Research*.** [S]
  https://www.sciencedirect.com/science/article/abs/pii/S0141118722002632 — up to
  **50% sway-thruster-demand reduction** by allowing drift inside a safe zone
  (dead-band/region DP) rather than tight holding. Energy is saved by *relaxing
  the position constraint*, not by penalizing thrust against a hard setpoint.
- **Nguyen & Sørensen (2009), "Setpoint Chasing for Thruster-Assisted Position
  Mooring," IEEE TCST.** [S] https://ieeexplore.ieee.org/document/5308756/ —
  choose the setpoint at the equilibrium where mean environmental load balances,
  so thrusters only damp oscillation; a DDPG variant *learns* this setpoint
  (J. Marine Sci. Technol. 2019, https://link.springer.com/article/10.1007/s00773-019-00678-5) [S].
- **Event-triggered / energy-based RL for USVs (2025), *Ocean Engineering*.** [S]
  https://www.sciencedirect.com/science/article/abs/pii/S0029801825008455 — an
  integral dynamic-threshold event trigger avoids excessive actuator updates and
  conserves energy while maintaining tracking (the dead-band/event-trigger idea
  applied to a learned controller).

### 2.3 Underactuated / single-thruster station-keeping

- **Pettersen & Egeland (1997), "Exponential Stabilization of an Underactuated
  Surface Vessel," IEEE CDC / MIC 18(3).** [V]
  https://www.mic-journal.no/ABS/MIC-1997-3-3.asp/ — the foundational hardness
  result: a 3-DOF surface vessel with only surge+yaw actuation (no sway) **cannot
  be asymptotically stabilized to a point by any time-invariant feedback**
  (Brockett's condition fails); the escape is *periodic time-varying* feedback.
  A trolling motor whose steering authority ∝ thrust is exactly this
  underactuated class — so to re-point toward the spot it must generally *move*.
- **Mazenc, Pettersen & Nijmeijer (2002), IEEE TAC 47(10).** [S]
  https://www.researchgate.net/publication/3024517 — smooth *time-varying*
  feedback giving global stabilization; confirms the route around Brockett is
  time-variation, not extra actuators.
- **Aicardi et al. (1995), "Closed-loop Steering of Unicycle-like Vehicles via
  Lyapunov Techniques," IEEE R&A Magazine 2(1).** [S]
  https://www.semanticscholar.org/paper/b5c4d7c95794569664068729382a837ce1ced09d
  — the kinematic analogue: forward thrust + steering angle, smooth global
  steering-to-a-pose. The "drive-and-turn toward the spot" controller class
  (consistent with "must keep moving").
- **Aguiar & Hespanha (2003), "Position Tracking for a Nonlinear Underactuated
  Hovercraft," IEEE CDC.** [V]
  https://web.ece.ucsb.edu/~hespanha/published/CDC03-1567.pdf — a hovercraft
  (one body-fixed thrust direction + yaw) is the closest analogue to a single
  steerable thruster; position *tracking* is tractable where pure set-point
  stabilization is obstructed.
- **Fossen & Strand (2001), "Nonlinear Passive Weather Optimal Positioning
  Control (WOPC) … Experimental Results," *Automatica* 37(5):701–715.** [S]
  https://www.sciencedirect.com/science/article/abs/pii/S0005109801000061 — **the
  key energy result.** Let heading rotate so the bow aligns with the resultant
  environmental force; the lateral disturbance vanishes in the body frame and the
  station-keeping force is minimized (pendulum analogy). Original is fully
  actuated; later work extends to sway-unactuated craft.
- **Wang et al. (2022), "Weather Optimal Area-Keeping Control for Underactuated
  ASV with Input Time-Delay," *Ocean Engineering*.** [S]
  https://www.researchgate.net/publication/360405970 — applies weather-optimal +
  *area* (dead-band) holding to an **underactuated** ASV explicitly to **reduce
  energy**. Direct evidence the single-thruster energy cost is reducible.
- **Walters, Kamalapurkar, … Dixon (2017), "Online Approximate Optimal Station
  Keeping of a Marine Craft in the Presence of a Current," arXiv:1710.10511.** [V]
  https://arxiv.org/abs/1710.10511 — ADP controller minimizing station error vs
  control effort under an unknown current; station-keeping = minimize the energy
  to counter the **net** disturbance (not a fixed large thrust).
- **Commercial GPS spot-lock (patents).**
  - **Johnson Outdoors / Minn Kota "Spot-Lock," US 9,132,900 B2 (2015).** [V]
    https://patents.google.com/patent/US9132900B2/en — GPS + compass + control
    module **continuously** adjust heading and power to hold the waypoint
    (drive-back; no idle/weathervane equilibrium described). The high-energy
    baseline our policy currently resembles.
  - **Navico "virtual anchor / position lock," US 10,809,725 B2 (2020).** [V]
    https://patents.google.com/patent/US10809725B2/en — uses an explicit
    **threshold distance / dead-band** (e.g. 5–10 ft); the motor idles inside the
    tolerance circle and only acts on excursions — the dead-band strategy, which
    already cuts duty cycle vs exact-point holding.

### 2.4 Domain randomization, sim-to-real, partial observability

- **Tobin et al. (2017), "Domain Randomization …," IROS 2017, arXiv:1703.06907.**
  [V] https://arxiv.org/abs/1703.06907 — the founding DR paper: randomize so
  broadly that reality looks like just another variation.
- **Peng et al. (2018), "Sim-to-Real Transfer … with Dynamics Randomization,"
  ICRA 2018, arXiv:1710.06537.** [V] https://arxiv.org/abs/1710.06537 — the key
  dynamics-randomization paper: randomize mass/friction/damping **and use a
  recurrent (LSTM) policy that performs implicit online system ID** from the
  history of states/actions. Directly relevant to our randomized mass/hull/motor.
- **Akkaya et al. (2019), "Solving Rubik's Cube …" (Automatic Domain
  Randomization), arXiv:1910.07113.** [V] https://arxiv.org/abs/1910.07113 — ADR
  **auto-widens each parameter range when the policy crosses a threshold** — a
  curriculum that grows breadth only as fast as the policy can absorb it; memory
  policies show emergent meta-learning.
- **Mehta et al. (2019), "Active Domain Randomization," arXiv:1904.04762.** [V]
  https://arxiv.org/abs/1904.04762 — the caution: naive uniform sampling over
  wide ranges yields **suboptimal, high-variance** policies; concentrate on
  informative instances. Relevant to our day-one full-width randomization.
- **Kumar et al. (2021), "RMA: Rapid Motor Adaptation," RSS 2021,
  arXiv:2107.04034.** [V] https://arxiv.org/abs/2107.04034 — privileged teacher
  encodes env factors (friction, payload, motor strength) into a latent; an
  adaptation module **regresses that latent online from a history of recent
  states/actions**. A memoryless MLP on instantaneous state cannot recover these.
  The architecture to emulate for unmeasured wind/current.
- **Heess et al. (2015), "Memory-based Control with Recurrent Neural Networks,"
  arXiv:1512.04455.** [V] https://arxiv.org/abs/1512.04455 — recurrent continuous-
  control policies handle "short-term integration of information from noisy
  sensors and the **identification of system parameters**" — the clearest early
  statement that a recurrent policy does implicit online system ID an MLP cannot.
- **Hausknecht & Stone (2015), "Deep Recurrent Q-Learning for POMDPs,"
  arXiv:1507.06527.** [V] https://arxiv.org/abs/1507.06527 — an LSTM is at least
  as good as, and degrades more gracefully than, **stacked frames**; the
  principled justification for our cheap "stack a short history" fallback.
- **O'Connell, Shi et al. (2022), "Neural-Fly Enables Rapid Learning for Agile
  Flight in Strong Winds," *Science Robotics*, arXiv:2205.06908.** [V]
  https://arxiv.org/abs/2205.06908 — the closest wind/current analogue: learn a
  wind-invariant representation, then **adapt a few linear coefficients online to
  estimate the current wind residual force** from recent data rather than sensing
  it. Cm-level tracking up to 12 m/s wind, beating L1-adaptive/INDI.
- **Body-frame / egocentric observation design.** Kwiatkowski et al. (2022,
  arXiv:2209.09344) [V] find **egocentric (body-frame) observations** yield more
  efficient navigation than world-frame; Havenstrøm 2020 (above) uses exactly a
  body-frame LOS error vector. This **confirms our body-frame observation choice
  is right.**

### 2.5 ES vs gradient RL, tiny/embedded policies, classical baselines

- **Mania, Guy & Recht (2018), "Simple random search … competitive for RL" (ARS),
  NeurIPS 2018, arXiv:1803.07055.** [V] https://arxiv.org/abs/1803.07055 — a
  derivative-free random search training **static linear policies** matches SOTA
  sample efficiency on MuJoCo and is ~15× faster. Strong support that **(a) ES is
  competitive with PPO/SAC and (b) tiny/linear policies suffice** — our regime.
- **Rajeswaran et al. (2017), "Towards Generalization and Simplicity in
  Continuous Control," NeurIPS 2017, arXiv:1703.02660.** [V]
  https://arxiv.org/abs/1703.02660 — linear/RBF policies solve standard
  continuous-control benchmarks competitively; **warns that narrow initial-state
  distributions cause overfit, trajectory-centric policies** (use diverse
  initialization). Validates both our tiny net and our wide start-condition
  randomization.
- **RLtools / "TinyRL" (Eschmann et al. 2023, arXiv:2306.03530).** [V]
  https://arxiv.org/abs/2306.03530 — first training of a deep-RL controller
  **directly on a microcontroller**; confirms small continuous-control policies
  run on embedded hardware (our Raspberry-Pi target is comfortable).
- **Rusu et al. (2016), "Policy Distillation," ICLR 2016, arXiv:1511.06295.** [V]
  https://arxiv.org/abs/1511.06295 — ~15× compression with no perf loss: a
  fallback if we ever need to train large and deploy tiny.
- **Classical baselines.** PID DP is the incumbent comparator (RL usually beats
  fixed-gain PID; often "RL tunes a PID," e.g. *Ocean Engineering* 2020
  https://doi.org/10.1016/j.oceaneng.2020.108053 [S]). NMPC is the strong
  baseline (Cai et al. 2021, arXiv:2106.08634 [V],
  https://arxiv.org/abs/2106.08634; energy-aware "Green NMPC" above). Backstepping
  / sliding-mode (Fossen lineage, https://www.fossen.biz/publications/ [V]) are
  the Lyapunov-stable nonlinear baselines. Net: **benchmark against a tuned PID
  and, ideally, an NMPC**; MPC tends to win on guarantees/constraints, RL on
  robustness-to-model-error and runtime compute.
- **Batista et al. (2025), "Evaluating Robustness of Deep RL for ASV Control in
  Field Tests," arXiv:2505.10033.** [V] https://arxiv.org/abs/2505.10033 — DRL
  trained with domain randomization, benchmarked **against MPC** on a real ASV
  under asymmetric drag / off-center payload; stays reliable. A model for our
  evaluation plan.

---

## 3. Point-by-point audit of our approach vs the literature

### 3.1 Observation design — mostly right, one important gap

**What we do (`env._obs`).** Body-frame anchor position error (fwd/lat), boat
velocity over ground (fwd/lat), yaw rate, previous action (2), and distance.
Sensibly normalised.

**Verdict.**
- **Right:** Body-frame / egocentric encoding is exactly what the literature
  recommends for heading-invariant control (Kwiatkowski 2022 [V]; Havenstrøm 2020
  [V]). Including `prev_action` is good practice and, importantly, it *enables* an
  action-rate penalty for free. Velocity-over-ground in body frame gives the
  policy the closing-speed signal a PD "kd" term would use.
- **Gap (high priority):** **wind and current are unobserved and there is no
  memory.** A memoryless MLP sees only the *instantaneous* state, so it cannot
  distinguish "I'm drifting because of a 6 m/s beam wind" from "I'm drifting
  because I'm coasting" — it can only react after the position error appears. The
  dominant sim-to-real result (Peng 2018 [V]; RMA 2021 [V]; Heess 2015 [V];
  Neural-Fly 2022 [V]) is that a **recurrent or history-conditioned** policy
  *infers the latent disturbance online* and rejects it feed-forward. This is
  almost certainly why our hold "drives back" rather than "leans into the wind."
- **Note:** ground-frame velocity already leaks *some* disturbance information
  (drift shows up as velocity), so the MLP is not blind — but it gets the
  disturbance only after it has acted on the boat, never ahead of time.

**Recommendations.**
- (Best) Replace the MLP with a small **GRU** over the same 8-d observation
  (ES handles recurrence fine — it's just more parameters in the flat vector;
  roll the hidden state inside `_rollout`). Expect better wind/current holds and
  likely lower energy from anticipation.
- (Cheapest, ES-friendly) **Stack a short observation history** (last 4–8 steps)
  or feed an **explicit running drift estimate**. We *already compute one* in
  `AnchorHoldMode._update_drift` (GPS velocity minus our own propulsion,
  low-passed) — surfacing that as two extra observation channels is a small,
  high-value change and is directly the Neural-Fly idea (estimate the residual
  disturbance, feed it forward).

### 3.2 Reward design — the energy/holding tradeoff

**What we do.** `reward = -dist - 0.08·thrust² - 0.6·(dist > 5 m)`.

**Verdict.**
- **Right:** The `-dist` linear pull is effectively a potential-based shaping
  term (Ng 1999 [V]) — it does the job without distorting the optimum. The watch-
  circle penalty matches the **region/zone reward** that real station-keeping
  systems use (balloon Loon work; energy-aware DP zones). A *light* energy term
  (0.08) is consistent with Øvereng 2021's "position + small actuation."
- **Confirmed by literature — our energy finding is real (conditionally):**
  Wang 2024 [V] shows quadratic effort penalties **bias the steady-state hold
  off-target**, and Ng 1999 [V] shows a non-potential energy term **provably
  shifts the optimum** toward less thrust. So a *strong* energy weight predictably
  wrecks the hold — exactly what we observed (57% / 15 m vs 80% / 6 m). This is
  worse for an underactuated craft (Pettersen 1997 [V]) that cannot null all
  disturbance directions cheaply.
- **But we may be leaving energy on the table.** The literature reduces energy by
  *reformulating the objective*, not by penalizing thrust: weathervaning (Fossen
  & Strand 2001 [S]) and dead-band/area-keeping (Navico patent [V]; Green NMPC
  [S], ~50% sway-demand cut). Our reward penalizes thrust against a hard `dist`
  target, which is the very thing that biases the hold — whereas a tolerance band
  + a reward that *doesn't* punish a steady bow-to-wind thrust would save energy
  without fighting the hold.

**Recommendations.**
1. **Add an action-rate penalty instead of a bigger energy penalty:**
   `−λ·‖aₜ − aₜ₋₁‖²` (we already have `prev_action`). CAPS (Mysore 2021 [V]) got
   ~80% power reduction on real hardware this way *without* hurting accuracy —
   directly the smoothness/wear win we want, minus the steady-state bias.
2. **Try a dead-band in the reward** (no `-dist` penalty inside, say, 1–2 m,
   matching the PID's `idle_deadband_m = 0.8`): rewards the policy for *settling*
   rather than hunting the exact point, the dead-band energy mechanism (Navico
   [V]; Green NMPC [S]). Our env even has a `deadband_m=1.0` field that is
   currently **unused in the reward** — wire it in and A/B it.
3. **Keep the energy weight light** (the Boré 2025 [V] ~30%-for-slightly-worse
   point is the realistic frontier); do not chase aggressive weights.

### 3.3 Action representation & smoothing

**What we do.** Direct `[thrust, steering]` in [−1,1] via tanh, clipped; no rate
limit, no smoothing, `prev_action` observed but not penalized.

**Verdict / recommendations.** Direct continuous action is standard. The missing
piece is **smoothness** — unregularized RL exploits high-frequency action
chatter (Mysore 2021 [V]), which on a real servo means wear and on a real motor
means wasted energy. Add the action-rate penalty (3.2 #1). Optionally consider
that on hardware the steering servo has a slew rate; modelling that (or rate-
limiting the action) would shrink the sim-to-real gap.

### 3.4 Domain-randomization breadth

**What we do (`scenarios.py`).** Wind 0–9 m/s (20% calm), current 0–1.2 m/s,
gusts (OU), slow weather wander, mass 200–400 kg, hull_tracking 0.35–2.5, mount
bow/stern/center, max_thrust 210–300 N, start 0–12 m / any bearing / any heading
/ initial drift. Deterministic per seed; CRN per generation; held-out validation.

**Verdict.**
- **Right:** This is genuinely good DR — randomizing **vehicle dynamics**
  (mass/hull/mount/motor), not just the environment, is exactly Peng 2018 [V] and
  is what makes a policy robust across the fleet. Capping wind at 9 m/s to drop
  unwinnable scenarios is sensible (they only add gradient noise and cap metrics).
  Diverse start conditions directly address Rajeswaran 2017's [V] overfit warning.
  CRN + held-out validation is textbook.
- **Caution (medium priority):** We randomize **full width from generation 0**.
  Mehta 2019 [V] shows wide uniform DR yields suboptimal, high-variance policies;
  Akkaya 2019 [V] (ADR) and Havenstrøm 2020 [V] (curriculum) both **grow
  difficulty as competence grows**. A curriculum (start calm/near, widen wind +
  start distance as within-% rises) would likely reach a better optimum faster
  and reduce the variance that 6-scenario batches already suffer from.
- **Interaction with §3.1:** very broad dynamics randomization is precisely the
  case where a memoryless policy underperforms a recurrent one — the policy must
  hedge across all masses/hulls it can't identify, whereas a recurrent policy
  adapts. Wide DR *raises the value* of adding memory.

### 3.5 The ES setup

**What we do.** OpenAI-ES: pop 48 antithetic (96 rollouts/gen), σ=0.1, Adam
lr=0.02, rank-normalised utilities, mild L2 (0.001) on grad and weight-decay,
CRN per generation, `K_TRAIN=6` scenarios/candidate.

**Verdict.**
- **Right:** Faithful OpenAI-ES (Salimans 2017 [V]) — antithetic sampling, centred
  ranks, CRN, Adam ascent. Exactly the method the literature endorses for a fast
  deterministic sim + tiny policy. σ=0.1 and lr=0.02 are within the paper's
  typical ranges.
- **Bug (high priority): Adam bias-correction resets on resume.** In `main`,
  `mhat = m_adam / (1 - b1**(gen - start_gen + 1))`. On a fresh start
  `start_gen=0`, fine. **On resume, `m_adam`/`v_adam` are loaded (large,
  well-warmed) but the bias-correction exponent restarts from 1**, so
  `(1 - b1**1) = 0.1` and the first post-resume step divides the (already
  unbiased) moments by ~0.1 — a ~10× over-large step, then ~3×, etc., for several
  generations. Since the env is checkpointed every 5 gens and "stop/resume" is
  advertised, this fires often and can perturb a converged policy. **Fix:** store
  a monotonic Adam timestep `t` in the checkpoint and use `b1**t` / `b2**t`
  (independent of `start_gen`).
- **Variance (medium):** `K_TRAIN=6` scenarios per candidate against a 0–9 m/s
  wind × mass × hull × mount randomization is a **high-variance fitness estimate**
  — two candidates can be ranked by luck of the 6 draws. CRN mitigates *relative*
  comparison within a generation (good), but the gradient is still noisy. Raising
  K (12–24) or using more antithetic pairs would steady the gradient; the env is
  cheap, so this is mostly a throughput tradeoff.
- **Minor:** the L2 `grad -= 0.001*theta` plus Adam is fine; just note Adam +
  decoupled weight decay (AdamW-style) is cleaner than mixing L2 into the grad,
  though immaterial at this scale.
- **Optional:** if OpenAI-ES plateaus, CMA-ES (Hansen 2016 [V]) adapts a full
  covariance and often does better at ~650 params; or a novelty/ERL hybrid
  (Conti 2018 [V]; Khadka 2018) if exploration stalls — though our dense reward
  makes this unlikely to be needed.

### 3.6 Evaluation protocol

**What we do.** Held-out 64-scenario validation; log val_return, within-%,
mean_dist, energy; "best-so-far" checkpoint by val_return; "second-half =
steady state" metric.

**Verdict.**
- **Right:** Held-out set, interpretable metrics, best-by-validation checkpoint,
  steady-state windowing — all good and better than many papers.
- **Missing (high priority): no classical baseline.** Every DP-RL paper scores
  PID and/or NMPC on the **same** task (Øvereng 2021; Wang 2023; Batista 2025).
  We have a tuned PID `AnchorHoldMode` right there — **run it on the identical
  64-scenario validation batch** and report within-%/mean-dist/energy side by
  side. Without it, "80% / 6 m" is unanchored. (Note: `AnchorHoldMode` consumes a
  `NavigationState`/GPS fix, not the env's body-frame obs, so this needs a thin
  adapter that drives the sim from `AnchorHoldMode.update` — straightforward but
  not free.)
- **Missing (medium): no sim-to-real / dt check.** The env docstring promises
  "validate with eval.py --dt 0.05," but training and metrics both run at dt=0.1.
  Add an explicit eval that re-scores the best policy at dt=0.05 to confirm the
  physics-equivalence claim; otherwise we may be optimising a slightly different
  dynamical system than we deploy. A real reality-gap test (sensor noise,
  steering slew, GPS latency) would follow the Batista 2025 [V] template.
- **Metric nuance:** `within_pct` uses radius=5 m; the policy reaches ~6 m mean
  in the second half, so within-% and mean-dist can move oppositely under
  reshaping — report both (we do) and add a P95/max-excursion to catch tail
  blow-outs the mean hides.

### 3.7 Partial observability — feed-forward MLP vs recurrent

Covered in §3.1 and §2.4: **this is the single clearest "the literature says do
it differently" item.** Unmeasured wind/current is a textbook POMDP; the field's
answer is memory (recurrent / history / online estimator). Our memoryless MLP is
the one place we diverge from best practice on a point the literature is
near-unanimous about. Lowest-risk first step: feed the existing
`_update_drift`-style estimate as observations; highest-ceiling step: a small GRU.

---

## 4. Does the science back our single-thruster energy finding?

**Our finding.** With a single *vectored* thruster the policy holds by driving
back to the spot (it cannot idle), so energy/thrust stays high; aggressive
energy-penalty shaping consistently wrecked the hold (~57% / 15 m) while the
holding-first reward reaches ~80% / ~6 m. We concluded the energy cost is largely
inherent to single-thruster station-keeping.

**Verdict: the science backs the *conditional* claim, and refines the strong
one.**

- **Yes — aggressive energy penalties degrade the hold, and more so for
  underactuated craft.** This is both *theoretically expected* (an energy term is
  non-potential-based, so it provably shifts the optimum toward less thrust —
  Ng–Harada–Russell 1999 [V]) and *empirically reproduced* (quadratic effort
  penalties cause steady-state error — Wang 2024 [V]; energy-aware AUV trades ~30%
  power for slightly worse positioning — Boré 2025 [V]). Underactuation makes it
  worse: a 3-DOF craft with no sway actuation cannot be stabilized to a point by
  static feedback at all (Pettersen 1997 [V]) and must *move* to reorient, so
  thrust is structurally coupled to holding. Our 57%/15-m collapse under heavy
  energy weighting is exactly what this predicts.

- **But "energy must stay high" is *not* a law — it's a property of the *strategy*
  (drive-back-to-point), not of single-thruster physics.** The underactuated
  station-keeping literature reduces energy without extra actuators by changing
  the objective:
  - **Weathervaning / weather-optimal positioning** (Fossen & Strand 2001 [S];
    underactuated area-keeping, Wang 2022 [S]): point the bow into the resultant
    load so the lateral disturbance vanishes in the body frame; in equilibrium the
    required force is *minimized*, and you only fight the net along-axis load.
  - **Dead-band / area-keeping** (Navico virtual-anchor patent US10809725B2 [V];
    Green NMPC ~50% sway-demand cut [S]; setpoint-chasing, Nguyen & Sørensen 2009
    [S]): idle inside a tolerance circle and only act on excursions; fight only
    the **net** disturbance (Walters/Dixon 2017 [V]).
  - Tellingly, commercial systems span this spectrum: **Minn Kota Spot-Lock**
    (patent US9132900B2 [V]) describes the high-energy *continuous drive-back* our
    policy currently resembles, while **Navico** (US10809725B2 [V]) uses the
    lower-energy *dead-band*.

- **Net.** A single vectored thruster genuinely *cannot* hold an exact pose with
  zero/constant idle thrust (the Brockett/nonholonomic obstruction is real), so a
  *floor* of motion-based correction is inherent — our finding is correct that
  far. But the *magnitude* of the energy cost is substantially reducible by
  (a) weathervaning to a bow-into-disturbance heading and (b) a tolerance
  dead-band, neither of which our current reward encourages. The right framing for
  the report: **"high energy is inherent to *naive drive-back* station-keeping, not to
  single-thruster station-keeping."** The fact that our *energy penalty* failed is
  predicted by theory; the conclusion to draw is not "give up on energy" but
  "save energy by reformulating the task (weathervane + dead-band), and get
  smoothness via an action-rate regularizer — not by penalizing thrust against a
  hard setpoint."

---

## 5. Concrete risks / bugs flagged

1. **[High] Adam bias-correction resets on resume** (`train.py`, the
   `mhat/vhat` correction uses `gen - start_gen + 1`). After every resume the
   first few steps are ~10×/3×/… too large because warmed moments are divided by a
   tiny correction factor. Fix: persist a monotonic Adam step `t` and use it.
2. **[Medium] `deadband_m` is dead config.** `AnchorEnv.__init__` takes
   `deadband_m=1.0` but the reward (`env.py` line ~129) never uses it. Either wire
   it into the reward (a holding dead-band, per §3.2) or remove it to avoid the
   impression that a dead-band is active.
3. **[Medium] High-variance fitness from `K_TRAIN=6`** across a very wide
   randomization — noisy gradient; raise K or pairs.
4. **[Medium] No baseline / no dt-equivalence eval** — can't substantiate "better
   than PID," and the dt=0.1→0.05 claim is asserted, not tested.
5. **[Low] Memoryless policy under unmeasured disturbance + wide DR** — not a bug,
   but the architecture the literature most consistently advises against for this
   exact POMDP (see §3.1/§3.7).
6. **[Low] Reward uses a hard-coded `radius_m` (5 m) watch circle** while the
   metric also uses 5 m — fine, but if the deployed `anchor_radius_m` differs, the
   policy was trained to a fixed circle; consider passing the radius as an
   observation if the product exposes a variable watch circle.

---

*Verification recap.* Strongly-verified anchors for the key claims: Salimans
2017, Mania ARS 2018, Rajeswaran 2017 (ES + tiny policies); Peng 2018, Kumar
2021 RMA, Heess 2015, Neural-Fly 2022 (recurrent/history infers disturbance);
Ng–Harada–Russell 1999, Wang 2024, Mysore 2021 CAPS, Boré 2025 (reward/energy
tradeoff); Pettersen 1997, Aguiar 2003, Walters 2017, and the Minn-Kota/Navico
patents (underactuated single-thruster). Fossen & Strand 2001 (weathervaning) and
the underactuated area-keeping / energy-aware-DP papers are snippet-confirmed
(ScienceDirect 403) but bibliographically consistent across indexers — verify the
DOIs before formal citation.
