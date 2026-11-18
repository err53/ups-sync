"""The icon ships inside the integration and Home Assistant serves it."""

import struct
from pathlib import Path

import pytest
from homeassistant.setup import async_setup_component

import custom_components.ups_sync as integration
from custom_components.ups_sync.const import DOMAIN

BRAND = Path(integration.__file__).parent / "brand"


@pytest.mark.parametrize(("name", "size"), [("icon.png", 256), ("icon@2x.png", 512)])
def test_icon_is_a_square_png_of_the_expected_size(name, size):
    data = (BRAND / name).read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    assert struct.unpack(">II", data[16:24]) == (size, size)


@pytest.mark.parametrize(
    ("asked", "served"),
    [
        ("icon.png", "icon.png"),
        ("icon@2x.png", "icon@2x.png"),
        ("dark_icon@2x.png", "icon@2x.png"),
        ("logo.png", "icon.png"),
    ],
)
async def test_home_assistant_serves_the_icon(hass, hass_client, asked, served):
    assert await async_setup_component(hass, "brands", {})
    client = await hass_client()
    resp = await client.get(f"/api/brands/integration/{DOMAIN}/{asked}")
    assert resp.status == 200
    assert await resp.read() == (BRAND / served).read_bytes()
