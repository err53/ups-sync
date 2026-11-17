"""APC Network Management Card over SNMPv2c (PowerNet MIB)."""

from __future__ import annotations

from dataclasses import dataclass

from pysnmp.hlapi.v3arch.asyncio import (
    CommunityData,
    ContextData,
    ObjectIdentity,
    ObjectType,
    SnmpEngine,
    UdpTransportTarget,
    get_cmd,
    set_cmd,
)
from pysnmp.proto.rfc1902 import Integer32
from pysnmp.smi import view

_APC = "1.3.6.1.4.1.318.1.1.1"
OID_STATUS = f"{_APC}.4.1.1.0"  # upsBasicOutputStatus
OID_RUNTIME = f"{_APC}.2.2.3.0"  # upsAdvBatteryRunTimeRemaining, 1/100 s
OID_WATTS = f"{_APC}.4.2.8.0"  # upsAdvOutputActivePower
OID_LOW_BATT = f"{_APC}.5.2.8.0"  # upsAdvConfigLowBatteryRunTime, 1/100 s
OID_GROUP_STATE = f"{_APC}.12.1.2.1.3"  # .<group>: 1 on, 2 off
OID_GROUP_CMD = f"{_APC}.12.3.2.1.3"  # .<group>: 1 immediate on, 2 immediate off

STATUS_ON_BATTERY = 3
# onLine, onSmartBoost, onSmartTrim, ecoMode. Bypass, self-test, off: unclear, no action.
STATUS_INPUT_OK = {2, 4, 12, 13}


class UpsError(Exception):
    """The UPS did not give a usable answer."""


@dataclass(frozen=True)
class UpsReading:
    input_present: bool
    on_battery: bool
    low_battery: bool
    runtime_min: float
    load_w: float
    group_on: bool


def make_engine() -> SnmpEngine:
    """Blocking. Loads the MIB files now so no request does it in the event loop."""
    engine = SnmpEngine()
    mib_view = view.MibViewController(
        engine.message_dispatcher.mib_instrum_controller.get_mib_builder()
    )
    engine.cache["mibViewController"] = mib_view
    mib_view.mibBuilder.load_modules()
    return engine


def parse_reading(status: int, runtime: int, watts: int, low: int, group: int) -> UpsReading:
    """Turn raw PowerNet values into a reading, or raise if the state is unclear."""
    on_battery = status == STATUS_ON_BATTERY
    if not on_battery and status not in STATUS_INPUT_OK:
        raise UpsError(f"UPS output status {status} is neither online nor on battery")
    if group not in (1, 2):
        raise UpsError(f"outlet group state {group} is neither on nor off")
    return UpsReading(
        input_present=not on_battery,
        on_battery=on_battery,
        low_battery=on_battery and runtime < low,
        runtime_min=runtime / 6000,
        load_w=float(watts),
        group_on=group == 1,
    )


class ApcClient:
    def __init__(
        self, engine: SnmpEngine, host: str, read: str, write: str, group: int
    ) -> None:
        self._engine = engine
        self._host = host
        self._read = CommunityData(read, mpModel=1)
        self._write = CommunityData(write, mpModel=1)
        self._group = group

    async def _target(self) -> UdpTransportTarget:
        return await UdpTransportTarget.create((self._host, 161), timeout=2, retries=2)

    @staticmethod
    def _check(err_ind, err_status, binds) -> list[int]:
        if err_ind or err_status:
            raise UpsError(str(err_ind or err_status.prettyPrint()))
        out = []
        for _, val in binds:
            try:
                out.append(int(val))
            except (TypeError, ValueError) as exc:  # noSuchObject etc.
                raise UpsError(f"non-numeric SNMP value {val!r}") from exc
        return out

    async def read(self) -> UpsReading:
        oids = (
            OID_STATUS,
            OID_RUNTIME,
            OID_WATTS,
            OID_LOW_BATT,
            f"{OID_GROUP_STATE}.{self._group}",
        )
        try:
            err_ind, err_status, _, binds = await get_cmd(
                self._engine,
                self._read,
                await self._target(),
                ContextData(),
                *(ObjectType(ObjectIdentity(o)) for o in oids),
            )
        except Exception as exc:  # pysnmp raises many unrelated types
            raise UpsError(str(exc)) from exc
        return parse_reading(*self._check(err_ind, err_status, binds))

    async def set_group(self, on: bool) -> None:
        try:
            err_ind, err_status, _, binds = await set_cmd(
                self._engine,
                self._write,
                await self._target(),
                ContextData(),
                ObjectType(
                    ObjectIdentity(f"{OID_GROUP_CMD}.{self._group}"),
                    Integer32(1 if on else 2),
                ),
            )
        except Exception as exc:
            raise UpsError(str(exc)) from exc
        self._check(err_ind, err_status, binds)
