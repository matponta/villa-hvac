"""VMC boost (#5): night free-cooling ventilation.

Two mechanical-ventilation machines. When summer + the outside air is meaningfully
cooler than the warmest room a unit serves, boost it to flush the rooms with cool
air (banking coolth for the next day). Self-contained + EDGE-TRIGGERED: it writes
the boost switch ONLY when its own decision flips, and never re-asserts — so a
manual boost (the owner's kitchen switch) is respected, not fought. This is why it
lives OUTSIDE the reconcile arbiter (which is idempotent-reassert by design).

Two guards on top of the thermal decision (v0.70.0, owner rule 2026-09-10):
a DURATION CAP (max continuous run, then an owed cooldown) and a MIN ON-TIME
(anti-flap). Both live in the pure `vmc_boost_step`; the night-quiet veto and
disable/unload always win over the min on-time.

Opt-in (`switch.vmc_auto`) on top of the master supervisor switch; fully
deploy-dark until both are on. Releases (hands the boost back) when disabled or on
unload.
"""
from __future__ import annotations

import logging
import math

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_NOT_HOME, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant, callback
from homeassistant.util import dt as dt_util

from .away import aggregate_presence
from .const import (
    HOUSE_MODE_NIGHT,
    OUTDOOR_TEMP,
    OUTDOOR_TEMP_FALLBACK,
    PRESENCE_PERSONS,
    SEASON_SUMMER,
    VMC_BOOST_COOLDOWN,
    VMC_BOOST_HYSTERESIS,
    VMC_BOOST_MARGIN,
    VMC_BOOST_MAX_ON,
    VMC_BOOST_MIN_ON,
    VMC_BOOST_OUTDOOR_HYSTERESIS,
    VMC_BOOST_OUTDOOR_MAX,
    VMC_GROUPS,
)
from .controller import current_house_mode, current_season, vmc_boost_enabled
from .supervisor import VmcBoostState, vmc_boost_decision, vmc_boost_step

_LOGGER = logging.getLogger(__name__)


class VmcController:
    """Edge-triggered VMC free-cooling boost (opt-in, deploy-dark)."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self._unsub = None
        self._commanded: dict[str, bool] = {}  # group -> the last state WE wrote
        self._state: dict[str, VmcBoostState] = {}  # group -> duration bookkeeping

    @property
    def status(self) -> dict[str, VmcBoostState]:
        """Live duration bookkeeping per group (read-only, for diagnostics)."""
        return dict(self._state)

    def start(self) -> None:
        coordinator = self.entry.runtime_data
        self._unsub = coordinator.async_add_listener(self._on_update)

    async def async_stop(self) -> None:
        if self._unsub is not None:
            self._unsub()
            self._unsub = None
        await self.async_release()

    @callback
    def _on_update(self) -> None:
        # Coordinator listeners are sync; do the (async) evaluation off-thread.
        self.hass.async_create_task(self._evaluate())

    def _num(self, entity_id: str) -> float | None:
        s = self.hass.states.get(entity_id)
        if s is None or s.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return None
        try:
            v = float(s.state)
        except (TypeError, ValueError):
            return None
        return v if math.isfinite(v) else None

    def _outdoor(self) -> float | None:
        v = self._num(OUTDOOR_TEMP)
        return v if v is not None else self._num(OUTDOOR_TEMP_FALLBACK)

    def _occupied(self) -> bool:
        """Is anyone home? Reuses the #7 durable presence fuse. Unknown -> treat as
        occupied (the safe/quiet default: don't risk a loud night boost blindly)."""
        presence = aggregate_presence(
            self.hass.states.get(p).state if self.hass.states.get(p) else None
            for p in PRESENCE_PERSONS
        )
        return presence != STATE_NOT_HOME

    def _indoor(self, zones: tuple[str, ...]) -> float | None:
        """Warmest served-room fused temp this cycle (None if none available)."""
        temps = (self.entry.runtime_data.data or {}).get("zone_temps") or {}
        vals = [
            t["value"]
            for zid in zones
            if (t := temps.get(zid)) and t.get("value") is not None
        ]
        return max(vals) if vals else None

    async def _evaluate(self) -> None:
        engine = getattr(self.entry.runtime_data, "engine", None)
        active = (
            engine is not None
            and engine.enabled
            and vmc_boost_enabled(self.hass, self.entry)
        )
        if not active:
            await self.async_release()  # disabled / deploy-dark -> hand back
            return
        is_summer = current_season(self.hass, self.entry) == SEASON_SUMMER
        outdoor = self._outdoor()
        # A bedroom-serving unit stays quiet during Notte WHILE the house is
        # occupied; an empty house lets it flush at night (owner rule 2026-07-10).
        night = current_house_mode(self.hass, self.entry) == HOUSE_MODE_NIGHT
        occupied = self._occupied()
        now = dt_util.utcnow()
        for group, cfg in VMC_GROUPS.items():
            on_now = self._commanded.get(group, False)
            quiet = bool(cfg.get("night_quiet")) and night and occupied
            want = vmc_boost_decision(
                is_summer=is_summer,
                outdoor=outdoor,
                indoor=self._indoor(cfg["zones"]),
                on_now=on_now,
                outdoor_max=VMC_BOOST_OUTDOOR_MAX,
                margin=VMC_BOOST_MARGIN,
                hysteresis=VMC_BOOST_HYSTERESIS,
                quiet=quiet,
                outdoor_hysteresis=VMC_BOOST_OUTDOOR_HYSTERESIS,
            )
            was = self._state.get(group, VmcBoostState())
            state, decision = vmc_boost_step(
                want=want,
                veto=quiet,  # bypasses min-on: never run a loud fan over sleepers
                on_now=on_now,
                now=now,
                state=was,
                max_on=VMC_BOOST_MAX_ON,
                cooldown=VMC_BOOST_COOLDOWN,
                min_on=VMC_BOOST_MIN_ON,
            )
            self._state[group] = state
            if state.cooldown_until != was.cooldown_until:
                if state.cooldown_until is not None:
                    _LOGGER.info(
                        "VMC %s: %s cap reached, resting until %s",
                        group, VMC_BOOST_MAX_ON, state.cooldown_until.isoformat(),
                    )
                else:
                    _LOGGER.info("VMC %s: cooldown served, boost re-armed", group)
            if decision != on_now:  # edge only — never re-assert
                await self._write(group, cfg["boost_switch"], decision)

    async def _write(self, group: str, switch_entity: str, on: bool) -> None:
        try:
            await self.hass.services.async_call(
                "switch", "turn_on" if on else "turn_off",
                {"entity_id": switch_entity}, blocking=True,
            )
            self._commanded[group] = on
        except Exception:  # noqa: BLE001 - a wedged VMC write must not break the tick
            _LOGGER.exception("VMC: could not set %s", switch_entity)

    async def async_release(self) -> None:
        """Hand every boost WE turned on back off (disable / unload)."""
        for group, cfg in VMC_GROUPS.items():
            if self._commanded.get(group):
                await self._write(group, cfg["boost_switch"], False)
        self._commanded.clear()
        self._state.clear()  # not managing -> no cap/cooldown to remember
