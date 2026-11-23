import pytest

# Shared with test_entities.py.
from tests.test_integration import rig  # noqa: F401


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    return
