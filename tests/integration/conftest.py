"""Fixtures for tests that run the integration inside Home Assistant."""

import pytest


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Let Home Assistant load integrations from custom_components/."""
    yield
