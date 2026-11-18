"""The SNMP and server clients: exact OIDs and values, state mapping, probes."""

from unittest.mock import patch

import pytest

from custom_components.ups_sync import snmp
from custom_components.ups_sync.servers import Server, ServerConfig, ServerError

APC = "1.3.6.1.4.1.318.1.1.1"


class _Val:
    def __init__(self, v):
        self.v = v

    def __int__(self):
        return self.v


async def _target(self):
    return "target"


def _oid_and_value(object_type):
    identity, value = object_type._ObjectType__args
    return identity._ObjectIdentity__args[0], value


async def test_read_asks_for_the_right_oids_in_order():
    seen = []

    async def get_cmd(engine, community, target, context, *objects):
        seen.extend(_oid_and_value(o)[0] for o in objects)
        return None, None, 0, [(None, _Val(v)) for v in (2, 342000, 412, 96000, 1, 100)]

    with patch.object(snmp, "get_cmd", get_cmd), patch.object(snmp.ApcClient, "_target", _target):
        r = await snmp.ApcClient(None, "h", "public", "private", 2).read()
    assert seen == [
        f"{APC}.4.1.1.0",  # output status
        f"{APC}.2.2.3.0",  # runtime remaining
        f"{APC}.4.2.8.0",  # output watts
        f"{APC}.5.2.8.0",  # low-battery duration
        f"{APC}.12.1.2.1.3.2",  # state of group 2
        f"{APC}.2.2.1.0",  # battery charge, optional
    ]
    assert r.runtime_min == 57 and r.load_w == 412 and r.group_on
    assert r.charge_pct == 100


async def test_a_card_without_a_charge_reading_still_reads():
    from pysnmp.proto.rfc1905 import NoSuchObject

    for last in ([(None, NoSuchObject())], []):

        async def get_cmd(engine, community, target, context, *objects, last=last):
            values = [(None, _Val(v)) for v in (2, 342000, 412, 96000, 1)]
            return None, None, 0, values + last

        with patch.object(snmp, "get_cmd", get_cmd), patch.object(
            snmp.ApcClient, "_target", _target
        ):
            r = await snmp.ApcClient(None, "h", "public", "private", 2).read()
        assert r.charge_pct is None and r.runtime_min == 57 and r.group_on


@pytest.mark.parametrize(("on", "value"), [(True, 1), (False, 2)])
async def test_set_group_writes_immediate_on_or_off_to_that_group_only(on, value):
    seen = []

    async def set_cmd(engine, community, target, context, *objects):
        seen.append((community.communityName, *_oid_and_value(objects[0])))
        return None, None, 0, [(None, _Val(value))]

    with patch.object(snmp, "set_cmd", set_cmd), patch.object(snmp.ApcClient, "_target", _target):
        await snmp.ApcClient(None, "h", "public", "private", 3).set_group(on)
    assert len(seen) == 1
    community, oid, written = seen[0]
    assert community == "private" and oid == f"{APC}.12.3.2.1.3.3" and int(written) == value


async def test_group_name_is_read_from_the_card_and_never_fails():
    from pysnmp.proto.rfc1902 import OctetString
    from pysnmp.proto.rfc1905 import NoSuchInstance

    seen = []

    def answer(value):
        async def get_cmd(engine, community, target, context, *objects):
            seen.extend(_oid_and_value(o)[0] for o in objects)
            return None, None, 0, [(None, value)]

        return get_cmd

    async def broken(*a):
        raise OSError("network down")

    async def timeout(*a):
        return "timeout", None, 0, []

    async def empty(*a):
        return None, None, 0, []

    client = snmp.ApcClient(None, "h", "public", "private", 2)
    with patch.object(snmp.ApcClient, "_target", _target):
        with patch.object(snmp, "get_cmd", answer(OctetString("Servers "))):
            assert await client.group_name() == "Servers"
        assert seen == [f"{APC}.12.2.2.1.2.2"]
        # A real name that merely looks like an SNMP error is kept.
        with patch.object(snmp, "get_cmd", answer(OctetString("No such server"))):
            assert await client.group_name() == "No such server"
        for fake in (broken, timeout, empty, answer(NoSuchInstance()), answer(object())):
            with patch.object(snmp, "get_cmd", fake):
                assert await client.group_name() == ""


