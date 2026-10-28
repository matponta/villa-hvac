"""VMC controller: night free-cooling boost (#5) + year-round speed (v0.86.0).

Two mechanical-ventilation machines (STORY_VMC.md):
- VMC 1 "Bagno Cantine" (ground): cantina vini, palestra, bagno ingresso, bagno
  palestra — rooms with 1-2 walls against soil, so the job is fighting damp.
- VMC 2 "Cucina e Casa" (living): kitchen, master suite + bath, office, studio V.

Levers, both EDGE-TRIGGERED (written only when our own decision flips, never
re-asserted), which is why this lives OUTSIDE the reconcile arbiter:
- BOOST switch (#5, summer): flush the rooms with cool outdoor air when it is
  meaningfully cooler than the warmest served room. Duration cap + min on-time
  (v0.70.0). v0.86.0: vetoed on the damp unit when the outside air is wetter in
  absolute terms (warm humid air onto cold earth walls condenses).
- SPEED (`fan.*` 33/66/100, v0.86.0): raised only for a reason — drying the
  ground rooms (any season, when the outside air is drier), or a quiet 66 %
  flush on the bedroom unit while the occupied-Notte veto blocks the boost.
  We snapshot the owner's speed before raising and restore it when the reason
  ends; a speed changed by hand while we hold it is conceded, never fought.
  Ownership is persisted so a restart cannot orphan a raised speed (the v0.68.0
  lesson).
- WINTER boost cap: a boost we did not command blows heated air outside — cut
  it after VMC_WINTER_BOOST_MAX unless the ground unit is drying.

Opt-in (`switch.vmc_auto`) on top of the master supervisor switch; deploy-dark
until both are on. Diagnostics (`view`) and alerts (`switch.vmc_alerts`, default
ON: filter, alarm, offline, unit off, damp room that can't be dried) run
regardless of the master.
"""
from __future__ import annotations

import asyncio
import logging
import math
from dataclasses import dataclass
from datetime import datetime

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    STATE_NOT_HOME,
    STATE_OFF,
    STATE_ON,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .away import aggregate_presence
from .const import (
    CELLAR_SAMPLE_MAX_AGE,
    CELLAR_ZONE,
    DEFAULT_SPLIT_RH_CEILING,
    DEFAULT_SPLIT_RH_FLOOR,
    HOUSE_MODE_NIGHT,
    OPT_SPLIT_RH_CEILING,
    OPT_SPLIT_RH_FLOOR,
    OPT_WINDOW_ALERT_TARGETS,
    OUTDOOR_RH,
    OUTDOOR_TEMP,
    OUTDOOR_TEMP_FALLBACK,
    PRESENCE_PERSONS,
    SEASON_SUMMER,
    SEASON_WINTER,
    VMC_ALERT_ALARM_FOR,
    VMC_ALERT_DAMP_FOR,
    VMC_ALERT_DAMP_RESET_RH,
    VMC_ALERT_DAMP_RH,
    VMC_ALERT_FAN_OFF_FOR,
    VMC_ALERT_OFFLINE_FOR,
    VMC_ALERT_TAG,
    VMC_BOOST_COOLDOWN,
    VMC_BOOST_HYSTERESIS,
    VMC_BOOST_MARGIN,
    VMC_BOOST_MAX_ON,
    VMC_BOOST_MIN_ON,
    VMC_BOOST_OUTDOOR_HYSTERESIS,
    VMC_BOOST_OUTDOOR_MAX,
    VMC_DEHUM_AH_HYSTERESIS,
    VMC_DEHUM_AH_MARGIN,
    VMC_DEHUM_RELEASE_DELTA,
    VMC_DEHUM_RH_VERY_HIGH,
    VMC_GROUPS,
    VMC_HOLD_MAX_AGE,
    VMC_MANUAL_CONFIRM_CYCLES,
    VMC_QUIET_FLUSH_PCT,
    VMC_RECOVERY_MIN_SPREAD,
    VMC_SPEED_MIN_HOLD,
    VMC_WINTER_BOOST_MAX,
    WINDOW_ALERT_TARGETS,
    ZONES,
)
from .controller import (
    current_house_mode,
    current_season,
    vmc_alerts_enabled,
    vmc_boost_enabled,
)
from .supervisor import VmcBoostState, vmc_boost_decision, vmc_boost_step
from .supervisor.cellar import sample_fresh
from .supervisor.ventilation import (
    REASON_BASE,
    REASON_QUIET_FLUSH,
    VMC_PCT_MIN,
    DehumidifyDecision,
    Held,
    RoomAir,
    bypass_state,
    hold_value,
    pct_matches,
    recovery_efficiency,
    vmc_dehumidify,
    vmc_speed_target,
)

