"""Safety controller for the rack's shared P1 fancoil."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import logging

from .const import (
    HOUSE_MODE_VACATION,
    RACK_GUARD_EMERGENCY_FAN_PCT,
    RACK_GUARD_EMERGENCY_RISE,
    RACK_GUARD_ENGAGE,
    RACK_GUARD_INITIAL_FAN_PCT,
    RACK_GUARD_MIN_DROP,
    RACK_GUARD_NO_RESPONSE,
    RACK_GUARD_RELEASE,
    RACK_GUARD_RELEASE_DROP,
    SEASON_SUMMER,
    WINDOW_ALERT_TARGETS,
    ZONES,
)
from .supervisor import (
    BLOCCO_LEVER,
    BLOCCO_RELEASE,
    _is_free_cooling,
    fan_lever,
    switch_lever,
    temperature_lever,
)

_LOGGER = logging.getLogger(__name__)
_ALERT_AFTER = timedelta(minutes=30)


@dataclass(frozen=True)
class RackGuardState:
    active: bool = False
    escalated: bool = False
    above_since: datetime | None = None
    below_since: datetime | None = None
    emergency_since: datetime | None = None
    activated_at: datetime | None = None
    activation_temp: float | None = None


def rack_guard_step(
    current: RackGuardState, temp: float | None, threshold: float, now: datetime
) -> tuple[RackGuardState, str | None]:
    """Pure engage/release/escalation hysteresis for the rack guard."""
    if temp is None:
        return RackGuardState(), "release" if current.active else None
    if not current.active:
        if temp > threshold:
            since = current.above_since or now
            if now - since >= RACK_GUARD_ENGAGE:
                return RackGuardState(
                    active=True, activated_at=now, activation_temp=temp
                ), "engage"
            return RackGuardState(above_since=since), None
        return RackGuardState(), None

    if temp < threshold - RACK_GUARD_RELEASE_DROP:
        below = current.below_since or now
        if now - below >= RACK_GUARD_RELEASE:
            return RackGuardState(), "release"
    else:
        below = None

    emergency_since = current.emergency_since
    if temp >= threshold + RACK_GUARD_EMERGENCY_RISE:
        emergency_since = emergency_since or now
    else:
        emergency_since = None
    no_response = (
        current.activated_at is not None
        and current.activation_temp is not None
        and now - current.activated_at >= RACK_GUARD_NO_RESPONSE
        and temp > current.activation_temp - RACK_GUARD_MIN_DROP
    )
    emergency = (
        emergency_since is not None and now - emergency_since >= RACK_GUARD_ENGAGE
    ) or no_response
    escalated = current.escalated or emergency
    return RackGuardState(
        active=True,
        escalated=escalated,
        below_since=below,
        emergency_since=emergency_since,
        activated_at=current.activated_at,
        activation_temp=current.activation_temp,
    ), "escalate" if escalated and not current.escalated else None


class RackGuardController:
    """Highest-priority merge controller for rack hardware protection."""

    def __init__(self, hass=None, entry=None) -> None:
        self.hass = hass
        self.entry = entry
        self.state = RackGuardState()
        self._snapshot: float | None = None
        self._yield_hot_since: datetime | None = None
        self._alert_sent = False
        self.alert_reason: str | None = None

    async def _notify(self, reason: str, temp: float) -> None:
        message = (
            f"Allarme HVAC: rack a {temp:.1f} °C. {reason}. "
            "Controllare valvola, ventola e circolazione dell'acqua refrigerata."
        )
        for target in WINDOW_ALERT_TARGETS:
            try:
                await self.hass.services.async_call(
                    "notify", target.removeprefix("notify."),
                    {"title": "Protezione rack Villa", "message": message},
                    blocking=True,
                )
            except Exception:  # noqa: BLE001 - alerting must not break safety control
                _LOGGER.exception("Rack guard: could not notify %s", target)

    def _maybe_alert(self, state, rack, *, eligible: bool) -> None:
        temp = rack.temp if rack is not None else None
        if temp is None:
            return
        threshold = state.rack_temp_threshold
        recovered = temp < threshold - RACK_GUARD_RELEASE_DROP and not self.state.active
        if recovered:
            self._yield_hot_since = None
            self._alert_sent = False
            self.alert_reason = None
            return
        reason = None
        if not eligible and temp >= threshold + RACK_GUARD_EMERGENCY_RISE:
            self._yield_hot_since = self._yield_hot_since or state.now
            if state.now - self._yield_hot_since >= _ALERT_AFTER:
                reason = "La protezione è sospesa mentre la temperatura resta critica"
        else:
            self._yield_hot_since = None
        if self.state.active and self.state.activated_at is not None:
            ineffective = (
                state.now - self.state.activated_at >= _ALERT_AFTER
                and (
                    rack.demand is not True
                    or self.state.activation_temp is None
                    or temp > self.state.activation_temp - RACK_GUARD_MIN_DROP
                )
            )
            if ineffective:
                reason = "La protezione è attiva ma il raffrescamento non risponde"
        if reason is None or self._alert_sent:
            return
        self._alert_sent = True
        self.alert_reason = reason
        if self.hass is not None and self.entry is not None:
            self.entry.async_create_background_task(
                self.hass, self._notify(reason, temp), "villa_hvac_rack_alert"
            )

    @staticmethod
    def _base(state, p1) -> float | None:
        if state.house_setpoint is None or state.mode_offset is None:
            return None
        return state.house_setpoint + state.mode_offset + p1.setpoint_offset

    def __call__(self, state) -> dict:
        rack = state.zones.get("rack")
        p1 = state.zones.get("stairs_p1")
        eligible = bool(
            state.rack_guard_enabled
            and state.season == SEASON_SUMMER
            and state.house_mode != HOUSE_MODE_VACATION
            and p1 is not None
            and p1.enabled
            and not p1.paused
            and not _is_free_cooling(state)
            and rack is not None
            and rack.temp is not None
        )
        if not eligible:
            out = self._release(state, p1)
            self._maybe_alert(state, rack, eligible=False)
            return out
        self.state, _ = rack_guard_step(
            self.state, rack.temp, state.rack_temp_threshold, state.now
        )
        self._maybe_alert(state, rack, eligible=True)
        if not self.state.active:
            return {}
        base = self._base(state, p1)
        if self._snapshot is None and base is not None:
            self._snapshot = round(base, 1)
        target = None
        if base is not None and p1.temp is not None:
            target = round(max(20.0, min(base, p1.temp - 1.0)), 1)
        fan = (
            RACK_GUARD_EMERGENCY_FAN_PCT
            if self.state.escalated else RACK_GUARD_INITIAL_FAN_PCT
        )
        zone = ZONES["stairs_p1"]
        out = {
            switch_lever("switch.fancoil_locale_rack_manuale"): "on",
            fan_lever(zone["fancoils"][0]): fan,
            BLOCCO_LEVER: BLOCCO_RELEASE,
        }
        if target is not None:
            out[temperature_lever(zone["climate"])] = target
        return out

    def _release(self, state, p1) -> dict:
        if not self.state.active and self._snapshot is None:
            self.state = RackGuardState()
            return {}
        zone = ZONES["stairs_p1"]
        out = {
            switch_lever("switch.fancoil_locale_rack_manuale"): "off",
            fan_lever(zone["fancoils"][0]): RACK_GUARD_INITIAL_FAN_PCT,
        }
        live = self._base(state, p1) if p1 is not None else None
        restore = live if live is not None else self._snapshot
        if restore is not None:
            out[temperature_lever(zone["climate"])] = round(restore, 1)
        self.state = RackGuardState()
        self._snapshot = None
        return out

    def failsafe_setpoints(self) -> dict[str, float]:
        if self._snapshot is None:
            return {}
        target = self._snapshot
        self._snapshot = None
        self.state = RackGuardState()
        return {ZONES["stairs_p1"]["climate"]: target}
