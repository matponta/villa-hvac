"""Camere silenziose: night-time bedroom silence + heat-guard (#2b).

Embeds the legacy HA subsystem (buonanotte / sveglia / notte_guardia_caldo) for
the 2 bedrooms. When the house enters Notte the bedrooms go silent (fancoil to
`manuale` + fan off); a hysteresis heat-guard nudges the fan to a low stage if
the room overheats and silences it again once it cools. Leaving Notte or the
daily auto-wake restores AUTO so KNX resumes control.

C1 (F4c Phase 1): #2b is now an ARBITER CONTROLLER — `NightSilenceController`
emits `{switch:manuale, fan:pct}` lever opinions merged like #9/#3, so every
bedroom write flows through the engine's single reconcile writer (with its
manual-override tracking) instead of direct service calls. `active` is DERIVED
from the house mode (== Notte) + Auto-setback + a wake latch, so a reboot-in-Notte
re-enters silence via the controller (no startup-resync special case).

`evaluate_guard` is pure (no HA) so the hysteresis is unit-testable.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_track_time_change
from homeassistant.util import dt as dt_util

from .const import (
    DEFAULT_AUTO_WAKE_TIME,
    DEFAULT_NIGHT_THRESHOLD,
    HOUSE_MODE_NIGHT,
    NIGHT_GUARD_FAN_PCT,
    NIGHT_GUARD_HIGH,
    NIGHT_GUARD_LOW,
    NIGHT_WAKE_DAY_MINUTES,
    OPT_AUTO_WAKE_TIME,
    OPT_NIGHT_THRESHOLD,
    ZONES,
)
from .controller import current_house_mode
from .supervisor import fan_lever, in_window, switch_lever

_LOGGER = logging.getLogger(__name__)


def bedrooms() -> list[tuple[str, dict]]:
    """(zone_id, zone) for zones flagged as bedrooms."""
    return [(zid, z) for zid, z in ZONES.items() if z.get("bedroom")]


@dataclass(frozen=True)
class GuardState:
    """Per-bedroom heat-guard state."""

    cooling: bool = False
    above_since: datetime | None = None
    below_since: datetime | None = None


def evaluate_guard(
    state: GuardState,
    temp: float | None,
    threshold: float,
    now: datetime,
    high: timedelta = NIGHT_GUARD_HIGH,
    low: timedelta = NIGHT_GUARD_LOW,
) -> tuple[GuardState, str | None]:
    """Advance the hysteresis. Returns (new_state, action).

    action is "cool" (run the fan at the low stage), "silence" (fan off), or
    None (no change). Mirrors the legacy notte_guardia_caldo: above threshold
    for `high` -> cool; below threshold for `low` -> silence.
    """
    if temp is None:
        return state, None
    if not state.cooling:
        if temp > threshold:
            since = state.above_since or now
            if now - since >= high:
                return GuardState(cooling=True), "cool"
            return replace(state, above_since=since, below_since=None), None
        return replace(state, above_since=None), None
    # currently cooling
    if temp < threshold:
        since = state.below_since or now
        if now - since >= low:
            return GuardState(cooling=False), "silence"
        return replace(state, below_since=since, above_since=None), None
    return replace(state, below_since=None), None


class NightSilenceController:
    """#2b camere silenziose as an engine merge controller (C1).

    Each cycle the engine calls `__call__(state)`; while the house is in Notte
    (`state.night_active`) it returns `{switch:manuale on, fan:pct}` opinions for
    the 2 bedrooms — silenced (fan 0), or the heat-guard low stage when a room
    overheats. The engine's reconcile arbiter does the writing, so #2b is no
    longer a second direct writer. On the Notte-exit cycle it emits a one-shot
    manuale release; placed AFTER FanBandController in the merge, so FanBand
    re-taking a bedroom for pacing wins over that release.

    `active` is DERIVED (mode == Notte, Auto-setback on, not woken) — computed in
    `build_house_state` into `state.night_active` — so a reboot-in-Notte silences
    via the next cycle with no startup-resync branch. The auto-wake timer sets a
    latch that releases the silence until the mode leaves Notte.
    """

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, coordinator) -> None:
        self.hass = hass
        self.entry = entry
        self.coordinator = coordinator
        self._guards: dict[str, GuardState] = {zid: GuardState() for zid, _ in bedrooms()}
        self._managing: set[str] = set()
        self._woken = False
        self._unsub_wake = None

    def _wake_hms(self) -> tuple[int, int, int]:
        """Parse the configured auto-wake time ('HH:MM[:SS]'); 08:00 on garbage."""
        raw = str(self.entry.options.get(OPT_AUTO_WAKE_TIME, DEFAULT_AUTO_WAKE_TIME))
        parts = raw.split(":")
        try:
            hour = int(parts[0])
            minute = int(parts[1]) if len(parts) > 1 else 0
            second = int(parts[2]) if len(parts) > 2 else 0
        except (ValueError, IndexError):
            hour, minute, second = 8, 0, 0
        return hour, minute, second

    @property
    def woken(self) -> bool:
        """Is the silence lifted? (build_house_state reads this into night_active.)

        The event latch alone is NOT enough: it lives in memory, so a reboot or
        reload while the house is still in Notte AFTER the wake time would lose
        it — night_active would re-derive True and re-silence the bedrooms until
        the mode finally leaves Notte (a fan-dead morning; the C1 reboot
        re-silence feature working against us). So the wake is ALSO derived from
        the clock: within [wake_time, wake_time + 12 h) the silence is lifted
        regardless of the latch. Outside that day window (an early-evening
        Notte) the latch alone decides, so going to bed re-silences normally.
        """
        if self._woken:
            return True
        hour, minute, _ = self._wake_hms()
        wake_min = (hour % 24) * 60 + minute % 60
        local = dt_util.now()
        return in_window(
            local.hour * 60 + local.minute,
            wake_min,
            (wake_min + NIGHT_WAKE_DAY_MINUTES) % 1440,
        )

    def start(self) -> None:
        self._schedule_wake()

    def stop(self) -> None:
        if self._unsub_wake:
            self._unsub_wake()
            self._unsub_wake = None

    def _threshold(self) -> float:
        try:
            return float(self.entry.options.get(OPT_NIGHT_THRESHOLD, DEFAULT_NIGHT_THRESHOLD))
        except (TypeError, ValueError):
            return DEFAULT_NIGHT_THRESHOLD

    def __call__(self, state) -> dict:
        # Reset the wake latch once we've left Notte, so a fresh Notte re-silences.
        if state.house_mode != HOUSE_MODE_NIGHT:
            self._woken = False
        if not state.night_active:
            return self._release()
        threshold = self._threshold()
        out: dict = {}
        for zid, zone in bedrooms():
            z = state.zones.get(zid)
            temp = z.temp if z is not None else None
            new_state, _action = evaluate_guard(
                self._guards[zid], temp, threshold, state.now
            )
            self._guards[zid] = new_state
            out[switch_lever(zone["manuale_switch"])] = "on"
            out[fan_lever(zone["fancoils"][0])] = (
                NIGHT_GUARD_FAN_PCT if new_state.cooling else 0
            )
            self._managing.add(zid)
        return out

    def _release(self) -> dict:
        """One-shot hand-back: manuale off for the bedrooms we were silencing."""
        if not self._managing:
            return {}
        out: dict = {}
        for zid, zone in bedrooms():
            if zid in self._managing:
                out[switch_lever(zone["manuale_switch"])] = "off"
                self._guards[zid] = GuardState()
        self._managing.clear()
        return out

    def _schedule_wake(self) -> None:
        hour, minute, second = self._wake_hms()
        self._unsub_wake = async_track_time_change(
            self.hass, self._on_wake, hour=hour, minute=minute, second=second
        )

    @callback
    def _on_wake(self, now: datetime) -> None:
        # Auto-wake: latch the silence off (until the mode leaves Notte) + nudge the
        # engine to hand the bedrooms back this cycle rather than the next tick.
        if current_house_mode(self.hass, self.entry) != HOUSE_MODE_NIGHT or self._woken:
            return
        self._woken = True
        engine = getattr(self.coordinator, "engine", None)
        if engine is not None:
            self.entry.async_create_background_task(
                self.hass, engine.request_run(), "villa_hvac_night_wake"
            )
