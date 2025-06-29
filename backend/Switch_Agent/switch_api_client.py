# switch_api_client.py

import logging
from pysnmp.hlapi.v1arch.asyncio import (
    get_cmd,
    next_cmd,
    CommunityData,
    UdpTransportTarget,
    ObjectType,
    ObjectIdentity,
    SnmpDispatcher,
)
from pysnmp.proto import rfc1902 # For Null object check
import asyncio
import re
import os
import redis
import json
from collections import defaultdict

# Removed: logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
# The logging configuration is now expected to be handled by the main application (e.g., switch_config_agent.py).

class SwitchAPIClient:
    """
    A client for interacting with Cisco and Nexus switches via SNMP.
    Fetches hardware health, interface status, adapting OIDs based on the provided model.
    Strictly uses the OIDs provided by the user.
    """
    
    # --- User-specified OIDs ---
    # Interface MIB (RFC1213-MIB/IF-MIB) - generally universal
    IF_OPER_STATUS_OID = "1.3.6.1.2.1.2.2.1.8"  # Operational Status (ifOperStatus)
    IF_IN_OCTETS_OID = "1.3.6.1.2.1.2.2.1.10"   # Bytes received on interface (ifInOctets)
    IF_DESCR_OID = "1.3.6.1.2.1.2.2.1.2"     # Interface Description (ifDescr)
    IF_ADMIN_STATUS_OID = "1.3.6.1.2.1.2.2.1.7" # Admin Status (ifAdminStatus)
    IF_OUT_OCTETS_OID = "1.3.6.1.2.1.2.2.1.16"  # Bytes transmitted on interface (ifOutOctets)
    IF_ALIAS_OID = "1.3.6.1.2.1.31.1.1.1.18" # Interface Alias (ifAlias)

    # Cisco IOS-XE Specific OIDs
    CISCO_CPU_5MIN_OID = "1.3.6.1.4.1.9.2.1.58.0" # cpmCPUTotal5minRev - specific for Cisco CPU, scalar with .0
    CISCO_MEM_POOL_NAME_OID = "1.3.6.1.4.1.9.9.48.1.1.1.2" # ciscoMemoryPoolName
    CISCO_MEM_POOL_USED_OID = "1.3.6.1.4.1.9.9.48.1.1.1.5"  # ciscoMemoryPoolUsed
    CISCO_MEM_POOL_FREE_OID = "1.3.6.1.4.1.9.9.48.1.1.1.6"  # ciscoMemoryPoolFree

    # Redis configuration for disabled/admin_down interfaces caching
    REDIS_HOST = os.environ.get('REDIS_HOST', '192.168.56.30')
    REDIS_PORT = int(os.environ.get('REDIS_PORT', 6379))
    DISABLED_PORTS_REDIS_KEY_PREFIX = "switch:disabled_ports:" # Use device IP as suffix
    DISABLED_PORTS_TTL_SECONDS = 60 * 60 # 1 hour TTL for admin down ports

    def __init__(self):
        self.snmp_dispatcher = SnmpDispatcher()
        logging.info("SwitchAPIClient initialized with SnmpDispatcher.")

        try:
            self.redis_client = redis.Redis(host=self.REDIS_HOST, port=self.REDIS_PORT, db=0, decode_responses=True)
            self.redis_client.ping()
            logging.info(f"SwitchAPIClient successfully connected to Redis at {self.REDIS_HOST}:{self.REDIS_PORT}.")
        except redis.exceptions.ConnectionError as e:
            logging.error(f"SwitchAPIClient: Failed to connect to Redis for disabled port caching at {self.REDIS_HOST}:{self.REDIS_PORT}. Error: {e}")
            self.redis_client = None
        except Exception as e:
            logging.error(f"SwitchAPIClient: Unexpected error during Redis connection test: {e}", exc_info=True)
            self.redis_client = None

    async def _get_snmp_value(self, ip, community, oid_string, original_oid_str_for_logging, timeout_seconds=5, retries=2):
        """
        Helper method to perform an asynchronous SNMP GET operation for a single OID.
        Returns raw value, interpreted value, and status.
        Takes OID as a string. Includes timeout and retry parameters.
        """
        result = {
            "value": "N/A",
            "interpreted": "N/A",
            "status": "Failed"
        }
        try:
            # Ensure oid_string is a string and valid before creating ObjectIdentity
            if not isinstance(oid_string, str) or not oid_string:
                result["value"] = f"Invalid OID string: {oid_string}"
                result["status"] = "Error"
                logging.error(f"Invalid OID string provided for {ip}: {oid_string}")
                return result

            try:
                oid_object = ObjectIdentity(oid_string) 
            except Exception as e:
                result["value"] = f"OID Initialization Error: {e}"
                result["status"] = "ERROR_OID_INIT"
                logging.error(f"Failed to initialize ObjectIdentity for OID '{oid_string}' on {ip}: {e}", exc_info=True)
                return result

            logging.debug(f"DEBUG: _get_snmp_value - Polling IP: {ip}, GET OID: {original_oid_str_for_logging}")
            errorIndication, errorStatus, errorIndex, varBinds = await get_cmd(
                self.snmp_dispatcher,
                CommunityData(community, mpModel=1),
                await UdpTransportTarget.create((ip, 161)),
                ObjectType(oid_object), # Pass the correctly formed ObjectIdentity object
                timeout=timeout_seconds,
                retries=retries
            )

            if errorIndication:
                result["value"] = f"Error: {errorIndication}"
                result["status"] = "Error"
                logging.error(f"SNMP GET Error for {ip} OID {original_oid_str_for_logging}: {errorIndication}")
            elif errorStatus:
                error_message = f"{errorStatus.prettyPrint()} at {varBinds[int(errorIndex)-1] if errorIndex and len(varBinds) >= int(errorIndex) else original_oid_str_for_logging}"
                result["value"] = f"Error: {error_message}"
                result["status"] = "Error"
                if "noSuchName" in error_message:
                    result["value"] = "No Such Object"
                    result["status"] = "OID_NOT_FOUND" # Specific status for OID not found
                logging.warning(f"SNMP GET Status Error for {ip} OID {original_oid_str_for_logging}: {error_message}")
            else:
                if varBinds:
                    snmp_oid_result, snmp_value = varBinds[0]
                    if isinstance(snmp_value, rfc1902.Null):
                        result["value"] = "No Such Object"
                        result["status"] = "OID_NOT_FOUND" # Specific status for OID not found
                        logging.warning(f"SNMP GET for {ip} OID {original_oid_str_for_logging} returned Null value (No Such Object).")
                        return result

                    raw_value = str(snmp_value)
                    interpreted_value = raw_value
                    
                    # Custom interpretation for CPU OIDs
                    if original_oid_str_for_logging == self.CISCO_CPU_5MIN_OID:
                        try:
                            # Extract integer from strings like "INTEGER: 7" or "Gauge32: 10"
                            match = re.search(r'\d+', raw_value)
                            if match:
                                interpreted_value = int(match.group(0))
                            else:
                                interpreted_value = "Invalid format"
                                logging.warning(f"Could not parse CPU value '{raw_value}' for OID {original_oid_str_for_logging}")
                        except ValueError:
                            interpreted_value = "Invalid Value"
                            logging.warning(f"Could not convert CPU value '{raw_value}' to integer for OID {original_oid_str_for_logging}")
                        
                    # Basic interpretation for common integer statuses (e.g., 1=up, 2=down)
                    elif original_oid_str_for_logging.startswith(self.IF_ADMIN_STATUS_OID) or \
                         original_oid_str_for_logging.startswith(self.IF_OPER_STATUS_OID):
                        status_map = {1: "up", 2: "down", 3: "testing", 4: "unknown", 5: "dormant", 6: "notPresent", 7: "lowerLayerDown"}
                        try:
                            # Use regex to extract the integer, as raw_value might be "INTEGER: up(1)"
                            match = re.search(r'\d+', raw_value)
                            if match:
                                numeric_status = int(match.group(0))
                                interpreted_value = status_map.get(numeric_status, raw_value) # Fallback to raw if number not in map
                            else:
                                interpreted_value = raw_value # No numeric part found, use raw string
                        except ValueError: # If int conversion of extracted number fails (unlikely if regex matches)
                            interpreted_value = raw_value
                            logging.warning(f"Could not parse status value '{raw_value}' as integer for OID {original_oid_str_for_logging}. Using raw value.")
                    
                    result["value"] = raw_value
                    result["interpreted"] = interpreted_value
                    result["status"] = "Success"
                    logging.debug(f"DEBUG: SNMP GET Success: {original_oid_str_for_logging} = {raw_value} (Interpreted: {interpreted_value})")
                else:
                    logging.warning(f"SNMP GET for {ip} OID {original_oid_str_for_logging} returned no varBinds (empty response).")
                    result["value"] = "No Data"
                    result["status"] = "NO_RESPONSE" # Specific status for no response
        except Exception as e:
            result["value"] = f"Exception: {e}"
            result["status"] = "Error"
            logging.error(f"Unexpected error during SNMP GET for {ip} OID {original_oid_str_for_logging}: {e}", exc_info=True)
            
        return result

    async def _snmp_walk(self, ip, community, base_oid_str, timeout_seconds=5, retries=2):
        """
        Helper method to perform an asynchronous SNMP WALK operation.
        Returns a dictionary where keys are the full OID strings and values are their SNMP values.
        Includes timeout and retry parameters.
        """
        results = {}
        cleaned_base_oid_str = base_oid_str.lstrip('.')
        
        if not isinstance(cleaned_base_oid_str, str) or not cleaned_base_oid_str:
            logging.error(f"Invalid base_oid_str provided for SNMP walk on {ip}: '{base_oid_str}'.")
            return {}

        try:
            # Initialize base OID as ObjectIdentity directly
            base_oid_object = ObjectIdentity(cleaned_base_oid_str) 
        except Exception as e:
            logging.error(f"Error initializing base ObjectIdentity for walk '{cleaned_base_oid_str}' on {ip}: {e}", exc_info=True)
            return {}
        
        current_oid_to_walk = base_oid_object # Start walk from the base OID

        logging.debug(f"DEBUG: _snmp_walk - Starting walk for IP: {ip} from base OID: {cleaned_base_oid_str}")

        while True:
            errorIndication, errorStatus, errorIndex, varBinds = await next_cmd(
                self.snmp_dispatcher,
                CommunityData(community, mpModel=1),
                await UdpTransportTarget.create((ip, 161)),
                ObjectType(current_oid_to_walk), # Pass ObjectIdentity directly
                lexicographicalMode=True,
                timeout=timeout_seconds,
                retries=retries
            )

            if errorIndication:
                logging.error(f"SNMP Walk Error for {ip} on {cleaned_base_oid_str}: {errorIndication}")
                break
            if errorStatus:
                error_message = f"{errorStatus.prettyPrint()} at {varBinds[int(errorIndex)-1] if errorIndex and len(varBinds) >= int(errorIndex) else cleaned_base_oid_str}"
                logging.warning(f"SNMP Walk Status Error for {ip} on {cleaned_base_oid_str}: {error_message}")
                if "noSuchName" in error_message or "endOfMibView" in error_message:
                    logging.debug(f"SNMP walk for {cleaned_base_oid_str} reached end of MIB or no such name, terminating gracefully.")
                else:
                    logging.error(f"Unhandled SNMP walk status error: {error_message}")
                break

            if not varBinds:
                # Add a more specific log here if varBinds is unexpectedly empty
                logging.debug(f"DEBUG: No more varBinds returned for walk on {cleaned_base_oid_str}, or walk ended unexpectedly empty. Ending walk.")
                break

            oid, value = varBinds[0]
            # Use tuple slicing for robust OID comparison instead of oid.starts_with()
            if not tuple(oid)[:len(base_oid_object)] == tuple(base_oid_object):
                logging.debug(f"DEBUG: OID {oid.prettyPrint()} is outside base branch ({base_oid_object.prettyPrint()}), stopping walk.")
                break
            
            # Convert OID to string for dictionary key to ensure numeric format
            results[str(oid)] = str(value) 
            current_oid_to_walk = oid # Continue walk from the last retrieved OID
            
            logging.debug(f"DEBUG: Walked OID: {str(oid)} = {str(value)}") # Log the new string format
        
        logging.info(f"Completed SNMP walk for {ip} on {cleaned_base_oid_str}. Found {len(results)} entries.")
        return results

    async def _get_admin_down_ports(self, ip, community):
        """
        Performs an SNMP walk on ifAdminStatus and caches admin down ports.
        Changed logging level for cached ports to DEBUG.
        """
        admin_down_ports = set()
        redis_key = f"{self.DISABLED_PORTS_REDIS_KEY_PREFIX}{ip}"

        if self.redis_client:
            try:
                cached_ports = self.redis_client.get(redis_key)
                if cached_ports:
                    admin_down_ports = set(json.loads(cached_ports))
                    logging.debug(f"Loaded {len(admin_down_ports)} admin down ports from Redis cache for {ip}.") # Changed to debug
                    return admin_down_ports
            except redis.exceptions.ConnectionError as e:
                logging.error(f"Redis connection error during admin down port cache operation for {ip}: {e}. Falling back to SNMP walk.")
            except Exception as e:
                logging.error(f"Error accessing Redis cache for admin down ports for {ip}: {e}", exc_info=True)
        else:
            logging.warning(f"Redis client not initialized. Performing SNMP walk for admin down ports for {ip} without caching.")

        logging.info(f"Performing SNMP walk for ifAdminStatus on {ip} to identify admin down ports.")
        if_admin_status_data = await self._snmp_walk(ip, community, self.IF_ADMIN_STATUS_OID)
        if_descr_data = await self._snmp_walk(ip, community, self.IF_DESCR_OID) # To get interface names

        for oid_str, status_value in if_admin_status_data.items():
            try:
                # Extract the integer from the SNMP response string (e.g., "INTEGER: 1")
                match = re.search(r'\d+', str(status_value))
                if match:
                    numeric_status = int(match.group(0))
                    if numeric_status == 2: # Admin Status 'down'
                        # Extract the interface index from the OID (last component)
                        if_index = oid_str.split('.')[-1]
                        # Try to find the interface description for better logging
                        descr_oid_str = f"{self.IF_DESCR_OID}.{if_index}"
                        interface_name = if_descr_data.get(descr_oid_str, f"Unknown Interface (idx {if_index})")
                        admin_down_ports.add(if_index)
                        logging.debug(f"Identified admin down port: '{interface_name}' (Index: {if_index}, OID: {oid_str})")
                else:
                    logging.warning(f"Could not extract numeric admin status from '{status_value}' for OID {oid_str}. Skipping.")
            except Exception as e:
                logging.warning(f"Error processing ifAdminStatus OID {oid_str}: {e}", exc_info=True)
        
        if self.redis_client and admin_down_ports:
            try:
                self.redis_client.setex(redis_key, self.DISABLED_PORTS_TTL_SECONDS, json.dumps(list(admin_down_ports)))
                logging.info(f"Cached {len(admin_down_ports)} admin down port IDs to Redis for {ip} with TTL {self.DISABLED_PORTS_TTL_SECONDS}s.")
            except redis.exceptions.ConnectionError as e:
                logging.error(f"Redis connection error during admin down port caching for {ip}: {e}.")
            except Exception as e:
                logging.error(f"Error caching admin down ports to Redis for {ip}: {e}", exc_info=True)

        return admin_down_ports

    async def get_switch_details(self, ip, community, model, data_scope="all"):
        """
        Fetches comprehensive details for a Cisco/Nexus switch,
        adapting OIDs based on the provided model and requested data_scope.
        data_scope can be "critical", "sensors", or "all".
        """
        switch_details = {
            "ip": ip,
            "model": model,
            "system_info": { 
                "description": "N/A", 
                "hostname": "N/A" 
            },
            "hardware_health": {
                "cpu_utilization": "N/A",
                "memory_utilization": "N/A",
                "power_supply_status": [], 
                "temperature_sensors": [], 
                "fan_status": [] 
            },
            "uptime": "N/A", 
            "interfaces": [],
            "vlans": [], 
            "multicast_info": {}, 
            "inventory": {} 
        }
        api_errors = [] 

        model_str = str(model).lower() if model else ""
        logging.info(f"Fetching {data_scope} data for {ip} with detected model: '{model_str}'")
        
        admin_down_ports = await self._get_admin_down_ports(ip, community)

        # --- Model-Specific Data Retrieval based on data_scope ---
        tasks = []

        if data_scope in ["sensors", "all"]:
            if "cisco" == model_str:
                tasks.append(self._get_snmp_value(ip, community, self.CISCO_CPU_5MIN_OID, self.CISCO_CPU_5MIN_OID))
                tasks.append(self._snmp_walk(ip, community, self.CISCO_MEM_POOL_NAME_OID))
                tasks.append(self._snmp_walk(ip, community, self.CISCO_MEM_POOL_USED_OID))
                tasks.append(self._snmp_walk(ip, community, self.CISCO_MEM_POOL_FREE_OID))
            elif "nexus" == model_str:
                logging.info(f"Nexus CPU and Memory polling ignored as per user request for {ip}.")
            else:
                logging.warning(f"Unsupported/Unrecognized model: '{model_str}'. CPU/Memory data will be 'N/A'.")
                api_errors.append(f"No specific OID profile for model: '{model_str}'. CPU/Memory data might be incomplete.")

        if data_scope in ["critical", "all"]:
            # Gather all interface-related SNMP walks concurrently
            interface_snmp_tasks = [
                self._snmp_walk(ip, community, self.IF_DESCR_OID),
                self._snmp_walk(ip, community, self.IF_ALIAS_OID),
                self._snmp_walk(ip, community, self.IF_ADMIN_STATUS_OID),
                self._snmp_walk(ip, community, self.IF_OPER_STATUS_OID),
                self._snmp_walk(ip, community, self.IF_IN_OCTETS_OID),
                self._snmp_walk(ip, community, self.IF_OUT_OCTETS_OID)
            ]
            tasks.extend(interface_snmp_tasks)
        
        # Execute all gathered SNMP tasks concurrently
        results = await asyncio.gather(*tasks, return_exceptions=True)

        # Process results based on their origin (CPU, Memory, Interfaces)
        result_idx = 0

        # Process Sensor data (if requested)
        if data_scope in ["sensors", "all"]:
            if "cisco" == model_str:
                cpu_result = results[result_idx]
                mem_pool_names = results[result_idx + 1]
                mem_pool_used = results[result_idx + 2]
                mem_pool_free = results[result_idx + 3]
                result_idx += 4 # Advance index for next section

                # Process CPU
                if not isinstance(cpu_result, Exception):
                    if cpu_result["status"] == "Success" and isinstance(cpu_result["interpreted"], int):
                        switch_details["hardware_health"]["cpu_utilization"] = f"{cpu_result['interpreted']}%"
                    elif cpu_result["status"] == "Error":
                        api_errors.append(f"Failed to get Cisco IOS-XE CPU utilization: {cpu_result['value']}")
                    elif cpu_result["status"] == "OID_NOT_FOUND":
                        api_errors.append(f"Cisco CPU OID {self.CISCO_CPU_5MIN_OID} not found on {ip}.")
                else:
                    api_errors.append(f"Exception fetching Cisco CPU: {cpu_result}")

                # Process Memory
                total_memory_bytes = 0
                used_memory_bytes = 0
                if not isinstance(mem_pool_names, Exception) and mem_pool_names:
                    if not isinstance(mem_pool_used, Exception) and not isinstance(mem_pool_free, Exception):
                        for oid_name, pool_name in mem_pool_names.items():
                            pool_index = oid_name.split('.')[-1]
                            try:
                                used = int(mem_pool_used.get(f"{self.CISCO_MEM_POOL_USED_OID}.{pool_index}", 0))
                                free = int(mem_pool_free.get(f"{self.CISCO_MEM_POOL_FREE_OID}.{pool_index}", 0))
                                total_memory_bytes += (used + free)
                                used_memory_bytes += used
                            except ValueError as ve:
                                logging.warning(f"Invalid memory value for pool {pool_name} ({pool_index}): {ve}")
                                api_errors.append(f"Invalid memory value for pool {pool_name} on {ip}.")
                            except Exception as e:
                                logging.warning(f"Error processing Cisco memory pool {pool_name} ({pool_index}): {e}", exc_info=True)
                                api_errors.append(f"Error processing Cisco memory for pool {pool_name} on {ip}: {e}.")
                        
                        if total_memory_bytes > 0:
                            mem_percent = (used_memory_bytes / total_memory_bytes) * 100
                            switch_details["hardware_health"]["memory_utilization"] = f"{mem_percent:.2f}%"
                        else:
                            api_errors.append("No Cisco-specific memory pool data found or calculated total memory is zero.")
                    else:
                        if isinstance(mem_pool_used, Exception):
                            api_errors.append(f"Exception fetching Cisco memory used data: {mem_pool_used}")
                        if isinstance(mem_pool_free, Exception):
                            api_errors.append(f"Exception fetching Cisco memory free data: {mem_pool_free}")
                        api_errors.append("Cisco memory pool used/free data incomplete.")
                else:
                    if isinstance(mem_pool_names, Exception):
                        api_errors.append(f"Exception fetching Cisco memory pool names: {mem_pool_names}")
                    api_errors.append("No Cisco memory pool names found. Memory utilization will be N/A.")

        # Process Critical (Interface) data (if requested)
        if data_scope in ["critical", "all"]:
            # Extract results for interface data
            if_descrs = results[result_idx]
            if_aliases = results[result_idx + 1]
            if_admin_statuses = results[result_idx + 2]
            if_oper_statuses = results[result_idx + 3]
            if_in_octets = results[result_idx + 4]
            if_out_octets = results[result_idx + 5]

            # Check for exceptions from gathered tasks
            if any(isinstance(r, Exception) for r in [if_descrs, if_aliases, if_admin_statuses, if_oper_statuses, if_in_octets, if_out_octets]):
                if isinstance(if_descrs, Exception): api_errors.append(f"Exception during ifDescr walk: {if_descrs}")
                if isinstance(if_aliases, Exception): api_errors.append(f"Exception during ifAlias walk: {if_aliases}")
                if isinstance(if_admin_statuses, Exception): api_errors.append(f"Exception during ifAdminStatus walk: {if_admin_statuses}")
                if isinstance(if_oper_statuses, Exception): api_errors.append(f"Exception during ifOperStatus walk: {if_oper_statuses}")
                if isinstance(if_in_octets, Exception): api_errors.append(f"Exception during ifInOctets walk: {if_in_octets}")
                if isinstance(if_out_octets, Exception): api_errors.append(f"Exception during ifOutOctets walk: {if_out_octets}")
                # If any key walk failed, treat the entire interface section as potentially incomplete.
                if_descrs = {} # Prevent further processing with partial data
                logging.warning(f"Partial or failed SNMP walks for interfaces on {ip}. Interface data may be incomplete.")


            if not if_descrs:
                api_errors.append("Failed to get interface descriptions (no interface data).")

            for oid_descr_str, if_name in if_descrs.items():
                if_index = oid_descr_str.split('.')[-1]

                # Retrieve admin status
                admin_status_raw = if_admin_statuses.get(f"{self.IF_ADMIN_STATUS_OID}.{if_index}", "N/A")
                admin_status_map = {1: "up", 2: "down", 3: "testing"}
                match = re.search(r'\d+', str(admin_status_raw))
                admin_status = admin_status_raw # Default to raw
                if match:
                    numeric_status = int(match.group(0))
                    admin_status = admin_status_map.get(numeric_status, admin_status_raw)

                if admin_status == "down":
                    logging.debug(f"Skipping interface {if_name} (index {if_index}) as it's administratively down.")
                    continue

                # Operational Status Parsing
                oper_status_oid = f"{self.IF_OPER_STATUS_OID}.{if_index}"
                oper_status_raw = if_oper_statuses.get(oper_status_oid)
                
                oper_status_map = {1: "up", 2: "down", 3: "testing", 4: "unknown", 5: "dormant", 6: "notPresent", 7: "lowerLayerDown"}
                
                oper_status = "N/A" # Default to N/A
                logging.debug(f"DEBUG: Processing oper_status for {if_name} ({if_index}). Raw value from walk: {oper_status_raw}")
                if oper_status_raw is not None:
                    try:
                        match = re.search(r'\d+', str(oper_status_raw))
                        if match:
                            numeric_status = int(match.group(0))
                            parsed_oper_status = oper_status_map.get(numeric_status)
                            if parsed_oper_status is not None:
                                oper_status = parsed_oper_status
                                logging.debug(f"DEBUG: Parsed oper_status for {if_name} ({if_index}): Numeric={numeric_status}, Interpreted='{oper_status}'")
                            else:
                                oper_status = str(oper_status_raw) 
                                logging.warning(f"Interface {if_name} (index {if_index}) oper_status numeric value {numeric_status} not in map. Using raw: {oper_status_raw}.")
                        else:
                            oper_status = str(oper_status_raw)
                            logging.warning(f"No numeric status found in oper_status_raw '{oper_status_raw}' for interface {if_name} ({if_index}). Setting to raw string '{oper_status}'.")

                    except Exception as e:
                        logging.warning(f"Error interpreting oper_status for interface {if_name} ({if_index}): {e}. Raw: {oper_status_raw}. Setting to N/A.", exc_info=True)
                        oper_status = "N/A"
                else:
                    logging.warning(f"Operational status OID ({oper_status_oid}) for interface {if_name} ({if_index}) returned no value (None). Setting to N/A.")
                    oper_status = "N/A"

                is_operational_down = (oper_status == "down" or oper_status == "lowerLayerDown")

                in_octets = int(if_in_octets.get(f"{self.IF_IN_OCTETS_OID}.{if_index}", 0))
                out_octets = int(if_out_octets.get(f"{self.IF_OUT_OCTETS_OID}.{if_index}", 0))
                
                if_alias = if_aliases.get(f"{self.IF_ALIAS_OID}.{if_index}", "")

                switch_details["interfaces"].append({
                    "index": if_index,
                    "name": if_name,
                    "alias": if_alias, 
                    "admin_status": admin_status,
                    "oper_status": oper_status,
                    "is_operational_down": is_operational_down, 
                    "in_octets": in_octets,
                    "out_octets": out_octets,
                    "in_multicast_pkts": 0, 
                    "out_multicast_pkts": 0, 
                    "in_bitrate_mbps": "N/A",
                    "out_bitrate_mbps": "N/A"
                })
        
        return switch_details, api_errors

# Global instance of the client
switch_api_client = SwitchAPIClient()
