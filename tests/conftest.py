"""Shared pytest fixtures for the villa_hvac tests."""
from __future__ import annotations

import pytest

pytest_plugins = "pytest_homeassistant_custom_component"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Enable loading custom integrations in every test."""
    yield


@pytest.fixture(autouse=True)
def _reset_season_memory():
    """current_season remembers the last conclusive read per entry (v0.76.0);
    keep tests independent."""
    from custom_components.villa_hvac.controller import _LAST_SEASON

    _LAST_SEASON.clear()
    yield
    _LAST_SEASON.clear()