_LOGGER = logging.getLogger(__name__)

STORE_KEY = "villa_hvac_vmc"
STORE_VERSION = 1
# After our own speed write the integration may still report the old value for
# a poll or two: don't count that as a hand change.
_WRITE_GRACE_S = 120.0


@dataclass
class SpeedOwnership:
    """A speed WE raised: what we set, what to give back, since when."""

    target: int
    snapshot: float | None
    reason: str
    since: datetime
    written_at: datetime
    mismatch: int = 0


@dataclass
class _Episode:
    since: datetime | None = None
    sent: bool = False


class VmcController:
    """Edge-triggered VMC boost + speed + alerts (opt-in, deploy-dark)."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self._unsub = None
        self._commanded: dict[str, bool] = {}  # group -> the last boost WE wrote
        self._state: dict[str, VmcBoostState] = {}  # group -> duration bookkeeping
        self._speed: dict[str, SpeedOwnership] = {}  # group -> a speed WE hold
        self._orphans: dict[str, dict] = {}  # persisted ownership awaiting the fan
        self._held: dict[str, Held] = {}  # entity -> last good reading
        self._boost_seen: dict[str, datetime] = {}  # group -> manual boost since
        self._episodes: dict[str, _Episode] = {}  # alert key -> episode
        self._view: dict[str, dict] = {}  # group -> diagnostics for the sensors
        self._store: Store = Store(hass, STORE_VERSION, STORE_KEY)
        # Smarty writes can hang for seconds: never let two ticks interleave
        self._lock = asyncio.Lock()

    @property
    def status(self) -> dict[str, VmcBoostState]:
        """Live duration bookkeeping per group (read-only, for diagnostics)."""
        return dict(self._state)

    @property
    def view(self) -> dict[str, dict]:
        """Per-unit diagnostics (status/efficiency sensors read this)."""
        return self._view

    async def async_start(self) -> None:
        try:
            data = await self._store.async_load() or {}
        except Exception:  # noqa: BLE001 - a corrupt store must not block setup
            _LOGGER.warning("VMC: could not load %s", STORE_KEY, exc_info=True)
            data = {}
        self._orphans = {
            g: v for g, v in (data.get("speed") or {}).items() if g in VMC_GROUPS
        }
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
        # A tick that finds the previous one still writing is skipped, not queued.
        if not self._lock.locked():
            self.hass.async_create_task(self._evaluate())

    # -- readings --------------------------------------------------------------
    def _num(self, entity_id: str) -> float | None:
        s = self.hass.states.get(entity_id)
        if s is None or s.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return None
        try:
            v = float(s.state)
        except (TypeError, ValueError):
            return None
        return v if math.isfinite(v) else None

    def _held_num(self, entity_id: str | None, now: datetime) -> float | None:
        """A numeric reading that survives the Smarty/EP `unavailable` blips."""
        if not entity_id:
            return None
        held = hold_value(
            self._held.get(entity_id), self._num(entity_id), now,
            max_age=VMC_HOLD_MAX_AGE,
        )
        if held is None:
            self._held.pop(entity_id, None)
            return None
        self._held[entity_id] = held
        return held.value

    def _fan_pct(self, fan: str, now: datetime) -> tuple[str | None, float | None]:
        """(raw fan state, held percentage). A fan reading OFF has no speed."""
        s = self.hass.states.get(fan)
        raw = s.state if s is not None else None
        key = f"{fan}#pct"
        live = None
        if raw == STATE_ON:
            pct = s.attributes.get("percentage")
            try:
                live = float(pct) if pct is not None else None
            except (TypeError, ValueError):
                live = None
        if raw == STATE_OFF:
            self._held.pop(key, None)
            return raw, None
        held = hold_value(self._held.get(key), live, now, max_age=VMC_HOLD_MAX_AGE)
        if held is None:
            self._held.pop(key, None)
            return raw, None
        self._held[key] = held
        return raw, held.value

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

    def _zone_temps(self) -> dict:
        return (self.entry.runtime_data.data or {}).get("zone_temps") or {}

    def _indoor(self, zones: tuple[str, ...]) -> float | None:
        """Warmest served-room fused temp this cycle (None if none available)."""
        temps = self._zone_temps()
        vals = [
            t["value"]
            for zid in zones
            if (t := temps.get(zid)) and t.get("value") is not None
        ]
        return max(vals) if vals else None

    def _opt_float(self, key: str, default: float) -> float:
        try:
            return float(self.entry.options.get(key, default))
        except (TypeError, ValueError):
            return default

    def _rooms(self, cfg: dict, now: datetime) -> list[RoomAir]:
        temps = self._zone_temps()
        rooms = []
        for zone, (sensor, ceiling, floor) in cfg.get("humidity_rooms", {}).items():
            if zone == "cantina_vini":
                # the wine band is owned by the split trio's options (#6)
                ceiling = self._opt_float(OPT_SPLIT_RH_CEILING, DEFAULT_SPLIT_RH_CEILING)
                floor = self._opt_float(OPT_SPLIT_RH_FLOOR, DEFAULT_SPLIT_RH_FLOOR)
            t = temps.get(zone) or {}
            temp, rh = t.get("value"), self._held_num(sensor, now)
            if zone == CELLAR_ZONE:
                # v0.87.0: only the fan-mixed reading is the cellar; the raw EP
                # value is a warm stagnant pocket whenever the fan is off.
                split = getattr(self.entry.runtime_data, "split", None)
                sample = sample_fresh(
                    getattr(split, "cellar_sample", None), now, CELLAR_SAMPLE_MAX_AGE,
                )
                temp, rh = (sample.temp, sample.rh) if sample else (None, None)
            rooms.append(RoomAir(
                zone=zone, temp=temp, rh=rh,
                rh_ceiling=ceiling, rh_floor=floor,
            ))
        return rooms

    # -- main loop -------------------------------------------------------------
    async def _evaluate(self) -> None:
        async with self._lock:
            await self._evaluate_locked()

    async def _evaluate_locked(self) -> None:
        now = dt_util.utcnow()
        season = current_season(self.hass, self.entry)
        is_summer = season == SEASON_SUMMER
        outdoor = self._outdoor()
        outdoor_rh = self._num(OUTDOOR_RH)
        dehum: dict[str, DehumidifyDecision | None] = {}
        for group, cfg in VMC_GROUPS.items():
            if cfg.get("dehumidify"):
                own = self._speed.get(group)
                dehum[group] = vmc_dehumidify(
                    self._rooms(cfg, now),
                    outdoor_temp=outdoor, outdoor_rh=outdoor_rh,
                    active=own is not None and own.reason != REASON_QUIET_FLUSH,
                    margin=VMC_DEHUM_AH_MARGIN,
                    hysteresis=VMC_DEHUM_AH_HYSTERESIS,
                    release_delta=VMC_DEHUM_RELEASE_DELTA,
                    rh_very_high=VMC_DEHUM_RH_VERY_HIGH,
                )
            else:
                dehum[group] = None
            self._update_view(group, cfg, now, dehum[group])
        await self._adopt_orphans(now)
        if vmc_alerts_enabled(self.hass, self.entry):
            await self._check_alerts(now, dehum)

        engine = getattr(self.entry.runtime_data, "engine", None)
        active = (
            engine is not None
            and engine.enabled
            and vmc_boost_enabled(self.hass, self.entry)
        )
        if not active:
            await self._release()  # disabled / deploy-dark -> hand back
            for group in VMC_GROUPS:
                self._view[group]["reason"] = "non_attivo"
            return
        # A bedroom-serving unit stays quiet during Notte WHILE the house is
        # occupied; an empty house lets it flush at night (owner rule 2026-07-10).
        night = current_house_mode(self.hass, self.entry) == HOUSE_MODE_NIGHT
        occupied = self._occupied()
        for group, cfg in VMC_GROUPS.items():
            d = dehum[group]
            quiet = bool(cfg.get("night_quiet")) and night and occupied
            # v0.86.0: never pull wetter air onto the earth-sheltered rooms
            damp = bool(is_summer and d is not None and d.damp_outside)
            on_now = self._commanded.get(group, False)
            indoor = self._indoor(cfg["zones"])
            want = vmc_boost_decision(
                is_summer=is_summer,
                outdoor=outdoor,
                indoor=indoor,
                on_now=on_now,
                outdoor_max=VMC_BOOST_OUTDOOR_MAX,
                margin=VMC_BOOST_MARGIN,
                hysteresis=VMC_BOOST_HYSTERESIS,
                quiet=quiet or damp,
                outdoor_hysteresis=VMC_BOOST_OUTDOOR_HYSTERESIS,
            )
            was = self._state.get(group, VmcBoostState())
            state, decision = vmc_boost_step(
                want=want,
                veto=quiet or damp,  # bypasses min-on: sleepers / wet air
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

            # The thermal want the quiet veto blocked -> a 66 % flush instead.
            own = self._speed.get(group)
            flushing = own is not None and own.reason == REASON_QUIET_FLUSH
            quiet_flush = quiet and not damp and vmc_boost_decision(
                is_summer=is_summer, outdoor=outdoor, indoor=indoor,
                on_now=flushing,
                outdoor_max=VMC_BOOST_OUTDOOR_MAX, margin=VMC_BOOST_MARGIN,
                hysteresis=VMC_BOOST_HYSTERESIS,
                outdoor_hysteresis=VMC_BOOST_OUTDOOR_HYSTERESIS,
            )
            target, reason = vmc_speed_target(
                dehumidify_pct=d.pct if d is not None else None,
                flush_wanted=quiet_flush, quiet=quiet,
                quiet_pct=VMC_QUIET_FLUSH_PCT,
            )
            await self._apply_speed(group, cfg, target, reason, now)
            await self._winter_boost_cap(
                group, cfg, now,
                winter=season == SEASON_WINTER,
                drying=d is not None and d.pct is not None,
            )
            v = self._view[group]
            own = self._speed.get(group)
            v["reason"] = (
                own.reason if own is not None
                else d.reason if d is not None and d.reason != REASON_BASE
                else REASON_BASE
            )
            v["owned_pct"] = own.target if own is not None else None
            v["snapshot_pct"] = own.snapshot if own is not None else None
            v["boost_commanded"] = self._commanded.get(group, False)
            v["boost_cooldown_until"] = (
                state.cooldown_until.isoformat() if state.cooldown_until else None
            )
            v["night_quiet"] = quiet

    # -- speed lever -----------------------------------------------------------
    async def _apply_speed(
        self, group: str, cfg: dict, target: int | None, reason: str,
        now: datetime,
    ) -> None:
        raw, live = self._fan_pct(cfg["fan"], now)
        own = self._speed.get(group)
        if own is not None:
            if raw == STATE_OFF:
                # switched off by hand (or a filter change): not ours to restart
                _LOGGER.info("VMC %s: fan switched off by hand, speed conceded", group)
                await self._drop(group)
                return
            in_grace = (now - own.written_at).total_seconds() < _WRITE_GRACE_S
            if live is not None and not in_grace and not pct_matches(live, own.target):
                own.mismatch += 1
                if own.mismatch >= VMC_MANUAL_CONFIRM_CYCLES:
                    _LOGGER.info(
                        "VMC %s: speed changed by hand (%s%% vs our %s%%), conceded",
                        group, live, own.target,
                    )
                    await self._drop(group)
                    return
            else:
                own.mismatch = 0
            if target is None:
                if now - own.since < VMC_SPEED_MIN_HOLD:
                    return  # anti-flap: hold a fresh raise
                back = round(own.snapshot) if own.snapshot is not None else VMC_PCT_MIN
                if await self._set_pct(cfg["fan"], back):
                    _LOGGER.info("VMC %s: %s ended, speed back to %s%%",
                                 group, own.reason, back)
                    await self._drop(group)
                return
            if target != own.target and await self._set_pct(cfg["fan"], target):
                own.target, own.reason = target, reason
                own.written_at, own.mismatch = now, 0
                await self._save()
            elif target == own.target:
                own.reason = reason
            return
        if target is None or live is None:
            return  # nothing wanted / unit off or offline: can't snapshot
        if live >= target - 5:
            return  # already at (or above) what we'd ask: nothing to own
        if await self._set_pct(cfg["fan"], target):
            _LOGGER.info("VMC %s: %s -> %s%% (was %s%%)", group, reason, target, live)
            self._speed[group] = SpeedOwnership(
                target=target, snapshot=live, reason=reason, since=now, written_at=now,
            )
            await self._save()

    async def _set_pct(self, fan: str, pct: int) -> bool:
        try:
            await self.hass.services.async_call(
                "fan", "set_percentage",
                {"entity_id": fan, "percentage": pct}, blocking=True,
            )
        except Exception:  # noqa: BLE001 - Smarty times out; retried next tick
            _LOGGER.warning("VMC: could not set %s to %s%%", fan, pct, exc_info=True)
            return False
        return True

    async def _drop(self, group: str) -> None:
        self._speed.pop(group, None)
        await self._save()

    async def _save(self) -> None:
        data = {
            "speed": {
                g: {"target": o.target, "snapshot": o.snapshot}
                for g, o in self._speed.items()
            }
        }
        try:
            await self._store.async_save(data)
        except Exception:  # noqa: BLE001 - persistence is best-effort
            _LOGGER.warning("VMC: could not save %s", STORE_KEY, exc_info=True)

    async def _adopt_orphans(self, now: datetime) -> None:
        """A speed raised before a restart: adopt it if the fan still runs at
        what we set (the normal loop then keeps or hands it back), forget it if
        someone has changed it since."""
        for group in list(self._orphans):
            cfg = VMC_GROUPS[group]
            raw, live = self._fan_pct(cfg["fan"], now)
            if raw in (None, STATE_UNAVAILABLE, STATE_UNKNOWN):
                continue  # integration not up yet: try next tick
            info = self._orphans.pop(group)
            target = info.get("target")
            if live is not None and target is not None and pct_matches(live, target):
                self._speed[group] = SpeedOwnership(
                    target=int(target), snapshot=info.get("snapshot"),
                    reason="ripristino", since=now - VMC_SPEED_MIN_HOLD,
                    written_at=now,
                )
                _LOGGER.info("VMC %s: adopted a pre-restart %s%% raise", group, target)
            else:
                await self._save()

    # -- winter boost cap ------------------------------------------------------
    async def _winter_boost_cap(
        self, group: str, cfg: dict, now: datetime, *, winter: bool, drying: bool,
    ) -> None:
        s = self.hass.states.get(cfg["boost_switch"])
        boosting = s is not None and s.state == STATE_ON
        if (
            not winter or not boosting or not VMC_WINTER_BOOST_MAX
            or self._commanded.get(group)
        ):
            self._boost_seen.pop(group, None)
            return
        since = self._boost_seen.setdefault(group, now)
        if now - since < VMC_WINTER_BOOST_MAX or drying:
            return
        _LOGGER.info(
            "VMC %s: winter boost on since %s, turned off (heat loss)",
            group, since.isoformat(),
        )
        try:
            await self.hass.services.async_call(
                "switch", "turn_off", {"entity_id": cfg["boost_switch"]},
                blocking=True,
            )
            self._boost_seen.pop(group, None)
        except Exception:  # noqa: BLE001 - retried next tick
            _LOGGER.warning("VMC: could not stop %s", cfg["boost_switch"], exc_info=True)

    # -- diagnostics -----------------------------------------------------------
    def _update_view(
        self, group: str, cfg: dict, now: datetime, d: DehumidifyDecision | None,
    ) -> None:
        outdoor_air = self._held_num(cfg.get("outdoor_air"), now)
        supply = self._held_num(cfg.get("supply_air"), now)
        extract = self._held_num(cfg.get("extract_air"), now)
        eff = recovery_efficiency(
            outdoor_air, supply, extract, min_spread=VMC_RECOVERY_MIN_SPREAD,
        )
        raw, live = self._fan_pct(cfg["fan"], now)
        boost = self.hass.states.get(cfg["boost_switch"])
        view = self._view.setdefault(group, {"reason": REASON_BASE})
        view.update({
            "name": cfg["name"],
            "fan_state": raw,
            "fan_pct": live,
            "boost_on": boost.state == STATE_ON if boost is not None else None,
            "outdoor_air": outdoor_air,
            "supply_air": supply,
            "extract_air": extract,
            "outdoor_probe_trusted": cfg.get("outdoor_trusted", True),
            "recovery_efficiency": eff,
            "bypass": bypass_state(eff),
            "filter_due": self._filter_due(cfg, now),
            "alarm": self._is_on(cfg.get("alarm")),
            "warning": self._is_on(cfg.get("warning")),
        })
        if d is not None:
            view.update({
                "humidity_zone": d.zone,
                "humidity_reason": d.reason,
                "room_ah": round(d.room_ah, 2) if d.room_ah is not None else None,
                "outdoor_ah": round(d.outdoor_ah, 2) if d.outdoor_ah is not None else None,
                "damp_outside": d.damp_outside,
                "rooms_rh": {
                    r.zone: r.rh for r in self._rooms(cfg, now)
                },
            })

    def _is_on(self, entity_id: str | None) -> bool | None:
        s = self.hass.states.get(entity_id) if entity_id else None
        if s is None or s.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return None
        return s.state == STATE_ON

    def _filter_due(self, cfg: dict, now: datetime) -> bool | None:
        """`filter_days_left` is a TIMESTAMP (when the filter is due)."""
        s = self.hass.states.get(cfg.get("filter") or "")
        if s is None or s.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return None
        due = dt_util.parse_datetime(s.state)
        if due is None:
            return None
        return due <= now

    # -- alerts ----------------------------------------------------------------
    async def _check_alerts(
        self, now: datetime, dehum: dict[str, DehumidifyDecision | None],
    ) -> None:
        for group, cfg in VMC_GROUPS.items():
            name = cfg["name"]
            fan = self.hass.states.get(cfg["fan"])
            fan_raw = fan.state if fan is not None else None
            view = self._view.get(group, {})
            await self._episode(
                f"{group}_filter", bool(view.get("filter_due")), now, None,
                f"{name}: filtro da cambiare",
                "Il timer filtri è scaduto. Sostituisci il filtro e premi "
                "'Reset filters timer'.",
            )
            await self._episode(
                f"{group}_alarm", bool(view.get("alarm")), now, VMC_ALERT_ALARM_FOR,
                f"{name}: allarme",
                "La VMC segnala un allarme da oltre 10 minuti.",
            )
            await self._episode(
                f"{group}_offline", fan_raw == STATE_UNAVAILABLE, now,
                VMC_ALERT_OFFLINE_FOR,
                f"{name}: non raggiungibile",
                "L'integrazione non riceve dati dalla VMC da 30 minuti.",
            )
            await self._episode(
                f"{group}_fan_off", fan_raw == STATE_OFF, now, VMC_ALERT_FAN_OFF_FOR,
                f"{name}: spenta",
                "La VMC è spenta da oltre un'ora"
                + (" — le stanze contro terra non vengono ventilate."
                   if cfg.get("dehumidify") else "."),
            )
            d = dehum.get(group)
            if d is None or d.zone is None:
                continue
            rh = self._view[group].get("rooms_rh", {}).get(d.zone)
            key = f"{group}_damp"
            ep = self._episodes.get(key)
            engaged = ep is not None and ep.since is not None
            damp = rh is not None and (
                rh >= VMC_ALERT_DAMP_RESET_RH if engaged
                else rh >= VMC_ALERT_DAMP_RH and d.damp_outside
            )
            await self._episode(
                key, damp, now, VMC_ALERT_DAMP_FOR,
                f"Umidità alta: {ZONES.get(d.zone, {}).get('name', d.zone)}",
                f"Umidità {rh:.0f}% da oltre 2 ore e l'aria esterna è più umida: "
                "la VMC non può asciugare. Valuta un deumidificatore."
                if rh is not None else "",
            )

    async def _episode(
        self, key: str, condition: bool, now: datetime, after, title: str,
        message: str,
    ) -> None:
        """One notification per episode: condition true for `after`, reset when
        it clears."""
        ep = self._episodes.setdefault(key, _Episode())
        if not condition:
            ep.since, ep.sent = None, False
            return
        if ep.since is None:
            ep.since = now
        if ep.sent or (after is not None and now - ep.since < after):
            return
        ep.sent = True
        _LOGGER.info("VMC alert %s: %s", key, title)
        raw = self.entry.options.get(OPT_WINDOW_ALERT_TARGETS)
        targets = (
            [t.strip() for t in str(raw).split(",") if t.strip()] if raw
            else list(WINDOW_ALERT_TARGETS)
        )
        for target in targets:
            try:
                await self.hass.services.async_call(
                    "notify", target.removeprefix("notify."),
                    {
                        "title": title,
                        "message": message,
                        "data": {"tag": f"{VMC_ALERT_TAG}_{key}"},
                    },
                    blocking=True,
                )
            except Exception:  # noqa: BLE001 - paging is best-effort, never fatal
                _LOGGER.warning("VMC alert: could not notify %s", target, exc_info=True)

    # -- boost write / release -------------------------------------------------
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
        """Hand back every boost and speed WE set (disable / unload / HA stop).

        Waits for an in-flight tick so it can't hand back under a write."""
        async with self._lock:
            await self._release()

    async def _release(self) -> None:
        for group, cfg in VMC_GROUPS.items():
            if self._commanded.get(group):
                await self._write(group, cfg["boost_switch"], False)
        self._commanded.clear()
        self._state.clear()  # not managing -> no cap/cooldown to remember
        self._boost_seen.clear()
        if not self._speed:
            return
        for group, own in list(self._speed.items()):
            back = round(own.snapshot) if own.snapshot is not None else VMC_PCT_MIN
            if await self._set_pct(VMC_GROUPS[group]["fan"], back):
                self._speed.pop(group, None)
        await self._save()
