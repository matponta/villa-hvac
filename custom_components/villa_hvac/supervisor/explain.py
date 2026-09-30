"""Per-room "why" surface (v0.75.0, system review 2026-09-30 §5). Pure.

One question, answered for every cooled room: WHO is driving this room right
now, toward WHAT, and what is the hardware actually doing? The inputs already
exist — lever provenance (v0.72.0), unit telemetry (v0.71.0), model validity
(v0.73.0), the shed controller's reason (v0.74.0) — this module only folds them
into a stable state code + an Italian sentence, the pattern the living-room
governor sensor proved legible.
"""
from __future__ import annotations

from dataclasses import dataclass

from .arbiter import fan_lever, preset_lever, switch_lever, temperature_lever
from .model import HouseState, ZoneSnapshot, _is_free_cooling

# Owner name -> (state code, Italian label). First match in priority order.
_OWNER_STATES: tuple[tuple[str, str, str], ...] = (
    ("RackGuardController", "guardia", "guardia rack"),
    ("P1GuardController", "guardia", "guardia P1"),
    ("SteadyGovernorController", "governor", "governor salotto"),
    ("NightSilenceController", "notte", "camere silenziose"),
    ("DemandShedController", "riposo_pdc", "riposo PdC"),
    ("CoolingController", "banda", "controllo a banda"),
    ("fan_actuator:rearm", "riattivazione", "riattivazione ventola"),
)
_HELD = ("override", "manual-hold")

_FAN_MODE_IT = {"auto": "in AUTO KNX", "manual": "in manuale", "off": "SPENTA"}


@dataclass(frozen=True)
class LiveClimate:
    setpoint: float | None
    preset: str | None


@dataclass(frozen=True)
class RoomExplain:
    state: str
    attributes: dict
    sentence: str


def _fmt(v, unit="", nd=1) -> str:
    if v is None:
        return "-"
    if isinstance(v, float):
        return f"{v:.{nd}f}{unit}"
    return f"{v}{unit}"


def explain_room(
    z: ZoneSnapshot,
    state: HouseState,
    *,
    live: LiveClimate,
    desired: dict,
    owners: dict,
    decisions: dict,
    fan_owner: str | None,
    fan_note: str | None,
    telemetry=None,            # UnitTelemetry | None
    validity=None,             # ModelValidity | None
    shed_reason: str | None = None,
    enabled: bool = True,      # master switch
) -> RoomExplain:
    tl = temperature_lever(z.climate) if z.climate else None
    pl = preset_lever(z.climate) if z.climate else None
    unit_levers = [tl, pl]
    for fan, man in z.fancoil_units:
        unit_levers += [fan_lever(fan), switch_lever(man)]
    unit_levers = [lv for lv in unit_levers if lv]
    lever_owners = [owners.get(lv) for lv in unit_levers if lv in desired]
    held = [lv for lv in unit_levers if (decisions.get(lv) or {}).get("note") in _HELD]

    # -- state, highest priority first --------------------------------------
    label = None
    if not enabled:
        code, label = "nativa", "supervisore spento (solo termostato KNX)"
    elif not z.enabled:
        code, label = "disabilitata", "zona disabilitata (#10)"
    elif z.paused:
        code, label = "finestre_aperte", "in pausa: finestre aperte"
    elif _is_free_cooling(state) and z.emitter == "fancoil":
        code, label = "free_cooling", "free cooling: fuori è più fresco"
    elif held:
        code, label = "manuale", "comando manuale rispettato"
    else:
        code = None
        for owner_name, c, lab in _OWNER_STATES:
            if owner_name in lever_owners or fan_owner == owner_name:
                code, label = c, lab
                break
        if code is None:
            if "house_mode_policy" in lever_owners:
                code, label = "supervisionata", f"modalità casa {state.house_mode}"
            else:
                code, label = "nativa", "solo termostato KNX"

    want_sp = desired.get(tl) if tl else None
    want_preset = desired.get(pl) if pl else None
    t = telemetry
    duty = round(t.valve_duty * 100.0) if t is not None and t.valve_duty is not None else None
    attrs = {
        "temperature": z.temp,
        "setpoint": live.setpoint,
        "setpoint_target": want_sp,
        "setpoint_owner": owners.get(tl) if tl in desired else None,
        "preset": live.preset,
        "preset_target": want_preset,
        "preset_owner": owners.get(pl) if pl in desired else None,
        "valve_open": t.valve_open if t else z.demand,
        "valve_duty_1h": duty,
        "valve_strokes_1h": t.valve_strokes if t else None,
        "fan_delivered": t.fan_delivered if t else z.fan_pct,
        "fan_owner": fan_owner,
        "fan_note": fan_note,
        "manual_levers": held,
        "model_valid": (validity.abc_valid and validity.k_valid) if validity else None,
        "model_reasons": list(validity.reasons) if validity else [],
        "mass_phase": state.mass_phase,
        "shed_reason": shed_reason,
        "house_mode": state.house_mode,
    }

    # -- sentence -------------------------------------------------------------
    parts = [f"{z.name}: {_fmt(z.temp, ' °C')}"]
    sp = f"termostato a {_fmt(live.setpoint, ' °C')}"
    if want_sp is not None and live.setpoint is not None and abs(
        float(want_sp) - float(live.setpoint)
    ) > 0.05:
        sp += f" (obiettivo {_fmt(float(want_sp), ' °C')})"
    parts.append(sp)
    why = label
    if code == "riposo_pdc" and shed_reason:
        why = f"{label}: {shed_reason}"
    if state.mass_phase and code == "supervisionata":
        why += (
            " — mantenimento massa: accumula fresco"
            if state.mass_phase == "bank" else " — mantenimento massa: lascia correre il picco"
        )
    parts.append(f"[{why}]")
    if t is not None:
        valve = {True: "valvola APERTA", False: "valvola chiusa"}.get(t.valve_open, "valvola ?")
        if duty is not None:
            valve += f", aperta {duty}% nell'ultima ora ({t.valve_strokes} aperture)"
        parts.append(valve)
        mode = (
            "off" if t.fan_delivered == 0 and t.manuale_on is False
            else "manual" if t.manuale_on else "auto" if t.manuale_on is False else None
        )
        fan = f"ventola {_fmt(t.fan_delivered, '%')} {_FAN_MODE_IT.get(mode, '')}".rstrip()
        if fan_owner and fan_owner not in ("knx_auto", "manual"):
            fan += f" (comandata da {fan_owner})"
        if fan_note == "pct_in_auto":
            fan += " — attenzione: % scritta mentre KNX è in AUTO"
        parts.append(fan)
    if validity is not None and not (validity.abc_valid and validity.k_valid):
        reasons = ", ".join(validity.reasons) or "-"
        parts.append(f"modello non affidabile ({reasons}): uso i valori di default")
    return RoomExplain(state=code, attributes=attrs, sentence="; ".join(parts) + ".")
