"""A deterministic, hardware-free closed-loop test harness.

It wires the real navigator + controller + simulator together and steps them
forward in lockstep with no asyncio and no wall-clock time, so integration
tests are fast and reproducible.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from vanchor.controller.controller import Controller
from vanchor.core.geo import haversine_m
from vanchor.core.models import BoatState, Environment, GeoPoint
from vanchor.core.state import NavigationState
from vanchor.nav.navigator import Navigator
from vanchor.sim.devices import SimCompass, SimGps
from vanchor.sim.simulator import Simulator

STOCKHOLM = GeoPoint(59.3293, 18.0686)


@dataclass
class Harness:
    start: GeoPoint = STOCKHOLM
    environment: Environment = field(default_factory=Environment)
    gps_hz: float = 1.0
    compass_hz: float = 5.0
    control_hz: float = 5.0
    physics_dt: float = 0.05
    model: str = "simple"

    def __post_init__(self) -> None:
        self.sim = Simulator(
            start=BoatState(point=self.start, heading_deg=0.0),
            environment=self.environment,
            model=self.model,
        )
        self.state = NavigationState()
        self.nav = Navigator(self.state, bus=None)
        self.controller = Controller(self.state, self.sim.motor, bus=None)
        self.gps = SimGps(self.sim.truth, bus=None, update_hz=self.gps_hz)
        self.compass = SimCompass(self.sim.truth, bus=None, update_hz=self.compass_hz)
        # Prime perceived state with one fix + heading.
        self.nav.handle_sentence(self.gps.sample(self.sim.truth()))
        self.nav.handle_sentence(self.compass.sample(self.sim.truth()))

    def command(self, cmd: dict) -> None:
        self.controller.handle_command(cmd)

    def run(self, seconds: float) -> list[float]:
        """Advance the whole loop for ``seconds`` of simulated time.

        Returns the per-sample ground-truth distance to the anchor (when one is
        set) so tests can assert on convergence.
        """
        t = 0.0
        next_gps = next_compass = next_ctrl = 0.0
        gps_period = 1.0 / self.gps_hz
        compass_period = 1.0 / self.compass_hz
        ctrl_period = 1.0 / self.control_hz
        distances: list[float] = []

        while t < seconds:
            self.sim.step(self.physics_dt)
            if t >= next_gps:
                self.nav.handle_sentence(self.gps.sample(self.sim.truth()))
                next_gps += gps_period
            if t >= next_compass:
                self.nav.handle_sentence(self.compass.sample(self.sim.truth()))
                next_compass += compass_period
            if t >= next_ctrl:
                self.controller.control_tick(ctrl_period)
                next_ctrl += ctrl_period
            if self.state.anchor is not None:
                distances.append(haversine_m(self.sim.truth().point, self.state.anchor))
            t += self.physics_dt
        return distances
