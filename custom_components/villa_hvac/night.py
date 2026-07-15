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

CHILLED WATER (v0.54.0): while guard-active (summer) the controller ALSO emits a
`temperature:` opinion at threshold−NIGHT_GUARD_SETPOINT_DROP (bounded: never
above the #2a mode target, which must be computable) — the manuale switch only
holds the FAN %, the EV FAN valve follows the thermostat setpoint, so without
this the guard circulated warm air with the valve CLOSED whenever the room sat
between the threshold (26) and the Notte setpoint (27; the whole first live
night for padronale). Controllers merge before the pure policies, so the opinion
outranks house_mode #2a on that lever; it yields on disabled/paused zones and
while free-cooling coasts (never a setpoint under building_protection). Released
by the guard's below-hysteresis / auto-wake / Notte exit (#2a re-asserts its
target in the same merge) AND by the engine's `async_fail_safe` via
`failsafe_setpoints()`, which restores the base RECORDED AT NUDGE TIME (live
entity reads are gone by the time an unload-path fail-safe runs).

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
    NIGHT_GUARD_SETPOINT_DROP,
    NIGHT_SILENCE_SWITCHES,
    NIGHT_WAKE_DAY_MINUTES,
    OPT_AUTO_WAKE_TIME,
    OPT_NIGHT_THRESHOLD,
    SEASON_SUMMER,
    ZONES,
)
from .controller import current_house_mode
from .supervisor import (
    _is_free_cooling,
    fan_lever,
    in_window,
    switch_lever,
    temperature_lever,
)

_LOGGER = logging.getLogger(__name__)


def bedrooms() -> list[tuple[str, dict]]:
    """(zone_id, zone) for zones flagged as bedrooms."""
    return [(zid, z) for zid, z in ZONES.items() if z.get("bedroom")]


