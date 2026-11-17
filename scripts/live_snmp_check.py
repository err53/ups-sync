"""Live check against a real APC card. Toggles ONLY the group given (use an empty one).

usage: python scripts/live_snmp_check.py <host> <group> [read] [write]
"""

import asyncio
import importlib.util
import pathlib
import sys

from pysnmp.hlapi.v3arch.asyncio import SnmpEngine

spec = importlib.util.spec_from_file_location(
    "apc", pathlib.Path(__file__).parents[1] / "custom_components/ups_sync/snmp.py"
)
apc = importlib.util.module_from_spec(spec)
sys.modules["apc"] = apc
spec.loader.exec_module(apc)


async def main(host: str, group: int, read: str = "public", write: str = "private"):
    client = apc.ApcClient(SnmpEngine(), host, read, write, group)
    first = await client.read()
    print("start  ", first)
    for want in (not first.group_on, first.group_on):
        await client.set_group(want)
        await asyncio.sleep(3)
        now = await client.read()
        print("set", "on " if want else "off", "->", "on" if now.group_on else "off")
        assert now.group_on == want, "group did not follow the command"
    print("OK: group", group, "toggled and returned to its original state")


asyncio.run(main(sys.argv[1], int(sys.argv[2]), *sys.argv[3:]))
