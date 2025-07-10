# switch_api_client.py

import logging
import time # Import time for bitrate calculation timestamp (primary import)
from pysnmp.hlapi.v1arch.asyncio import (
    get_cmd,
    next_cmd,
    CommunityData,
    UdpTransportTarget,
    SnmpDispatcher,
)
from pysnmp.proto import rfc1902 # For Null object check
from pysnmp.proto.rfc1902 import ObjectIdentifier # Import ObjectIdentifier directly
from pysnmp.error import PySnmpError # To specifically catch SNMP errors
from pysnmp.smi import builder, view # Import builder and view for MIB management
import asyncio
from datetime import timedelta


# The logging configuration is expected to be handled by the main application.

class SwitchAPIClient:
    """
    A client for interacting with switches via SNMP.
    Fetches hardware health, interface status, and system information.
    This version is updated to be resilient to SNMP timeouts and avoids MIB lookups.
    """
    
    # --- System MIBs ---
    SYS_UPTIME_OID = "1.3.6.1.2.1.1.3.0"
    SYS_HOSTNAME_OID = "1.3.6.1.2.1.1.5.0"

    # --- Interface MIBs (IF-MIB) ---
    IF_DESCR_OID = "1.3.6.1.2.1.2.2.1.2"
    IF_ADMIN_STATUS_OID = "1.3.6.1.2.1.2.2.1.7"
    IF_OPER_STATUS_OID = "1.3.6.1.2.1.2.2.1.8"
    IF_IN_OCTETS_OID = "1.3.6.1.2.1.2.2.1.10"
    IF_OUT_OCTETS_OID = "1.3.6.1.2.1.2.2.1.16"
    IF_ALIAS_OID = "1.3.6.1.2.1.31.1.1.1.18"

    # --- Cisco Specific MIBs ---
    CISCO_CPU_5MIN_OID = "1.3.6.1.4.1.9.2.1.58.0"
    CISCO_MEM_POOL_USED_OID = "1.3.6.1.4.1.9.9.48.1.1.1.5"
    CISCO_MEM_POOL_FREE_OID = "1.3.6.1.4.1.9.9.48.1.1.1.6"
    
    # --- Environment Monitoring MIBs (CISCO-ENVMON-MIB) ---
    CISCO_ENV_MON_FAN_STATUS_OID = "1.3.6.1.4.1.9.9.13.1.4.1.3"
    CISCO_ENV_MON_TEMP_VALUE_OID = "1.3.6.1.4.1.9.9.13.1.3.1.3"
    CISCO_ENV_MON_POWER_STATUS_OID = "1.3.6.1.4.1.9.9.13.1.5.1.3"
    
    # --- Entity MIB for sensor descriptions ---
    ENTITY_PHYSICAL_NAME_OID = "1.3.6.1.2.1.47.1.1.1.1.7"

    # OID for EndOfMibView, used for robust walk termination check
    END_OF_MIB_VIEW_OID = "1.3.6.1.6.3.1.1.5.0"


    def __init__(self, timeout=15, retries=2): # Increased default timeout to 15 seconds and retries to 2
        self.snmp_dispatcher = SnmpDispatcher()
        self.timeout = timeout
        self.retries = retries
        logging.info(f"SwitchAPIClient initialized with SnmpDispatcher (timeout={timeout}s, retries={retries}).")
        
        # Create an empty MibBuilder. By default, it has no MIB sources.
        mibBuilder = builder.MibBuilder()
        # Create a MibViewController using this empty MibBuilder.
        # Assign it to the dispatcher to ensure no MIB lookups are performed.
        self.snmp_dispatcher.mibViewController = view.MibViewController(mibBuilder)
        logging.info("Configured SnmpDispatcher's MIB view controller to prevent MIB lookups.")


    def _oid_to_tuple(self, oid_str):
        """Converts an OID string to a tuple of integers."""
        try:
            return tuple(int(x) for x in oid_str.split('.'))
        except ValueError:
            logging.error(f"Invalid OID string format: {oid_str}. Cannot convert to tuple.")
            return None

    async def _snmp_get(self, ip, community, oid):
        """
        Helper method to perform a single SNMP GET operation.
        It is resilient to timeouts and other SNMP errors, returning None on failure.
        """
        # Ensure OID is a clean string and convert to tuple
        clean_oid = str(oid).strip()
        if not clean_oid:
            logging.error(f"Invalid (empty or whitespace-only) OID provided for SNMP GET for {ip}.")
            return None
        
        oid_tuple = self._oid_to_tuple(clean_oid)
        if oid_tuple is None:
            return None

        try:
            errorIndication, errorStatus, errorIndex, varBinds = await get_cmd(
                self.snmp_dispatcher,
                CommunityData(community, mpModel=1),
                await UdpTransportTarget.create((ip, 161), timeout=self.timeout, retries=self.retries),
                # Directly create VarBind tuple with ObjectIdentifier and Null
                (ObjectIdentifier(oid_tuple), rfc1902.Null()),
                lookupMib=False # Explicitly disable MIB lookup
            )
            if errorIndication:
                # Log the specific error indication
                logging.warning(f"SNMP GET for {ip} on {clean_oid} failed with error indication: {errorIndication}")
                return None
            
            if errorStatus:
                # Log the specific error status
                logging.warning(f"SNMP GET for {ip} on {clean_oid} failed with error status: {errorStatus.prettyPrint()}")
                return None

            if varBinds and varBinds[0] and not isinstance(varBinds[0][1], rfc1902.Null):
                return varBinds[0][1]
            else:
                # Log if Null or no varBinds, indicating 'no such object' or similar
                if varBinds and varBinds[0] and isinstance(varBinds[0][1], rfc1902.Null):
                    logging.warning(f"SNMP GET for {ip} on {clean_oid} returned Null value (likely 'no such object' or 'no such instance').")
                else:
                    logging.warning(f"SNMP GET for {ip} on {clean_oid} returned no varBinds.")
                return None # Explicitly return None if Null or empty varBinds
        except PySnmpError as e:
            logging.error(f"PySNMP error during SNMP GET for {ip} on {clean_oid}: {e}", exc_info=True)
        except Exception as e:
            logging.error(f"Unexpected exception during SNMP GET for {ip} on {clean_oid}: {e}", exc_info=True)
        
        return None

    async def _snmp_walk(self, ip, community, oid):
        """
        Helper method to perform an SNMP WALK operation.
        It is resilient to timeouts and other SNMP errors, returning an empty dictionary on failure.
        """
        results = {}
        
        # Ensure OID is a clean string and convert to tuple
        clean_oid = str(oid).strip()
        if not clean_oid:
            logging.error(f"Invalid (empty or whitespace-only) OID provided for SNMP walk for {ip}.")
            return {}

        oid_tuple = self._oid_to_tuple(clean_oid)
        if oid_tuple is None:
            return {}

        try:
            # Use ObjectIdentifier directly for the current OID and base OID
            current_oid_varbind = (ObjectIdentifier(oid_tuple), rfc1902.Null())
            base_oid_obj = ObjectIdentifier(oid_tuple)
        except PySnmpError as e:
            logging.error(f"PySNMP error initializing ObjectIdentifier for OID '{clean_oid}' for {ip}: {e}", exc_info=True)
            return {}
        except Exception as e:
            logging.error(f"Unexpected exception initializing ObjectIdentifier for OID '{clean_oid}' for {ip}: {e}", exc_info=True)
            return {}

        try:
            while True:
                errorIndication, errorStatus, errorIndex, varBinds = await next_cmd(
                    self.snmp_dispatcher,
                    CommunityData(community, mpModel=1),
                    await UdpTransportTarget.create((ip, 161), timeout=self.timeout, retries=self.retries),
                    current_oid_varbind, # Pass the varbind tuple directly
                    lexicographicMode=True,
                    lookupMib=False # Explicitly disable MIB lookup
                )

                if errorIndication:
                    # Log the specific error indication
                    logging.warning(f"SNMP Walk for {ip} on {clean_oid} failed with error indication: {errorIndication}")
                    break
                
                if errorStatus:
                    # Log the specific error status
                    logging.warning(f"SNMP Walk for {ip} on {clean_oid} failed with error status: {errorStatus.prettyPrint()}")
                    break
                
                # varBinds is a sequence of sequences, get the first one
                varBind = varBinds[0]
                next_oid_obj, value = varBind

                # Check if we've walked past the branch we're interested in
                # Also check for EndOfMibView by its OID, which indicates the end of the walk
                # We compare the string representation of the OID to be robust against type issues.
                if not next_oid_obj.isPrefixOf(base_oid_obj) or str(next_oid_obj) == self.END_OF_MIB_VIEW_OID:
                    if str(next_oid_obj) == self.END_OF_MIB_VIEW_OID:
                        logging.info(f"SNMP Walk for {ip} on {clean_oid} terminated by EndOfMibView OID.")
                    else:
                        logging.info(f"SNMP Walk for {ip} on {clean_oid} completed (walked past base OID).")
                    break
                
                results[str(next_oid_obj)] = value
                
                # Set the OID for the next request - create a new varbind for the next iteration
                current_oid_varbind = (next_oid_obj, rfc1902.Null())

        except PySnmpError as e:
            logging.error(f"PySNMP error during SNMP walk for {ip} on {clean_oid}: {e}", exc_info=True)
            return {}
        except Exception as e:
            logging.error(f"Unexpected exception during SNMP walk for {ip} on {clean_oid}: {e}", exc_info=True)
            return {}

        return results

    def _parse_timeticks(self, timeticks):
        """Converts SNMP timeticks to a human-readable string."""
        if timeticks is None:
            return "N/A"
        try:
            total_seconds = int(timeticks) / 100
            return str(timedelta(seconds=total_seconds))
        except (ValueError, TypeError):
            return "N/A"

    def _parse_env_status(self, value, entity_type):
        """Parses environment monitor status codes."""
        status_map = {
            "fan": {1: "normal", 2: "warning", 3: "critical", 4: "shutdown", 5: "notPresent", 6: "notFunctioning"},
            "power": {1: "normal", 2: "warning", 3: "critical", 4: "shutdown", 5: "notPresent", 6: "notFunctioning"},
        }
        try:
            return status_map.get(entity_type, {}).get(int(value), "unknown")
        except (ValueError, TypeError):
            return "invalid"

    def calculate_bitrate(self, current_octets: int, prev_octets: int, time_delta_secs: float) -> float:
        """Calculates bitrate in kbps."""
        if time_delta_secs <= 0 or current_octets < prev_octets:
            return 0.0  # Avoid division by zero or counter reset issues
        
        delta_bytes = current_octets - prev_octets
        delta_bits = delta_bytes * 8
        bits_per_second = delta_bytes * 8 / time_delta_secs # Corrected calculation for bits per second
        kbps = bits_per_second / 1000
        return round(kbps, 2)

    async def get_switch_details(self, ip, community, model, last_poll_data=None):
        """
        Fetches comprehensive details for a switch, calculates metrics, and identifies alarms.
        last_poll_data is used for bitrate calculation and should contain 'timestamp' and 'interfaces'.
        """
        # Redundant import time within the method to ensure it's available
        import time 
        current_poll_time = time.time() # Capture current time at the start of the function

        switch_details = {
            "ip": ip,
            "model": model,
            "system_info": {"hostname": "N/A", "uptime": "N/A"},
            "hardware_health": {
                "cpu_utilization": "N/A",
                "memory_utilization": "N/A",
                "power_supplies": [],
                "temperature_sensors": [],
                "fans": []
            },
            "interfaces": [],
            "api_errors": [], # This list will capture alarm messages
            "timestamp": current_poll_time 
        }

        # --- Create and run all SNMP tasks concurrently ---
        tasks = {
            "hostname": self._snmp_get(ip, community, self.SYS_HOSTNAME_OID),
            "uptime": self._snmp_get(ip, community, self.SYS_UPTIME_OID),
            "if_descr": self._snmp_walk(ip, community, self.IF_DESCR_OID),
            "if_alias": self._snmp_walk(ip, community, self.IF_ALIAS_OID),
            "if_admin_status": self._snmp_walk(ip, community, self.IF_ADMIN_STATUS_OID),
            "if_oper_status": self._snmp_walk(ip, community, self.IF_OPER_STATUS_OID),
            "if_in_octets": self._snmp_walk(ip, community, self.IF_IN_OCTETS_OID),
            "if_out_octets": self._snmp_walk(ip, community, self.IF_OUT_OCTETS_OID),
            "cpu": self._snmp_get(ip, community, self.CISCO_CPU_5MIN_OID),
            "mem_used": self._snmp_walk(ip, community, self.CISCO_MEM_POOL_USED_OID),
            "mem_free": self._snmp_walk(ip, community, self.CISCO_MEM_POOL_FREE_OID),
            "fan_status": self._snmp_walk(ip, community, self.CISCO_ENV_MON_FAN_STATUS_OID),
            "temp_value": self._snmp_walk(ip, community, self.CISCO_ENV_MON_TEMP_VALUE_OID),
            "power_status": self._snmp_walk(ip, community, self.CISCO_ENV_MON_POWER_STATUS_OID),
            "entity_names": self._snmp_walk(ip, community, self.ENTITY_PHYSICAL_NAME_OID)
        }
        
        results = await asyncio.gather(*tasks.values())
        results_map = dict(zip(tasks.keys(), results))

        # --- Process Results ---
        # Note: API errors from _snmp_get/_snmp_walk are already logged internally.
        # This loop is more for identifying missing data from the results_map.
        for key, result in results_map.items():
            if result is None or (isinstance(result, dict) and not result):
                # Only add to api_errors if it's a critical piece of info that's missing
                if key in ["hostname", "uptime", "cpu", "mem_used", "mem_free", "if_descr"]:
                    switch_details["api_errors"].append(f"Critical data missing for {key} (likely SNMP timeout/error or no data).")


        # System Info
        hostname_val = results_map.get("hostname")
        switch_details["system_info"]["hostname"] = str(hostname_val) if hostname_val else "N/A"
        uptime_val = results_map.get("uptime")
        switch_details["system_info"]["uptime"] = self._parse_timeticks(uptime_val)

        # CPU and Memory
        cpu_val = results_map.get("cpu")
        if cpu_val is not None:
            try:
                switch_details["hardware_health"]["cpu_utilization"] = f"{int(cpu_val)}%"
            except (ValueError, TypeError):
                switch_details["api_errors"].append(f"Could not parse CPU utilization: {cpu_val}")
        
        mem_used_val = results_map.get("mem_used")
        mem_free_val = results_map.get("mem_free")
        if mem_used_val and mem_free_val:
            try:
                total_used = sum(int(v) for v in mem_used_val.values())
                total_free = sum(int(v) for v in mem_free_val.values())
                total_mem = total_used + total_free
                if total_mem > 0:
                    mem_percent = (total_used / total_mem) * 100
                    switch_details["hardware_health"]["memory_utilization"] = f"{mem_percent:.2f}%"
            except (ValueError, TypeError) as e:
                switch_details["api_errors"].append(f"Could not parse memory values: {e}")

        # Environmentals (Fans, Temp, Power)
        # Entity names are optional, provide a fallback
        entity_names = {oid.split('.')[-1]: str(name) for oid, name in (results_map.get("entity_names") or {}).items()}

        fan_status_val = results_map.get("fan_status")
        if fan_status_val:
            for oid, status in fan_status_val.items():
                index = oid.split('.')[-1]
                fan_entry = {
                    "name": entity_names.get(index, f"Fan {index}"),
                    "status": self._parse_env_status(status, "fan")
                }
                switch_details["hardware_health"]["fans"].append(fan_entry)
                # Alarm logic for fans
                if fan_entry["status"] not in ["normal", "notPresent", "unknown"]: # Consider 'unknown' as non-alarming for now
                    switch_details["api_errors"].append(f"Fan '{fan_entry['name']}' is in '{fan_entry['status']}' state.")


        power_status_val = results_map.get("power_status")
        if power_status_val:
            for oid, status in power_status_val.items():
                index = oid.split('.')[-1]
                psu_entry = {
                    "name": entity_names.get(index, f"Power Supply {index}"),
                    "status": self._parse_env_status(status, "power")
                }
                switch_details["hardware_health"]["power_supplies"].append(psu_entry)
                # Alarm logic for power supplies
                if psu_entry["status"] not in ["normal", "notPresent", "unknown"]: # Consider 'unknown' as non-alarming for now
                    switch_details["api_errors"].append(f"Power Supply '{psu_entry['name']}' is in '{psu_entry['status']}' state.")


        temp_value_val = results_map.get("temp_value")
        if temp_value_val:
            for oid, value in temp_value_val.items():
                index = oid.split('.')[-1]
                try:
                    temp_celsius = int(value)
                    temp_entry = {
                        "name": entity_names.get(index, f"Temp Sensor {index}"),
                        "temperature_celsius": temp_celsius
                    }
                    switch_details["hardware_health"]["temperature_sensors"].append(temp_entry)
                    # Alarm logic for temperature (e.g., threshold > 60 degrees Celsius)
                    if temp_celsius > 60:
                        switch_details["api_errors"].append(f"High Temperature on '{temp_entry['name']}': {temp_celsius}°C (Threshold: 60°C).")
                except (ValueError, TypeError):
                     switch_details["api_errors"].append(f"Could not parse temperature value for sensor index {index}: {value}")

        # Interfaces
        if_descr = results_map.get("if_descr")
        if if_descr:
            admin_status_map = {1: "up", 2: "down", 3: "testing"}
            oper_status_map = {1: "up", 2: "down", 3: "testing", 4: "unknown", 5: "dormant", 6: "notPresent", 7: "lowerLayerDown"}

            prev_interfaces_map = {iface['index']: iface for iface in last_poll_data.get("interfaces", [])} if last_poll_data and isinstance(last_poll_data.get("interfaces"), list) else {}
            time_delta = current_poll_time - last_poll_data.get("timestamp", 0) if last_poll_data else 0

            for oid, name in if_descr.items():
                if_index = oid.split('.')[-1]
                
                try:
                    admin_status_val = int(results_map.get("if_admin_status", {}).get(f"{self.IF_ADMIN_STATUS_OID}.{if_index}", 0))
                    oper_status_val = int(results_map.get("if_oper_status", {}).get(f"{self.IF_OPER_STATUS_OID}.{if_index}", 0))
                    in_octets = int(results_map.get("if_in_octets", {}).get(f"{self.IF_IN_OCTETS_OID}.{if_index}", 0))
                    out_octets = int(results_map.get("if_out_octets", {}).get(f"{self.IF_OUT_OCTETS_OID}.{if_index}", 0))

                    admin_status = admin_status_map.get(admin_status_val, "unknown")
                    oper_status = oper_status_map.get(oper_status_val, "unknown")

                    in_kbps = 0
                    out_kbps = 0
                    prev_iface = prev_interfaces_map.get(if_index)
                    if prev_iface:
                        in_kbps = self.calculate_bitrate(in_octets, prev_iface.get('in_octets', 0), time_delta)
                        out_kbps = self.calculate_bitrate(out_octets, prev_iface.get('out_octets', 0), time_delta)

                    interface_entry = {
                        "index": if_index,
                        "name": str(name),
                        "alias": str(results_map.get("if_alias", {}).get(f"{self.IF_ALIAS_OID}.{if_index}", "")),
                        "admin_status": admin_status,
                        "oper_status": oper_status,
                        "in_octets": in_octets,
                        "out_octets": out_octets,
                        "in_kbps": in_kbps,
                        "out_kbps": out_kbps,
                    }
                    switch_details["interfaces"].append(interface_entry)

                    # Alarm logic for interfaces: admin up but operational down
                    if admin_status == "up" and oper_status in ["down", "lowerLayerDown"]:
                        switch_details["api_errors"].append(f"Interface {interface_entry['name']} ({interface_entry['alias']}) is operationally DOWN (Admin: UP).")
                    # Any other operational down status
                    elif oper_status == "down":
                        switch_details["api_errors"].append(f"Interface {interface_entry['name']} ({interface_entry['alias']}) operational status is DOWN.")

                except (ValueError, TypeError, KeyError) as e:
                    switch_details["api_errors"].append(f"Could not process interface index {if_index}: {e}")

        return switch_details

# Global instance of the client
switch_api_client = SwitchAPIClient(timeout=15, retries=2)
