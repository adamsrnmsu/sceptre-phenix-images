#!/usr/bin/env python3
"""OPC UA gateway for the sceptre linux_fuxa stack (Linux stand-in for Kepware TopServer).

Polls each RTU over Modbus/TCP and serves the values as OPC UA nodes
`ns=2;s=<rtu>.<device>.<field>` (Double, engineering units). Writing a node
that has a holding register forwards the value to the RTU (gate/valve commands).

Config: /etc/sceptre/opc_gateway.json, rendered by the sceptre app:
  {"opc_endpoint": "opc.tcp://0.0.0.0:4840", "poll_interval": 1.0, "scale": [0, 1000],
   "rtus": [{"name": "rtu-1", "host": "10.1.1.101", "port": 502, "unit": 1,
             "points": [{"node": "rtu-1.lock.height",
                         "read":  {"type": "input-register",   "address": 30001},
                         "write": {"type": "holding-register", "address": 40000} | null}]}]}
Register <-> engineering units: bennu scales to 16 bit linearly over `scale`
(Server.cpp: raw = 65535*(v-min)/(max-min)), same as the Kepware tag scaling.

Run:   opcua_gateway.py --config FILE      Self-test: python3 -I opcua_gateway.py --self-test
Needs: asyncua==2.1.0 pymodbus==3.16.2
"""
import argparse
import asyncio
import json
import logging
import sys

from asyncua import Server, ua
from pymodbus.client import AsyncModbusTcpClient

log = logging.getLogger("opcua_gateway")
NS_URI = "urn:sceptre:opc"
FULL = 65535.0


def to_eng(raw, lo, hi):
    return round(lo + raw / FULL * (hi - lo), 3)


def to_raw(v, lo, hi):
    return max(0, min(65535, round((v - lo) / (hi - lo) * FULL)))


def runs(addrs):
    """Sorted addresses -> [(start, count)] of contiguous runs (bennu rejects unmapped gaps)."""
    out = []
    for a in sorted(set(addrs)):
        if out and a == out[-1][0] + out[-1][1]:
            out[-1][1] += 1
        else:
            out.append([a, 1])
    return out


class Gateway:
    def __init__(self, cfg):
        self.cfg = cfg
        self.lo, self.hi = cfg.get("scale", [0, 1000])
        self.nodes = {}   # node id string -> asyncua Node
        self.cache = {}   # node id -> last value we set; a differing datachange is a client write
        self.points = {}  # node id -> (rtu dict, point dict)
        self.clients = {}

    async def start(self):
        s = self.server = Server()
        await s.init()
        s.set_endpoint(self.cfg.get("opc_endpoint", "opc.tcp://0.0.0.0:4840"))
        s.set_server_name("sceptre OPC gateway")
        s.set_security_policy([ua.SecurityPolicyType.NoSecurity])
        idx = await s.register_namespace(NS_URI)
        assert idx == 2, f"expected namespace index 2, got {idx}"  # node ids are published as ns=2
        for rtu in self.cfg["rtus"]:
            for p in rtu["points"]:
                n = await s.nodes.objects.add_variable(
                    ua.NodeId(p["node"], idx), ua.QualifiedName(p["node"], idx), 0.0, ua.VariantType.Double)
                if p.get("write"):
                    await n.set_writable()
                self.nodes[p["node"]] = n
                self.points[p["node"]] = (rtu, p)
                self.cache[p["node"]] = 0.0
        await s.start()
        sub = await s.create_subscription(100, self)
        await sub.subscribe_data_change([self.nodes[k] for k, (_, p) in self.points.items() if p.get("write")])
        self.task = asyncio.create_task(self.poll_forever())

    async def stop(self):
        self.task.cancel()
        for c in self.clients.values():
            c.close()
        await self.server.stop()

    # asyncua subscription handler: forward client writes to the RTU holding register
    def datachange_notification(self, node, val, data):
        nid = node.nodeid.Identifier
        if val is not None and nid in self.cache and val != self.cache[nid]:  # val None = our own Bad-status write
            self.cache[nid] = val
            asyncio.create_task(self.write_through(nid, val))

    async def write_through(self, nid, val):
        rtu, p = self.points[nid]
        w = p["write"]
        try:
            c = await self.client(rtu)
            r = await c.write_register(w["address"], to_raw(val, self.lo, self.hi), device_id=rtu.get("unit", 1))
            if r.isError():
                raise IOError(str(r))
            log.info("%s <- %s (holding %d)", nid, val, w["address"])
        except Exception as e:  # noqa: BLE001 - keep serving; the next poll shows the real state
            log.warning("write %s failed: %s", nid, e)

    async def client(self, rtu):
        c = self.clients.get(rtu["name"])
        if c is None:
            c = self.clients[rtu["name"]] = AsyncModbusTcpClient(rtu["host"], port=rtu.get("port", 502), timeout=3)
        if not c.connected:
            await c.connect()
        return c

    async def poll_rtu(self, rtu):
        unit = rtu.get("unit", 1)
        regs = {}  # (type, address) -> raw
        try:
            c = await self.client(rtu)
            want = {}
            for p in rtu["points"]:
                r = p["read"]
                want.setdefault(r["type"], []).append(r["address"])
            for typ, addrs in want.items():
                fn = c.read_input_registers if typ == "input-register" else c.read_holding_registers
                for start, count in runs(addrs):
                    r = await fn(start, count=count, device_id=unit)
                    if r.isError():
                        raise IOError(str(r))
                    regs.update({(typ, start + i): v for i, v in enumerate(r.registers)})
            good = True
        except Exception as e:  # noqa: BLE001
            log.warning("poll %s failed: %s", rtu["name"], e)
            good = False
        for p in rtu["points"]:
            nid = p["node"]
            if good:
                v = to_eng(regs[(p["read"]["type"], p["read"]["address"])], self.lo, self.hi)
                self.cache[nid] = v  # set before write so our own datachange is not seen as a client write
                await self.nodes[nid].write_value(ua.DataValue(ua.Variant(v, ua.VariantType.Double)))
            else:  # keep last value, flag Bad so clients (Telegraf, FUXA) see the outage
                dv = ua.DataValue(ua.Variant(self.cache[nid], ua.VariantType.Double),
                                  StatusCode=ua.StatusCode(ua.StatusCodes.BadCommunicationError))
                await self.nodes[nid].write_value(dv)

    async def poll_forever(self):
        while True:
            for r in await asyncio.gather(*(self.poll_rtu(r) for r in self.cfg["rtus"]), return_exceptions=True):
                if isinstance(r, Exception):
                    log.exception("poll error", exc_info=r)
            await asyncio.sleep(self.cfg.get("poll_interval", 1.0))