async def test_snmp_errors_become_ups_errors():
    async def timeout(*a):
        return "No SNMP response received before timeout", None, 0, []

    async def no_such(*a):
        return None, None, 0, [(None, "No Such Instance")] * 5

    for fake in (timeout, no_such):
        with patch.object(snmp, "get_cmd", fake), patch.object(snmp.ApcClient, "_target", _target):
            with pytest.raises(snmp.UpsError):
                await snmp.ApcClient(None, "h", "public", "private", 2).read()


class _Resp:
    def __init__(self, status, body=None):
        self.status, self._body = status, body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def json(self, content_type=None):
        return self._body


class _Session:
    """Records requests; answers from a {(method, url): (status, body)} table."""

    def __init__(self, table):
        self.table, self.sent = table, []

    def request(self, method, url, json=None, **kw):
        self.sent.append((method, url, json))
        return _Resp(*self.table.get((method, url), (404, None)))

    def post(self, url, data=None, headers=None, **kw):
        self.sent.append(("POST", url, data, headers))
        return _Resp(*self.table.get(("POST", url), (404, None)))


SYS = "https://idrac/redfish/v1/Systems/System.Embedded.1"
RESET = f"{SYS}/Actions/ComputerSystem.Reset"
PVE = "https://host:8006/api2/json/nodes/pve1/status"
CFG = ServerConfig("s", "host", 8006, "idrac", "u", "p", "pve1", "u@pve!t=x")


@pytest.mark.parametrize(
    ("state", "want"),
    [("On", True), ("Off", False), ("PoweringOn", True), ("PoweringOff", True), (None, None)],
)
async def test_chassis_state_mapping(state, want):
    """Anything but a clear Off counts as on, so it can never permit a cut."""
    session = _Session({("GET", SYS): (200, {"PowerState": state} if state else {})})
    assert await Server(CFG, session).chassis_on() is want


async def test_chassis_unknown_when_idrac_fails_or_absent():
    assert await Server(CFG, _Session({("GET", SYS): (401, None)})).chassis_on() is None
    assert await Server(ServerConfig("s", "host", 22), _Session({})).chassis_on() is None


async def test_shutdown_prefers_proxmox_then_falls_back_to_idrac():
    session = _Session({("POST", PVE): (200, None), ("POST", RESET): (204, None)})
    assert await Server(CFG, session).shutdown() == "proxmox"
    assert session.sent == [
        ("POST", PVE, {"command": "shutdown"}, {"Authorization": "PVEAPIToken=u@pve!t=x"})
    ]

    session = _Session({("POST", PVE): (500, None), ("POST", RESET): (204, None)})
    assert await Server(CFG, session).shutdown() == "idrac"
    assert session.sent[-1] == ("POST", RESET, {"ResetType": "GracefulShutdown"})

    session = _Session({("POST", PVE): (500, None), ("POST", RESET): (409, None)})
    with pytest.raises(ServerError):
        await Server(CFG, session).shutdown()


async def test_power_on_uses_reset_on():
    session = _Session({("POST", RESET): (204, None)})
    await Server(CFG, session).power_on()
    assert session.sent == [("POST", RESET, {"ResetType": "On"})]


@pytest.mark.parametrize(
    ("error", "want"),
    [(None, True), (ConnectionRefusedError(), True), (TimeoutError(), False), (OSError(65, "no route"), False)],
)
async def test_reach_probe(error, want):
    """A refused connection comes from a live kernel; only silence is "down"."""

    class Writer:
        def close(self):
            pass

    async def open_connection(host, port):
        if error:
            raise error
        return None, Writer()

    with patch("custom_components.ups_sync.servers.asyncio.open_connection", open_connection):
        assert await Server(CFG, None).reachable() is want
