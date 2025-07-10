import logging
import time
import asyncio
from datetime import timedelta
import sys

from pysnmp.hlapi.v1arch.asyncio import (
    get_cmd, next_cmd, CommunityData, UdpTransportTarget, SnmpDispatcher
)
from pysnmp.proto import rfc1902
from pysnmp.proto.rfc1902 import ObjectIdentifier
from pysnmp.error import PySnmpError
from pysnmp.smi import builder, view

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')


class SwitchAPIClient:
    IF_DESCR_OID = "1.3.6.1.2.1.2.2.1.2"
    IF_ALIAS_OID = "1.3.6.1.2.1.31.1.1.1.18"
    IF_ADMIN_STATUS_OID = "1.3.6.1.2.1.2.2.1.7"
    IF_OPER_STATUS_OID = "1.3.6.1.2.1.2.2.1.8"
    IF_IN_OCTETS_OID = "1.3.6.1.2.1.2.2.1.10"
    IF_OUT_OCTETS_OID = "1.3.6.1.2.1.2.2.1.16"
    SYS_HOSTNAME_OID = "1.3.6.1.2.1.1.5.0"
    SYS_UPTIME_OID = "1.3.6.1.2.1.1.3.0"

    def __init__(self, timeout=5, retries=1):
        self.snmp_dispatcher = SnmpDispatcher()
        self.timeout = timeout
        self.retries = retries
        mib_builder = builder.MibBuilder()
        self.snmp_dispatcher.mibViewController = view.MibViewController(mib_builder)

    def _oid_to_tuple(self, oid_str):
        return tuple(int(x) for x in oid_str.split('.'))

    async def _snmp_get(self, ip, community, oid):
        oid_tuple = self._oid_to_tuple(oid)
        try:
            errorIndication, errorStatus, _, varBinds = await get_cmd(
                self.snmp_dispatcher,
                CommunityData(community, mpModel=1),
                await UdpTransportTarget.create((ip, 161), timeout=self.timeout, retries=self.retries),
                (ObjectIdentifier(oid_tuple), rfc1902.Null()),
                lookupMib=False
            )
            if errorIndication or errorStatus:
                return None
            val = varBinds[0][1]
            return int(val) if isinstance(val, (rfc1902.Integer, rfc1902.Gauge32, rfc1902.Counter32, rfc1902.TimeTicks)) else str(val)
        except Exception as e:
            logging.error(f"SNMP GET failed for {oid}: {e}")
            return None

    async def _snmp_walk(self, ip, community, oid):
        results = {}
        oid_tuple = self._oid_to_tuple(oid)
        base_oid = ObjectIdentifier(oid_tuple)
        varbind = (base_oid, rfc1902.Null())
        try:
            while True:
                errorIndication, errorStatus, _, varBinds = await next_cmd(
                    self.snmp_dispatcher,
                    CommunityData(community, mpModel=1),
                    await UdpTransportTarget.create((ip, 161), timeout=self.timeout, retries=self.retries),
                    varbind,
                    lexicographicMode=True,
                    lookupMib=False,
                    maxCalls=1
                )
                if errorIndication or errorStatus:
                    break
                next_oid, val = varBinds[0]
                if not next_oid.isPrefixOf(base_oid):
                    break
                results[str(next_oid)] = int(val) if isinstance(val, (rfc1902.Integer, rfc1902.Gauge32, rfc1902.Counter32)) else str(val)
                varbind = (next_oid, rfc1902.Null())
        except Exception as e:
            logging.error(f"SNMP WALK failed for {oid}: {e}")
        return results

    def calculate_bitrate(self, current, previous, seconds):
        if seconds <= 0 or current < previous:
            return 0.0
        return round((current - previous) * 8 / seconds / 1000, 2)

    def _parse_timeticks(self, ticks):
        try:
            return str(timedelta(seconds=int(ticks) / 100))
        except:
            return "N/A"

    async def get_switch_details(self, ip, community, model, last_poll_data=None):
        now = time.time()
        tasks = {
            "hostname": self._snmp_get(ip, community, self.SYS_HOSTNAME_OID),
            "uptime": self._snmp_get(ip, community, self.SYS_UPTIME_OID),
            "if_descr": self._snmp_walk(ip, community, self.IF_DESCR_OID),
            "if_alias": self._snmp_walk(ip, community, self.IF_ALIAS_OID),
            "if_admin_status": self._snmp_walk(ip, community, self.IF_ADMIN_STATUS_OID),
            "if_oper_status": self._snmp_walk(ip, community, self.IF_OPER_STATUS_OID),
            "if_in_octets": self._snmp_walk(ip, community, self.IF_IN_OCTETS_OID),
            "if_out_octets": self._snmp_walk(ip, community, self.IF_OUT_OCTETS_OID),
        }

        results = await asyncio.gather(*tasks.values())
        results_map = dict(zip(tasks.keys(), results))

        # Debug logs to detect which tasks failed
        for key, val in results_map.items():
            if val is None:
                logging.warning(f"SNMP GET failed for {key}")
            elif isinstance(val, dict) and not val:
                logging.warning(f"SNMP WALK returned no data for {key}")

        interfaces = []
        errors = []
        descr = results_map["if_descr"]
        admin_map = {1: "up", 2: "down"}
        oper_map = {1: "up", 2: "down", 7: "lowerLayerDown"}
        prev_map = {i['index']: i for i in last_poll_data.get("interfaces", [])} if last_poll_data else {}
        delta = now - last_poll_data.get("timestamp", 0) if last_poll_data else 0

        for oid, name in descr.items():
            idx = oid.split('.')[-1]
            admin = admin_map.get(results_map["if_admin_status"].get(f"{self.IF_ADMIN_STATUS_OID}.{idx}"), "unknown")
            oper = oper_map.get(results_map["if_oper_status"].get(f"{self.IF_OPER_STATUS_OID}.{idx}"), "unknown")
            in_bytes = results_map["if_in_octets"].get(f"{self.IF_IN_OCTETS_OID}.{idx}", 0)
            out_bytes = results_map["if_out_octets"].get(f"{self.IF_OUT_OCTETS_OID}.{idx}", 0)
            alias = results_map["if_alias"].get(f"{self.IF_ALIAS_OID}.{idx}", "")
            prev = prev_map.get(idx, {})
            in_kbps = self.calculate_bitrate(in_bytes, prev.get("in_octets", 0), delta)
            out_kbps = self.calculate_bitrate(out_bytes, prev.get("out_octets", 0), delta)
            interfaces.append({
                "index": idx, "name": name, "alias": alias,
                "admin_status": admin, "oper_status": oper,
                "in_octets": in_bytes, "out_octets": out_bytes,
                "in_kbps": in_kbps, "out_kbps": out_kbps
            })
            if admin == "up" and oper in ["down", "lowerLayerDown"]:
                errors.append(f"Interface {name} ({alias}) is DOWN (Admin UP).")

        return {
            "ip": ip,
            "model": model,
            "timestamp": now,
            "system_info": {
                "hostname": results_map.get("hostname", "N/A"),
                "uptime": self._parse_timeticks(results_map.get("uptime"))
            },
            "interfaces": interfaces,
            "hardware_health": {},
            "api_errors": errors
        }


async def main():
    ip = input("Enter switch IP: ")
    community = input("Enter SNMP community: ")
    client = SwitchAPIClient(timeout=5)
    switch_data = await client.get_switch_details(ip, community, "TestModel", last_poll_data={})
    import pprint
    pprint.pprint(switch_data)


if __name__ == "__main__":
    if sys.platform.startswith("win"):
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("Interrupted by user.")
