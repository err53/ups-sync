"""Per-server probes and commands: TCP reachability, iDRAC Redfish, Proxmox API."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import aiohttp

_SYSTEM = "/redfish/v1/Systems/System.Embedded.1"
_TIMEOUT = aiohttp.ClientTimeout(total=12)


class ServerError(Exception):
    """A command to a server or its iDRAC failed."""


@dataclass(frozen=True)
class ServerConfig:
    name: str
    host: str
    reach_port: int
    idrac_host: str | None = None
    idrac_user: str | None = None
    idrac_password: str | None = None
    pve_node: str | None = None
    pve_token: str | None = None  # user@realm!tokenid=secret


class Server:
    def __init__(self, cfg: ServerConfig, session: aiohttp.ClientSession) -> None:
        self.cfg = cfg
        self._session = session

    async def reachable(self) -> bool:
        try:
            _, writer = await asyncio.wait_for(
                asyncio.open_connection(self.cfg.host, self.cfg.reach_port), 3
            )
        except ConnectionRefusedError:
            return True  # a refusal comes from a running kernel
        except (TimeoutError, OSError):
            return False
        writer.close()
        return True

    async def _idrac(self, method: str, path: str, body: dict | None = None) -> dict:
        c = self.cfg
        if not c.idrac_host:
            raise ServerError("no iDRAC configured")
        try:
            async with self._session.request(
                method,
                f"https://{c.idrac_host}{path}",
                json=body,
                auth=aiohttp.BasicAuth(c.idrac_user or "", c.idrac_password or ""),
                ssl=False,
                timeout=_TIMEOUT,
            ) as resp:
                if resp.status >= 300:
                    raise ServerError(f"iDRAC HTTP {resp.status}")
                return await resp.json(content_type=None) if method == "GET" else {}
        except (TimeoutError, aiohttp.ClientError, ValueError) as exc:
            raise ServerError(f"iDRAC: {exc!r}") from exc

    async def chassis_on(self) -> bool | None:
        """True on, False off, None if the iDRAC is absent or silent."""
        try:
            state = (await self._idrac("GET", _SYSTEM)).get("PowerState")
        except ServerError:
            return None
        # PoweringOn, PoweringOff and anything unexpected count as on.
        return None if state is None else state != "Off"

    async def _reset(self, kind: str) -> None:
        await self._idrac(
            "POST", f"{_SYSTEM}/Actions/ComputerSystem.Reset", {"ResetType": kind}
        )

    async def power_on(self) -> None:
        await self._reset("On")

    async def _pve_shutdown(self) -> None:
        c = self.cfg
        try:
            async with self._session.post(
                f"https://{c.host}:8006/api2/json/nodes/{c.pve_node}/status",
                data={"command": "shutdown"},
                headers={"Authorization": f"PVEAPIToken={c.pve_token}"},
                ssl=False,
                timeout=_TIMEOUT,
            ) as resp:
                if resp.status >= 300:
                    raise ServerError(f"Proxmox HTTP {resp.status}")
        except (TimeoutError, aiohttp.ClientError) as exc:
            raise ServerError(f"Proxmox: {exc!r}") from exc

    async def shutdown(self) -> str:
        """Ask the OS to shut down. Returns the channel that accepted it."""
        errors = []
        if self.cfg.pve_node and self.cfg.pve_token:
            try:
                await self._pve_shutdown()
                return "proxmox"
            except ServerError as exc:
                errors.append(str(exc))
        if self.cfg.idrac_host:
            try:
                await self._reset("GracefulShutdown")
                return "idrac"
            except ServerError as exc:
                errors.append(str(exc))
        raise ServerError("; ".join(errors) or "no shutdown channel configured")
