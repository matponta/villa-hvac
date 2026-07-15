"""Living-room-only #3 v3 steady fan governor."""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import date, datetime, timedelta
import logging

from .const import HOUSE_MODE_HOME, SEASON_SUMMER, WINDOW_ALERT_TARGETS
from .supervisor import _is_free_cooling, fan_lever, switch_lever, temperature_lever

EVALUATION = timedelta(minutes=15)
HISTORY = timedelta(minutes=45)
KITCHEN_WINDOW = timedelta(minutes=10)
DOWN_BLOCK = timedelta(minutes=30)
FAN_CEILING = 70
SHARED_DUTY = 0.80
LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class GovernorDecision:
    state: str
    fan: int | None
    action: str
    reason: str | None = None


def steady_governor_step(
    *, fan: int, floor: int, context: str, error: float,
    duty: float | None, strokes_h: float | None, kitchen_fast_rise: bool,
    stable_evaluations: int,
) -> tuple[int, str, int]:
    """Pure normal-operation 15-minute fan step."""
    floor = max(20, min(FAN_CEILING, floor))
    fan = max(floor, min(FAN_CEILING, fan))
    if kitchen_fast_rise:
        return min(FAN_CEILING, fan + 10), "rialzo rapido cucina: +10%", 0
    if error >= 0.6:
        return min(FAN_CEILING, fan + 20), "recupero comfort: +20%", 0
    if duty is None or strokes_h is None:
        return fan, "dati finestra insufficienti: mantieni", stable_evaluations
    stable = error <= 0.3 and strokes_h <= 3.0
    if context == "SHARED_CALL":
        stable_evaluations = stable_evaluations + 1 if stable else 0
        if stable_evaluations >= 2:
            return max(floor, fan - 10), "PdC già impegnata: -10%", 0
        if duty >= 0.9 and error > 0.3:
            return min(FAN_CEILING, fan + 10), "valvola continua e temperatura sale: +10%", 0
        return fan, "carico condiviso stabile: mantieni", stable_evaluations
    if strokes_h > 3.0:
        return max(floor, fan - 10), "troppi cicli valvola: -10%", 0
    if duty > 0.8:
        return min(FAN_CEILING, fan + 10), "chiamata marginale lunga: +10%", 0
    if duty < 0.5 and stable:
        return max(floor, fan - 10), "chiamata marginale leggera: -10%", 0
    return fan, "finestra marginale in obiettivo: mantieni", stable_evaluations