def night_silence_selected(hass: HomeAssistant | None, zone_id: str) -> bool:
    """Return the persistent #2b selection for a zone (default ON).

    The mapping is explicit so control behavior never depends on HA's generated
    entity naming rules.  Missing state during startup preserves the historical
    both-bedrooms-selected behavior.
    """
    entity_id = NIGHT_SILENCE_SWITCHES.get(zone_id)
    if entity_id is None or hass is None:
        return True
    selected = hass.states.get(entity_id)
    return selected is None or selected.state == "on"


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
    the 2 bedrooms — silenced (fan 0), or the heat-guard low stage (plus the
    v0.54.0 chilled-water `temperature:` nudge) when a room overheats. The
    engine's reconcile arbiter does the writing, so #2b is no
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
        # v0.54.0 chilled water: zone -> the #2a mode base RECORDED AT NUDGE TIME
        # for every bedroom whose setpoint the guard has written this Notte
        # episode (the lever may be displaced below that base). The restore
        # target is snapshotted here — NOT recomputed from live entities at
        # restore time — because on the unload path the integration's own
        # select/number entities are already gone when `async_fail_safe` runs
        # (adversarial review, 2026-07-11). NOT dropped on a mid-night guard
        # silence (house_mode restores that same cycle, but a fail-safe later
        # that night must still be able to re-write the base); dropped on the
        # Notte-exit restore only when #2a is actually re-asserting.
        self._nudged: dict[str, float] = {}
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
            return self._release(state)
        threshold = self._threshold()
        free_cooling = _is_free_cooling(state)
        out: dict = {}
        for zid, zone in bedrooms():
            if not night_silence_selected(self.hass, zid):
                out.update(self._release_zone(state, zid, zone, free_cooling))
                continue
            z = state.zones.get(zid)
            temp = z.temp if z is not None else None
            new_state, _action = evaluate_guard(
                self._guards[zid], temp, threshold, state.now
            )
            self._guards[zid] = new_state
            out[switch_lever(zone["manuale_switch"])] = "on"
            # v0.55.0: a #4-paused bedroom (window OPEN, now on a real contact)
            # stays fully silent — "stop the AC in that room" includes the guard
            # fan, which used to circulate warm air into the open window (the
            # known #4 edge, closed now that the bedrooms have contacts). Same
            # while FREE-COOLING coasts (outdoor or windows-airing): the zone is
            # BP with the valve shut, so the stage could only stir warm room air
            # at night (the v0.54.0 nudge already yields there — the fan now
            # matches). The hysteresis still advances, so the stage resumes on
            # the next cycle once the pause/coast ends.
            paused = z is not None and z.paused
            out[fan_lever(zone["fancoils"][0])] = (
                NIGHT_GUARD_FAN_PCT
                if (new_state.cooling and not paused and not free_cooling) else 0
            )
            # v0.54.0 chilled water: guard-active ALSO owns the room setpoint
            # (controllers merge before the pure policies, so this outranks
            # house_mode #2a) — drives it below the room temp so the KNX
            # thermostat opens the EV FAN valve and the held 33% fan moves
            # chilled air, not the 26–27 dead-band warm-air loop of the first
            # live night. On guard silence the key is simply not emitted and
            # #2a re-asserts the mode setpoint in the same merge.
            if new_state.cooling:
                base = self._mode_base(state, z) if z is not None else None
                nudge = self._nudge_target(state, z, threshold, base)
                if nudge is not None:
                    out[temperature_lever(z.climate)] = nudge
                    self._nudged[zid] = round(base, 1)  # restore snapshot
            self._managing.add(zid)
        return out

    @staticmethod
    def _mode_base(state, z) -> float | None:
        """What #2a would set this zone to right now (house base + mode offset +
        per-room trim); None when not computable (e.g. Vacanza has no offset)."""
        if state.house_setpoint is None or state.mode_offset is None:
            return None
        return state.house_setpoint + state.mode_offset + z.setpoint_offset

    def _nudge_target(self, state, z, threshold: float, base: float | None) -> float | None:
        """The chilled-water setpoint for a guard-active bedroom, or None to
        leave the lever alone (the guard stays fan-only).

        Summer only: in winter the guard stays fan-only — threshold−drop sits
        ABOVE the winter setback target, and a raised setpoint on a heat-mode
        thermostat could heat the very room the guard is trying to cool. Skips
        disabled/#4-paused zones (their building_protection is owned by the
        higher preset policies; never push a setpoint under a pause) and yields
        while FREE-COOLING holds the fancoils in building_protection (a setpoint
        under that BP is inert but displaced; the coast owns cooling — the
        free-cool × guard escalation question belongs to the outside-air merge
        design). REQUIRES a computable #2a base and is bounded by it, so the
        nudge can only ever DEEPEN, never raise — with the base unknown, a raw
        threshold−drop could RAISE a trimmed room's setpoint (e.g. a −3 °C
        room trim puts the mode target below 25.5).
        """
        if state.season != SEASON_SUMMER:
            return None
        if z is None or not z.enabled or z.paused or not z.climate:
            return None
        if _is_free_cooling(state):
            return None
        if base is None:
            return None
        return round(min(threshold - NIGHT_GUARD_SETPOINT_DROP, base), 1)

    def _release(self, state) -> dict:
        """One-shot hand-back: manuale off — plus any guard-nudged setpoint back
        to the house-mode base — for the bedrooms we were silencing. (If the #3
        band re-takes a bedroom this same cycle, its opinion merges first and
        wins; if #2a is active it keeps re-asserting the same base every cycle.)

        The restore prefers the LIVE #2a base (the mode we are releasing into),
        falling back to the nudge-time snapshot (e.g. Vacanza: no live offset).
        The tracking is dropped only when #2a is actually re-asserting the lever
        (auto_setback on + base computable); otherwise the one-shot write is a
        single unprotected telegram, so the snapshot is KEPT for the fail-safe.

        DEAD-FAN-AT-WAKE (v0.56.0): the silence wrote each bedroom fan's KNX
        ON/OFF object OFF, and a KNX fancoil in AUTO does NOT restart a fan whose
        switch object was left off — only an explicit ON revives it, while the
        interlock holds the EV valve shut. So releasing manuale alone hands KNX a
        DEAD fan (the room floats warm, valve closed, invisibly — padronale
        2026-07-12). The hand-back therefore ALSO re-arms the fan with a one-shot
        turn-on (NIGHT_GUARD_FAN_PCT; AUTO re-drives the % once it is alive) for
        every bedroom the silence had OFF. It SKIPS a bedroom that is still
        #4-paused or free-cooling — building_protection holds those and a fan
        would only stir warm air into an open window; the engine self-heal
        watchdog re-arms them once the pause/coast ends. A guard that was
        actively cooling at hand-back already has a live fan (33%), so no turn-on
        is emitted — keeping the guard-fired release byte-identical.
        """
        if not self._managing:
            return {}
        free_cooling = _is_free_cooling(state)
        out: dict = {}
        for zid, zone in bedrooms():
            out.update(self._release_zone(state, zid, zone, free_cooling))
        return out

    def _release_zone(self, state, zid: str, zone: dict, free_cooling: bool) -> dict:
        """One-shot safe hand-back for one participating bedroom."""
        if zid not in self._managing:
            return {}
        out: dict = {switch_lever(zone["manuale_switch"]): "off"}
        z = state.zones.get(zid)
        paused = z is not None and z.paused
        if not paused and not free_cooling and not self._guards[zid].cooling:
            out[fan_lever(zone["fancoils"][0])] = NIGHT_GUARD_FAN_PCT
        if zid in self._nudged:
            live = self._mode_base(state, z) if z is not None else None
            base = live if live is not None else self._nudged[zid]
            out[temperature_lever(zone["climate"])] = round(base, 1)
            if state.auto_setback and live is not None:
                del self._nudged[zid]
        self._guards[zid] = GuardState()
        self._managing.discard(zid)
        return out

    def failsafe_setpoints(self) -> dict[str, float]:
        """#2b fail-safe: climate entity → the #2a base setpoint RECORDED AT
        NUDGE TIME for every bedroom the guard nudged this Notte episode.
        `async_fail_safe` writes these so a guard-displaced setpoint
        (threshold−drop, colder than asked) never outlives the supervisor.

        Deliberately NO live entity reads: on the unload path the platforms
        (and with them `number.house_setpoint`/`select.house_mode`) are torn
        down BEFORE `async_fail_safe` runs, so a restore computed live would
        silently no-op exactly where it matters most. Clears the tracking for
        the zones it returns.
        """
        if not self._nudged:
            return {}
        zones = dict(bedrooms())
        out: dict[str, float] = {}
        for zid, base in self._nudged.items():
            zone = zones.get(zid)
            if zone and zone.get("climate"):
                out[zone["climate"]] = base
        self._nudged.clear()
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
