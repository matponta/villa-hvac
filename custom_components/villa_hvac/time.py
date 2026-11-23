"""v0.96.0 night band start/end times (STORY_NIGHT_SCHEDULE.md).

`time.fascia_notturna_inizio` / `_fine` (winter, default 21:30 -> 07:00) and
the summer pair `_estate` (default 23:30 -> 07:00). Restored across restarts;
read every cycle through `controller.night_band_times`.
"""
from __future__ import annotations

from datetime import time

from homeassistant.components.time import TimeEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from . import VillaHvacConfigEntry
from .const import NIGHT_BAND_TIMES


async def async_setup_entry(
    hass: HomeAssistant,
    entry: VillaHvacConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the night band times."""
    async_add_entities([NightBandTime(entry, suffix) for suffix in NIGHT_BAND_TIMES])


def _parse(raw: str | None) -> time | None:
    try:
        return time.fromisoformat(str(raw))
    except (TypeError, ValueError):
        return None


class NightBandTime(TimeEntity, RestoreEntity):
    """One edge of the night band."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, entry: VillaHvacConfigEntry, suffix: str) -> None:
        name, default, icon = NIGHT_BAND_TIMES[suffix]
        self._entry = entry
        self._attr_name = name
        self._attr_icon = icon
        self._attr_unique_id = f"{entry.entry_id}_{suffix}"
        self._attr_native_value = _parse(default)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if (last := await self.async_get_last_state()) is not None:
            if (parsed := _parse(last.state)) is not None:
                self._attr_native_value = parsed

    async def async_set_value(self, value: time) -> None:
        self._attr_native_value = value
        self.async_write_ha_state()
        engine = getattr(self._entry.runtime_data, "engine", None)
        if engine is not None:
            await engine.request_run()
