"""Diagnostic sensors for Villa HVAC (Phase 0, read-only)."""
from __future__ import annotations

from datetime import datetime, timedelta

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from . import VillaHvacConfigEntry
from .const import (
    CONDOMINIO_BATTERY_POWER,
    CONDOMINIO_BATTERY_SOC,
    CONDOMINIO_GRID_POWER,
    CONDOMINIO_PV_REMAINING,
    OUTDOOR_TEMP,
    OUTDOOR_TEMP_FALLBACK,
    PDC_LOAD_POWER,
    SOLAR_RADIATION,
    ZONES,
)
from .controller import (
    return_armed,
    return_date,
    return_daypart,
    return_precond_enabled,
)
from .coordinator import VillaHvacCoordinator
from .supervisor import PlanView, cooling_load


async def async_setup_entry(
    hass: HomeAssistant,
    entry: VillaHvacConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the diagnostic sensor and the per-zone fused temperature sensors."""
    coordinator = entry.runtime_data
    entities: list[SensorEntity] = [
        CoolingDemandZonesSensor(coordinator, entry),
        HvacPlanSensor(coordinator, entry),
        ReturnPlanSensor(coordinator, entry),
        EnergyBiasSensor(coordinator, entry),
    ]
    entities += [
        ZoneTemperatureSensor(coordinator, entry, zone_id, zone)
        for zone_id, zone in ZONES.items()
    ]
    # F2: one learned-thermal-model diagnostic per cooling fancoil leader zone.
    entities += [
        HvacModelSensor(coordinator, entry, zone_id, zone)
        for zone_id, zone in ZONES.items()
        if zone.get("climate") and zone.get("emitter") == "fancoil"
    ]
    async_add_entities(entities)


def _num_state(hass: HomeAssistant, entity_id: str) -> float | None:
    s = hass.states.get(entity_id)
    if s is None or s.state in ("unavailable", "unknown"):
        return None
    try:
        return float(s.state)
    except (TypeError, ValueError):
        return None


class CoolingDemandZonesSensor(
    CoordinatorEntity[VillaHvacCoordinator], SensorEntity
):
    """Number of zones currently calling for cooling (fancoil fan > 0)."""

    _attr_has_entity_name = True
    _attr_name = "Cooling demand zones"
    _attr_icon = "mdi:snowflake-thermometer"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "zones"

    def __init__(
        self, coordinator: VillaHvacCoordinator, entry: VillaHvacConfigEntry
    ) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_cooling_demand_zones"

    @property
    def native_value(self) -> int | None:
        return self.coordinator.data.get("cooling_zone_count")

    @property
    def extra_state_attributes(self) -> dict:
        data = self.coordinator.data
        return {
            "zones": data.get("cooling_zones"),
            "consenso_freddo": data.get("consenso_freddo"),
            "consenso_caldo": data.get("consenso_caldo"),
        }


class ZoneTemperatureSensor(
    CoordinatorEntity[VillaHvacCoordinator], SensorEntity
):
    """Fused current temperature for a zone (#1).

    Thermostat-primary: reads the zone's `sensor.clima_*` twin, falling back to
    the climate's `current_temperature` when the primary is missing or stale.
    """

    _attr_has_entity_name = True
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_suggested_display_precision = 1

    def __init__(
        self,
        coordinator: VillaHvacCoordinator,
        entry: VillaHvacConfigEntry,
        zone_id: str,
        zone: dict,
    ) -> None:
        super().__init__(coordinator)
        self._zone_id = zone_id
        self._attr_name = f"{zone['name']} temperature"
        self._attr_unique_id = f"{entry.entry_id}_{zone_id}_temperature"

    @property
    def _zone_data(self) -> dict:
        return (self.coordinator.data.get("zone_temps") or {}).get(self._zone_id) or {}

    @property
    def native_value(self) -> float | None:
        return self._zone_data.get("value")

    @property
    def available(self) -> bool:
        return super().available and self._zone_data.get("value") is not None

    @property
    def extra_state_attributes(self) -> dict:
        data = self._zone_data
        return {
            "source": data.get("source"),
            "sensor_raw": data.get("sensor_raw"),
            "climate_raw": data.get("climate_raw"),
        }


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _minutes(value: timedelta | None) -> float | None:
    return round(value.total_seconds() / 60, 1) if value is not None else None


# Icon per plan regime (the sensor state) — keeps the dashboard chip legible.
_PLAN_ICONS = {
    "pre_cool": "mdi:snowflake-alert",
    "peak_run": "mdi:weather-sunny-alert",
    "duty_rest": "mdi:pause-circle-outline",
    "cooling": "mdi:snowflake",
    "free_cool": "mdi:weather-windy",
    "heating": "mdi:fire",
    "idle": "mdi:hvac",
}


class HvacPlanSensor(CoordinatorEntity[VillaHvacCoordinator], SensorEntity):
    """The organism's next-12h PLAN (#11), projected from the engine each cycle.

    State = the current regime (pre_cool / peak_run / duty_rest / cooling /
    free_cool / heating / idle). Attributes carry the forecast curve + peak, the
    duty run/rest windows, per-zone planned setpoints, and the shading covers, so
    a dashboard timeline card can render the 12h intent. Computed even while the
    supervisor is deploy-dark, so the plan is visible before actuation lights up.
    """

    _attr_has_entity_name = True
    _attr_name = "HVAC plan"
    # Recorder-exclude the heavy, every-30s attributes so they don't bloat the DB
    # (the 12h per-room trajectories + the forecast curve + per-zone lists).
    _unrecorded_attributes = frozenset(
        {"room_plans", "forecast", "zones", "covers_closing", "per_zone"}
    )

    def __init__(
        self, coordinator: VillaHvacCoordinator, entry: VillaHvacConfigEntry
    ) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_hvac_plan"

    @property
    def _plan(self) -> PlanView | None:
        engine = getattr(self.coordinator, "engine", None)
        return engine.plan_view if engine is not None else None

    @property
    def native_value(self) -> str | None:
        plan = self._plan
        return plan.summary if plan is not None else None

    @property
    def icon(self) -> str:
        plan = self._plan
        return _PLAN_ICONS.get(plan.summary if plan else "", "mdi:calendar-clock")

    @property
    def extra_state_attributes(self) -> dict:
        plan = self._plan
        if plan is None:
            return {}
        engine = getattr(self.coordinator, "engine", None)
        peak_at = None
        if plan.peak_eta is not None:
            peak_at = _iso(dt_util.utcnow() + plan.peak_eta)
        rest_starts = None
        if plan.stint_start is not None and plan.stint_cap is not None:
            rest_starts = _iso(plan.stint_start + plan.stint_cap)
        return {
            "supervisor_on": bool(getattr(engine, "enabled", False)),
            "regime": plan.regime,
            "g_house": plan.g_house,
            "k_house": plan.k_house,
            "load_ratio": plan.load_ratio,
            "solar_model": plan.solar_model,
            "season": plan.season,
            "house_mode": plan.house_mode,
            "cooling": plan.cooling,
            "free_cool": plan.free_cool,
            "precool": plan.precool,
            "at_peak": plan.at_peak,
            "forecast_peak": plan.forecast_peak,
            "peak_eta_minutes": _minutes(plan.peak_eta),
            "peak_at": peak_at,
            "house_setpoint": plan.house_setpoint,
            "effective_setpoint": plan.effective_setpoint,
            "precool_setpoint": plan.precool_setpoint,
            "duty_enabled": plan.duty_enabled,
            "in_cooloff": plan.in_cooloff,
            "cooloff_until": _iso(plan.cooloff_until),
            "stint_start": _iso(plan.stint_start),
            "stint_elapsed_minutes": _minutes(plan.stint_elapsed),
            "stint_cap_minutes": _minutes(plan.stint_cap),
            "rest_starts": rest_starts,
            "blocco": plan.blocco,
            "blocco_desired": plan.blocco_desired,
            "covers_closing": list(plan.covers_closing),
            "forecast": [
                {"datetime": _iso(when), "temperature": temp}
                for when, temp in plan.forecast
            ],
            "room_plans": [
                {
                    "zone": tr.zone_id,
                    "precool_depth": tr.precool_depth,
                    "precool_start_min": tr.precool_start_min,
                    "peak_breach": tr.peak_breach,
                    "max_temp": tr.max_temp,
                    "points": [
                        {"min": p.minute, "temp": p.temp, "setpoint": p.setpoint,
                         "fan": p.fan, "phase": p.phase, "saturated": p.saturated}
                        for p in tr.points
                    ],
                }
                for tr in plan.room_trajectories
            ],
            "zones": [
                {
                    "zone": z.zone_id,
                    "name": z.name,
                    "temperature": z.temp,
                    "target": z.target,
                    "demand": z.demand,
                    "enabled": z.enabled,
                    "paused": z.paused,
                }
                for z in plan.zones
            ],
        }


class HvacModelSensor(CoordinatorEntity[VillaHvacCoordinator], SensorEntity):
    """F2 diagnostic: the learned per-room thermal model.

    State = the current heat-gain rate G (°C/h) from the blended model; attributes
    expose the learned {a,b,c,k} and the confidences so you can watch each room's
    model converge before it ever feeds control.
    """

    _attr_has_entity_name = True
    _attr_icon = "mdi:chart-bell-curve-cumulative"
    _attr_native_unit_of_measurement = "°C/h"
    _attr_suggested_display_precision = 2

    def __init__(
        self,
        coordinator: VillaHvacCoordinator,
        entry: VillaHvacConfigEntry,
        zone_id: str,
        zone: dict,
    ) -> None:
        super().__init__(coordinator)
        self._zone_id = zone_id
        self._attr_name = f"{zone['name']} model"
        self._attr_unique_id = f"{entry.entry_id}_hvac_model_{zone_id}"

    @property
    def _model(self):
        engine = getattr(self.coordinator, "engine", None)
        thermal = getattr(engine, "thermal", None)
        return thermal.model_for(self._zone_id) if thermal is not None else None

    @property
    def _temp(self) -> float | None:
        return (self.coordinator.data.get("zone_temps") or {}).get(self._zone_id, {}).get("value")

    @property
    def native_value(self) -> float | None:
        m = self._model
        if m is None:
            return None
        outdoor = _num_state(self.hass, OUTDOOR_TEMP)
        if outdoor is None:
            outdoor = _num_state(self.hass, OUTDOOR_TEMP_FALLBACK)
        return round(
            cooling_load(self._temp, outdoor, _num_state(self.hass, SOLAR_RADIATION),
                         a=m.a, b=m.b, c=m.c),
            3,
        )

    @property
    def extra_state_attributes(self) -> dict:
        m = self._model
        if m is None:
            return {}
        engine = getattr(self.coordinator, "engine", None)
        thermal = getattr(engine, "thermal", None)
        abc_conf, k_conf = thermal.confidence(self._zone_id) if thermal else (0.0, 0.0)
        return {
            "a_outdoor": round(m.a, 5),
            "b_solar": round(m.b, 6),
            "c_base": round(m.c, 4),
            "k_capacity": round(m.k, 4),
            "abc_confidence": round(abc_conf, 3),
            "k_confidence": round(k_conf, 3),
            "passive_updates": m.n,
            "capacity_updates": m.n_k,
        }


class ReturnPlanSensor(CoordinatorEntity[VillaHvacCoordinator], SensorEntity):
    """#8 diagnostic: the return-home state (off / waiting / precond) + the plan.

    State = the AwayReturnController's decision (or `off`). Attributes expose the
    armed ETA, the computed lead time, and the pre-cond window start, so a
    dashboard module can show "Via — in attesa, pre-cond alle HH:MM".
    """

    _attr_has_entity_name = True
    _attr_name = "Return plan"
    _attr_icon = "mdi:home-clock-outline"

    def __init__(
        self, coordinator: VillaHvacCoordinator, entry: VillaHvacConfigEntry
    ) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_return_plan"

    @property
    def _ctrl(self):
        engine = getattr(self.coordinator, "engine", None)
        return getattr(engine, "away_return", None)

    @property
    def native_value(self) -> str:
        ctrl = self._ctrl
        return (ctrl.decision if ctrl and ctrl.decision else "off")

    @property
    def extra_state_attributes(self) -> dict:
        ctrl = self._ctrl
        rdate = return_date(self.hass, self._entry)
        lead_min = (
            round(ctrl.lead.total_seconds() / 60) if ctrl and ctrl.lead else None
        )
        eta = ctrl.eta if ctrl else None
        window_start = (
            (eta - ctrl.lead) if (eta is not None and ctrl and ctrl.lead) else None
        )
        return {
            "opt_in": return_precond_enabled(self.hass, self._entry),
            "armed": return_armed(self.hass, self._entry),
            "return_date": rdate.isoformat() if rdate else None,
            "daypart": return_daypart(self.hass, self._entry),
            "eta": eta.isoformat() if eta else None,
            "lead_minutes": lead_min,
            "precond_starts": window_start.isoformat() if window_start else None,
        }


class EnergyBiasSensor(CoordinatorEntity[VillaHvacCoordinator], SensorEntity):
    """PV/energy-aware pre-cool diagnostic (F4c-lite).

    State = the current decision (bank / coast / hold / off). Attributes expose the
    effectiveness ranking, the daily solar-vs-consumption balance, the chosen floor,
    and the live Condominio energy signals (PdC load, battery SoC/power, grid) so a
    dashboard can show why the organism is banking or deferring right now.
    """

    _attr_has_entity_name = True
    _attr_name = "Energy bias"
    _attr_icon = "mdi:solar-power-variant"

    def __init__(
        self, coordinator: VillaHvacCoordinator, entry: VillaHvacConfigEntry
    ) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_energy_bias"

    @property
    def _decision(self):
        engine = getattr(self.coordinator, "engine", None)
        return getattr(engine, "_pv_decision", None)

    @property
    def native_value(self) -> str:
        d = self._decision
        return d.mode if d is not None else "off"

    @property
    def extra_state_attributes(self) -> dict:
        d = self._decision
        return {
            "solar_rich": d.solar_rich if d else None,
            "eff_now": round(d.eff_now, 3) if d else None,
            "eff_peak": round(d.eff_peak, 3) if d else None,
            "floor": d.floor if d else None,
            "reason": d.reason if d else None,
            "pv_kwh_remaining": _num_state(self.hass, CONDOMINIO_PV_REMAINING),
            "pdc_load_w": _num_state(self.hass, PDC_LOAD_POWER),
            "battery_soc": _num_state(self.hass, CONDOMINIO_BATTERY_SOC),
            "battery_power_w": _num_state(self.hass, CONDOMINIO_BATTERY_POWER),
            "grid_power_w": _num_state(self.hass, CONDOMINIO_GRID_POWER),
        }
