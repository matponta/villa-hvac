"""Safety controller for the rack's shared P1 fancoil."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import logging

from .const import (
    HOUSE_MODE_VACATION,
    P1_GUARD_FAN_PCT,
    P1_GUARD_SETPOINT_DROP,
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
        was_active = self.state.active
        self.state, _ = rack_guard_step(
            self.state, rack.temp, state.rack_temp_threshold, state.now
        )
        self._maybe_alert(state, rack, eligible=True)
        if not self.state.active:
            # Natural cool-down while still eligible: hand back like the
            # ineligible path (manuale OFF, fan left alive, setpoint restored)
            # instead of silently dropping the levers and stranding the shared
            # rack/P1 fan pinned in manual.
            if was_active or self._snapshot is not None:
                return self._release(state, p1, force=True)
            return {}
        base = self._base(state, p1)
        if self._snapshot is None and base is not None:
            self._snapshot = round(base, 1)
        target = None
        if base is not None and p1.temp is not None:
            # Floor the target term at 20 FIRST, then cap at base — so the result
            # is always ≤ base (never warmer than the zone's own target) even when
            # base itself is < 20 (cold house setpoint / negative offset).
            target = round(min(base, max(20.0, p1.temp - 1.0)), 1)
        fan = (
            RACK_GUARD_EMERGENCY_FAN_PCT
            if self.state.escalated else RACK_GUARD_INITIAL_FAN_PCT
        )
        # The rack fan belongs to the rack zone; its chilled-water valve is
        # controlled by the P1 thermostat (nudge that to open it). P1 no longer
        # "owns" the rack fan (stairs_p1.fancoils is empty), so read both explicitly.
        rack_fan = ZONES["rack"]["fancoils"][0]
        p1_climate = ZONES["stairs_p1"]["climate"]
        out = {
            switch_lever("switch.fancoil_locale_rack_manuale"): "on",
            fan_lever(rack_fan): fan,
            BLOCCO_LEVER: BLOCCO_RELEASE,
        }
        if target is not None:
            out[temperature_lever(p1_climate)] = target
        return out

    def _release(self, state, p1, force: bool = False) -> dict:
        if not force and not self.state.active and self._snapshot is None:
            self.state = RackGuardState()
            return {}
        rack_fan = ZONES["rack"]["fancoils"][0]
        p1_climate = ZONES["stairs_p1"]["climate"]
        out = {
            switch_lever("switch.fancoil_locale_rack_manuale"): "off",
            fan_lever(rack_fan): RACK_GUARD_INITIAL_FAN_PCT,
        }
        live = self._base(state, p1) if p1 is not None else None
        restore = live if live is not None else self._snapshot
        if restore is not None:
            out[temperature_lever(p1_climate)] = round(restore, 1)
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


# The office fan's manuale switch (engine naming: fan.fancoil_X -> switch.fancoil_X_manuale).
_OFFICE_MANUALE = "switch.fancoil_studio_pianerottolo_p1_manuale"


class P1GuardController:
    """P1 'both fans' secondary trigger — P1 has no fan of its own.

    When the P1 landing runs hot, force BOTH fancoils that vent into it cooler:
    the rack fan (via a P1-thermostat nudge, same valve the rack guard uses) AND
    the office fan (via an office-thermostat nudge, bounded ≤ the office's own
    base so it is never driven WARMER). Reuses the rack-guard hysteresis. On
    release/ineligibility both are handed back (manuale OFF, fan left alive,
    both setpoints restored). Merged AFTER the rack guard (which wins the shared
    rack levers when both fire) and BEFORE the cooling controller/policies (so
    the office nudge outranks house_mode while active). Opt-in switch.p1_guard.
    """

    def __init__(self, hass=None, entry=None) -> None:
        self.hass = hass
        self.entry = entry
        self.state = RackGuardState()
        self._snap_p1: float | None = None
        self._snap_office: float | None = None

    @staticmethod
    def _base(state, zone) -> float | None:
        if state.house_setpoint is None or state.mode_offset is None or zone is None:
            return None
        return state.house_setpoint + state.mode_offset + zone.setpoint_offset

    def __call__(self, state) -> dict:
        p1 = state.zones.get("stairs_p1")
        office = state.zones.get("office")
        eligible = bool(
            state.p1_guard_enabled
            and state.season == SEASON_SUMMER
            and state.house_mode != HOUSE_MODE_VACATION
            and p1 is not None and p1.enabled and not p1.paused
            and office is not None and office.enabled and not office.paused
            and not _is_free_cooling(state)
            and p1.temp is not None
        )
        if not eligible:
            return self._release(state, p1, office)
        was_active = self.state.active
        self.state, _ = rack_guard_step(
            self.state, p1.temp, state.p1_guard_threshold, state.now
        )
        if not self.state.active:
            if was_active or self._snap_p1 is not None or self._snap_office is not None:
                return self._release(state, p1, office, force=True)
            return {}
        rack_fan = ZONES["rack"]["fancoils"][0]
        office_fan = ZONES["office"]["fancoils"][0]
        out = {
            switch_lever("switch.fancoil_locale_rack_manuale"): "on",
            fan_lever(rack_fan): P1_GUARD_FAN_PCT,
            switch_lever(_OFFICE_MANUALE): "on",
            fan_lever(office_fan): P1_GUARD_FAN_PCT,
            BLOCCO_LEVER: BLOCCO_RELEASE,
        }
        p1_base = self._base(state, p1)
        if self._snap_p1 is None and p1_base is not None:
            self._snap_p1 = round(p1_base, 1)
        if p1_base is not None:
            out[temperature_lever(ZONES["stairs_p1"]["climate"])] = round(
                min(p1_base, max(20.0, p1.temp - P1_GUARD_SETPOINT_DROP)), 1
            )
        office_base = self._base(state, office)
        if self._snap_office is None and office_base is not None:
            self._snap_office = round(office_base, 1)
        if office_base is not None and office.temp is not None:
            # Never warmer than the office's own base; only nudge it DOWN to open
            # the office fan's valve (floor the temp term first, then cap at base).
            out[temperature_lever(ZONES["office"]["climate"])] = round(
                min(office_base, max(20.0, office.temp - P1_GUARD_SETPOINT_DROP)), 1
            )
        return out

    def _release(self, state, p1, office, force: bool = False) -> dict:
        if (
            not force and not self.state.active
            and self._snap_p1 is None and self._snap_office is None
        ):
            self.state = RackGuardState()
            return {}
        rack_fan = ZONES["rack"]["fancoils"][0]
        office_fan = ZONES["office"]["fancoils"][0]
        out = {
            switch_lever("switch.fancoil_locale_rack_manuale"): "off",
            fan_lever(rack_fan): P1_GUARD_FAN_PCT,
            switch_lever(_OFFICE_MANUALE): "off",
            fan_lever(office_fan): P1_GUARD_FAN_PCT,
        }
        p1_live = self._base(state, p1)
        p1_restore = p1_live if p1_live is not None else self._snap_p1
        if p1_restore is not None:
            out[temperature_lever(ZONES["stairs_p1"]["climate"])] = round(p1_restore, 1)
        office_live = self._base(state, office)
        office_restore = office_live if office_live is not None else self._snap_office
        if office_restore is not None:
            out[temperature_lever(ZONES["office"]["climate"])] = round(office_restore, 1)
        self.state = RackGuardState()
        self._snap_p1 = None
        self._snap_office = None
        return out

    def failsafe_setpoints(self) -> dict[str, float]:
        out: dict[str, float] = {}
        if self._snap_p1 is not None:
            out[ZONES["stairs_p1"]["climate"]] = self._snap_p1
        if self._snap_office is not None:
            out[ZONES["office"]["climate"]] = self._snap_office
        self._snap_p1 = None
        self._snap_office = None
        self.state = RackGuardState()
        return out
