"""v0.96.0 night band — HA wiring (STORY_NIGHT_SCHEDULE.md).

`NightBand.step` runs inside `build_house_state` every cycle (deploy-dark
included, like the rest of the read-only plan): it reads the switch, the
band times, the HA alarm and the winter model, asks the pure
`night_band_plan` which rooms are in the band, and returns their trims. It
writes nothing itself — the trims ride on `ZoneSnapshot.night_trim` into the
same #2a / band / precool targets the house mode feeds, through the arbiter.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.util import dt as dt_util

from .const import (
    DOMAIN,
    HOUSE_MODE_HOME,
    NIGHT_BAND_SUMMER_LEAD,
    PRESET_CONTROLLABLE_EMITTERS,
    SEASON_WINTER,
    ZONE_TWINS,
)
from .controller import (
    night_band_enabled,
    night_band_offset,
    night_band_times,
    preheat_max_lead,
    wake_alarm,
)
from .supervisor.night_band import (
    NightBandLatch,
    NightBandPlan,
    band_window,
    night_band_plan,
    room_lead,
    wake_after,
)
from .supervisor.winter_model import WinterParams, recovery_minutes
from .supervisor.winter_plan import outdoor_estimate


def night_band_signal(entry: ConfigEntry) -> str:
    """Dispatcher signal: the night-band view changed (the sensor refreshes)."""
    return f"{DOMAIN}_{entry.entry_id}_night_band"


def _settings_zone(zone_id: str) -> str:
    return ZONE_TWINS.get(zone_id, zone_id)


def _local(when: datetime | None) -> str | None:
    return dt_util.as_local(when).isoformat() if when is not None else None


class NightBand:
    """Holds the pre-heat latch + the last plan for `sensor.fascia_notturna`."""

    def __init__(self) -> None:
        self.latch = NightBandLatch()
        self.plan: NightBandPlan | None = None
        self.view: dict = {"status": "spenta"}

    def step(
        self, hass: HomeAssistant, entry: ConfigEntry, *, now: datetime,
        season: str, mode: str, auto_setback: bool,
        house_setpoint: float | None, zones: dict, outdoor: float | None,
        forecast=(), winter=None,
    ) -> dict[str, float]:
        """zone_id -> night trim (°C) for the rooms in the band right now."""
        tz = dt_util.get_default_time_zone()
        enabled = night_band_enabled(hass, entry)
        start, end = night_band_times(hass, entry, season)
        wake = wake_alarm(hass, entry)
        offset = night_band_offset(entry, season)
        max_lead = preheat_max_lead(entry)
        rooms = [
            z for z in zones.values()
            if z.climate and z.emitter in PRESET_CONTROLLABLE_EMITTERS
        ]
        # The alarm this night (for the outdoor estimate up to the wake).
        window = band_window(now, start, end, tz)
        wake_at = wake_after(window[0], wake, tz) if window is not None else None
        out_est = (
            outdoor_estimate(forecast, now, wake_at, outdoor)
            if wake_at is not None else outdoor
        )
        leads: dict[str, timedelta] = {}
        details: dict[str, dict] = {}
        for z in rooms:
            if season == SEASON_WINTER:
                params = (winter.get(_settings_zone(z.zone_id))
                          if winter is not None else WinterParams())
                target = (house_setpoint + z.setpoint_offset
                          if house_setpoint is not None else None)
                rec = recovery_minutes(
                    target - abs(offset) if target is not None else None,
                    target, out_est, params,
                )
                lead = room_lead(rec, params.confidence(), max_lead)
                details[z.zone_id] = {
                    "recovery_min": round(rec) if rec is not None else None,
                    "confidence": round(params.confidence(), 2),
                }
            else:
                lead = min(NIGHT_BAND_SUMMER_LEAD, max_lead)
                details[z.zone_id] = {}
            leads[z.zone_id] = lead
        # Twins are one room: the follower leaves the band with its leader.
        for follower, leader in ZONE_TWINS.items():
            if follower in leads and leader in leads:
                leads[follower] = leads[leader]
        plan, self.latch = night_band_plan(
            now, enabled=enabled,
            active_mode=(mode == HOUSE_MODE_HOME and auto_setback),
            start=start, end=end, tz=tz, wake=wake, leads=leads, latch=self.latch,
        )
        self.plan = plan
        trims = {zid: offset for zid in plan.trimmed}
        previous = self.view
        self.view = {
            "status": plan.status,
            "reason": plan.reason,
            "season": season,
            "offset": offset,
            "start": start.strftime("%H:%M"),
            "end": end.strftime("%H:%M"),
            "window_start": _local(plan.window_start),
            "window_end": _local(plan.window_end),
            "alarm": wake is not None,
            "alarm_time": wake.strftime("%H:%M") if wake is not None else None,
            "wake_used": _local(plan.wake),
            "max_lead_min": round(max_lead.total_seconds() / 60),
            "trimmed": sorted(plan.trimmed),
            "rooms": {
                z.zone_id: {
                    "name": z.name,
                    "lead_min": round(leads[z.zone_id].total_seconds() / 60),
                    "end": _local(plan.room_end.get(z.zone_id)),
                    "in_band": z.zone_id in plan.trimmed,
                    **details.get(z.zone_id, {}),
                }
                for z in rooms
            },
        }
        if self.view != previous:
            async_dispatcher_send(hass, night_band_signal(entry))
        return trims
