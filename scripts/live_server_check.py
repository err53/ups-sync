"""Live, non-disruptive check of the server client against real machines.

Reads reachability and chassis power, then sends iDRAC "power on" to a
server that is ALREADY on. The iDRAC refuses it without doing anything,
which proves the account may use the power action. Nothing is shut down.

usage: python scripts/live_server_check.py name host reach_port idrac_host idrac_user:pass
"""

import asyncio
import importlib.util
import pathlib
import sys

import aiohttp

spec = importlib.util.spec_from_file_location(
    "srv", pathlib.Path(__file__).parents[1] / "custom_components/ups_sync/servers.py"
)
srv = importlib.util.module_from_spec(spec)
sys.modules["srv"] = srv
spec.loader.exec_module(srv)


async def main(name, host, port, idrac, creds):
    user, _, password = creds.partition(":")
    cfg = srv.ServerConfig(name, host, int(port), idrac, user, password)
    async with aiohttp.ClientSession() as session:
        s = srv.Server(cfg, session)
        reach, chassis = await s.reachable(), await s.chassis_on()
        print(f"{name}: reachable={reach} chassis_on={chassis}")
        assert reach and chassis is True, "expected a running server"
        try:
            await s.power_on()
            print(f"{name}: power-on accepted (no effect, already on)")
        except srv.ServerError as exc:
            assert "401" not in str(exc) and "403" not in str(exc), exc
            print(f"{name}: power-on refused as expected for a running server: {exc}")
        bad = srv.Server(
            srv.ServerConfig(name, host, 9, idrac, user, "wrong-password"), session
        )
        assert await bad.reachable() is False and await bad.chassis_on() is None
        print(f"{name}: closed port -> unreachable, bad password -> chassis unknown")


asyncio.run(main(*sys.argv[1:]))