class SteadyGovernorController:
    """Stateful history adapter around the pure living-room governor law."""

    def __init__(self, hass=None, entry=None) -> None:
        self.hass = hass
        self.entry = entry
        self.samples: deque[tuple[datetime, bool, bool, float | None]] = deque()
        self.fan = 40
        self.last_eval: datetime | None = None
        self.down_block_until: datetime | None = None
        self.stable_evaluations = 0
        self.error_cycles = 0
        self.ceiling_since: datetime | None = None
        self.escalations: deque[datetime] = deque()
        self.escalated_until: datetime | None = None
        self.demoted_day: date | None = None
        self._managed = False
        self._demotion_notified: date | None = None
        self.view: dict = {"state": "NATIVE", "reason": "switch disattivato"}

    def _history(self, state, living) -> tuple[float | None, float | None, str, float | None]:
        now = state.now
        other = any(
            z.zone_id not in {"living_room", "kitchen", "rack"}
            and z.enabled and z.demand is True
            for z in state.zones.values()
        )
        kitchen = state.zones.get("kitchen")
        living_call = living.demand is True or (
            kitchen is not None and kitchen.demand is True
        )
        self.samples.append((now, living_call, other, state.kitchen_ep_temp))
        while self.samples and now - self.samples[0][0] > HISTORY:
            self.samples.popleft()
        if len(self.samples) < 2 or self.samples[-1][0] - self.samples[0][0] < timedelta(minutes=30):
            duty = strokes = other_duty = None
        else:
            duty = sum(s[1] for s in self.samples) / len(self.samples)
            other_duty = sum(s[2] for s in self.samples) / len(self.samples)
            transitions = sum(
                a[1] != b[1] for a, b in zip(self.samples, list(self.samples)[1:])
            )
            hours = (self.samples[-1][0] - self.samples[0][0]).total_seconds() / 3600
            strokes = transitions / hours if hours > 0 else None
        context = "SHARED_CALL" if other_duty is not None and other_duty >= SHARED_DUTY else "MARGINAL_CALL"
        slope = None
        if state.kitchen_ep_fresh and state.kitchen_ep_temp is not None:
            candidates = [
                s for s in self.samples
                if now - s[0] >= KITCHEN_WINDOW and s[3] is not None
            ]
            old = min(
                candidates,
                key=lambda s: abs((now - s[0]) - KITCHEN_WINDOW),
                default=None,
            )
            if old is not None:
                slope = state.kitchen_ep_temp - old[3]
        return duty, strokes, context, slope

    def _release(self, living) -> dict:
        if not self._managed or living is None:
            self._managed = False
            return {}
        self._managed = False
        out = {}
        for fan, manuale in living.fancoil_units:
            out[switch_lever(manuale)] = "off"
            out[fan_lever(fan)] = max(33, self.fan)
        return out

    def _notify_demotion(self, now: datetime) -> None:
        if self.hass is None or self.entry is None or self._demotion_notified == now.date():
            return
        self._demotion_notified = now.date()

        async def send() -> None:
            for target in WINDOW_ALERT_TARGETS:
                try:
                    await self.hass.services.async_call(
                        "notify", target.removeprefix("notify."),
                        {"title": "HVAC Salotto", "message": (
                            "Regolazione silenziosa sospesa per oggi dopo due escalation; "
                            "ventole restituite ad AUTO."
                        )}, blocking=True,
                    )
                except Exception:  # best-effort alert; control must keep running
                    LOGGER.warning("Governor demotion notification failed for %s", target)

        self.entry.async_create_background_task(
            self.hass, send(), "villa_hvac_governor_demotion"
        )

    def __call__(self, state) -> dict:
        living = state.zones.get("living_room")
        kitchen = state.zones.get("kitchen")
        target = living.resolved_center if living is not None else None
        armed = state.steady_pacing_enabled
        actuate = armed and state.paced_living_room
        eligible = bool(
            armed and state.season == SEASON_SUMMER and state.house_mode == HOUSE_MODE_HOME
            and living is not None and living.enabled and not living.paused
            and living.temp is not None and living.demand is not None
            and kitchen is not None and kitchen.demand is not None
            and target is not None and not _is_free_cooling(state)
        )
        if self.demoted_day is not None and self.demoted_day != state.now.date():
            self.demoted_day = None
            self.escalations.clear()
        duty = strokes = slope = None
        context = "MARGINAL_CALL"
        if living is not None:
            duty, strokes, context, slope = self._history(state, living)
        error = living.temp - target if living is not None and living.temp is not None and target is not None else None
        if self.demoted_day == state.now.date():
            eligible = False
            reason = "sospeso fino a domani dopo escalation ripetute"
            status = "DEMOTED"
        elif self.escalated_until is not None and state.now < self.escalated_until:
            eligible = False
            reason = "AUTO temporaneo dopo escalation comfort"
            status = "ESCALATED"
        elif not eligible:
            missing_control_data = bool(
                armed and living is not None
                and (living.temp is None or living.demand is None
                     or kitchen is None or kitchen.demand is None)
            )
            reason = (
                "dati temperatura o valvole non disponibili: restituito ad AUTO"
                if missing_control_data else "non idoneo o switch disattivato"
            )
            status = "ESCALATED" if missing_control_data else "NATIVE"
        else:
            reason = None
            status = "PACED" if actuate else "SHADOW"

        out = {}
        if not eligible:
            out = self._release(living)
        else:
            assert error is not None
            self.error_cycles = self.error_cycles + 1 if error >= 0.6 else 0
            if self.fan >= FAN_CEILING and error >= 0.6:
                self.ceiling_since = self.ceiling_since or state.now
            else:
                self.ceiling_since = None
            escalate = error >= 1.0 or (
                self.ceiling_since is not None
                and state.now - self.ceiling_since >= timedelta(minutes=20)
            )
            if escalate:
                status = "ESCALATED"
                reason = "comfort o dati richiedono AUTO"
                self.escalations.append(state.now)
                self.escalated_until = state.now + EVALUATION
                while self.escalations and state.now - self.escalations[0] > timedelta(hours=3):
                    self.escalations.popleft()
                if len(self.escalations) >= 2:
                    self.demoted_day = state.now.date()
                    status = "DEMOTED"
                    self._notify_demotion(state.now)
                out = self._release(living)
            else:
                due = self.last_eval is None or state.now - self.last_eval >= EVALUATION
                kitchen_fast = slope is not None and slope >= 0.4
                if due or kitchen_fast or self.error_cycles >= 10:
                    new_fan, action, self.stable_evaluations = steady_governor_step(
                        fan=self.fan, floor=living.fan_min, context=context,
                        error=error, duty=duty, strokes_h=strokes,
                        kitchen_fast_rise=kitchen_fast,
                        stable_evaluations=self.stable_evaluations,
                    )
                    if self.down_block_until is not None and state.now < self.down_block_until:
                        new_fan = max(new_fan, self.fan)
                    if kitchen_fast:
                        self.down_block_until = state.now + DOWN_BLOCK
                    self.fan = new_fan
                    self.last_eval = state.now
                else:
                    action = "in attesa della prossima valutazione"
                if actuate:
                    self._managed = True
                    out[temperature_lever(living.climate)] = round(target, 1)
                    for fan, manuale in living.fancoil_units:
                        out[switch_lever(manuale)] = "on"
                        out[fan_lever(fan)] = self.fan
                else:
                    self._managed = False
                reason = action
        self.view = {
            "state": status, "target": target,
            "actual": living.temp if living is not None else None,
            "proposed_fan": self.fan if armed else None,
            "delivered_fan": living.fan_pct if living is not None else None,
            "valve_duty": duty, "strokes_per_hour": strokes,
            "house_context": context, "kitchen_delta_10m": slope,
            "reason": reason,
            "next_evaluation": (
                self.last_eval + EVALUATION if self.last_eval is not None else state.now
            ),
        }
        return out

    def failsafe(self) -> None:
        self._managed = False