async def serve(cfg):
    g = Gateway(cfg)
    await g.start()
    log.info("serving %d nodes on %s", len(g.nodes), cfg.get("opc_endpoint"))
    try:
        await asyncio.Event().wait()
    finally:
        await g.stop()


# ---------------------------------------------------------------- self-test
async def _self_test():
    from asyncua import Client
    from pymodbus.server import ModbusTcpServer
    from pymodbus.simulator import DataType, SimData, SimDevice

    # fake RTU: input regs at 30000.. (height 250, lock 500), holding at 40000.. (bennu keys by absolute address)
    dev = SimDevice(1, simdata=[
        SimData(30000, values=[to_raw(250.0, 0, 1000), to_raw(500.0, 0, 1000), 0, 0, 0, 0, 0, 0], datatype=DataType.REGISTERS),
        SimData(40000, values=[0] * 4, datatype=DataType.REGISTERS)])
    mb = ModbusTcpServer(dev, address=("127.0.0.1", 15020))
    mb_task = asyncio.create_task(mb.serve_forever())
    await asyncio.sleep(0.5)
    probe = AsyncModbusTcpClient("127.0.0.1", port=15020)
    await probe.connect()

    async def hr0():
        return (await probe.read_holding_registers(40000, count=1, device_id=1)).registers[0]

    cfg = {"opc_endpoint": "opc.tcp://127.0.0.1:14840", "poll_interval": 0.2, "scale": [0, 1000],
           "rtus": [{"name": "rtu-t", "host": "127.0.0.1", "port": 15020, "unit": 1, "points": [
               {"node": "rtu-t.lowerwater.height", "read": {"type": "input-register", "address": 30000}, "write": None},
               {"node": "rtu-t.lock.height", "read": {"type": "input-register", "address": 30001}, "write": None},
               {"node": "rtu-t.lowergate.open", "read": {"type": "input-register", "address": 30002},
                "write": {"type": "holding-register", "address": 40000}}]}]}
    g = Gateway(cfg)
    await g.start()
    try:
        async with Client("opc.tcp://127.0.0.1:14840") as c:
            ns = await c.get_namespace_index(NS_URI)
            assert ns == 2, ns
            n = c.get_node("ns=2;s=rtu-t.lowerwater.height")
            for _ in range(30):
                v = await n.read_value()
                if abs(v - 250.0) < 0.1:
                    break
                await asyncio.sleep(0.1)
            assert abs(v - 250.0) < 0.1, f"read-through failed: {v}"
            assert abs(await c.get_node("ns=2;s=rtu-t.lock.height").read_value() - 500.0) < 0.1
            # write-through: command the gate, expect the holding register to follow
            await c.get_node("ns=2;s=rtu-t.lowergate.open").write_value(ua.DataValue(ua.Variant(1000.0, ua.VariantType.Double)))
            for _ in range(30):
                if await hr0() == 65535:
                    break
                await asyncio.sleep(0.1)
            assert await hr0() == 65535, "write-through failed"
            # no echo: our own polling must not rewrite the holding register
            await probe.write_register(40000, 123, device_id=1)
            await asyncio.sleep(0.6)
            assert await hr0() == 123, "poll echoed into holding register"
            # outage -> Bad status
            probe.close()
            await mb.shutdown()
            mb_task.cancel()
            for _ in range(40):
                dv = await n.read_data_value(raise_on_bad_status=False)
                if not dv.StatusCode.is_good():
                    break
                await asyncio.sleep(0.2)
            assert not dv.StatusCode.is_good(), "RTU outage not flagged"
    finally:
        await g.stop()
    assert runs([30000, 30001, 30002, 30005, 30006]) == [[30000, 3], [30005, 2]]
    print("opcua_gateway self-test ok")


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--config", default="/etc/sceptre/opc_gateway.json")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if a.self_test:
        asyncio.run(_self_test())
    else:
        with open(a.config) as f:
            asyncio.run(serve(json.load(f)))


if __name__ == "__main__":
    sys.exit(main())
