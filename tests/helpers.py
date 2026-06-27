"""Shared test helpers for the engine-driven (Supervisor) world.

After the A4 cutover the migrated #2/#4/#10 only act when the master switch is on
(strict deploy-dark), and the engine writes a lever only when the live state
differs from the desired value. So engine-path tests must (1) enable the master
switch and (2) seed thermostat states the reconcile can diff against.
"""
from __future__ import annotations

from custom_components.villa_hvac.controller import controllable_zones

SUPERVISOR = "switch.supervisor"


async def enable_supervisor(hass) -> None:
    """Flip the master Supervisor switch on so the engine actuates."""
    await hass.services.async_call(
        "switch", "turn_on", {"entity_id": SUPERVISOR}, blocking=True
    )
    await hass.async_block_till_done()


def seed_thermostats(hass, *, hvac="cool", preset="comfort", temperature=None) -> None:
    """Give every controllable thermostat a live state (real KNX climates always
    have one) so the engine's reconcile has a current value to compare against."""
    for _zone_id, climate in controllable_zones():
        attrs = {"preset_mode": preset}
        if temperature is not None:
            attrs["temperature"] = temperature
        hass.states.async_set(climate, hvac, attrs)
