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
import asyncio # Explicitly import asyncio for async operations
import re # Import the regex module
import os # For environment variables
import redis # For Redis caching
import json # For JSON serialization

# Set to INFO for less verbose logging in production
# For performance in production, consider setting this to WARNING or ERROR.
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

class KMXAPIClient:
    """
    A client for interacting with KMX devices via SNMP.
    Translates Java SNMP logic for fetching filtered alarm data.
    """
    # GSM Status Map from MIRANDA-MIB (assuming values are integers)
    # This maps integer OID values to human-readable severity strings.
    GSM_STATUS_MAP = {
        -1: "disabled",
        10000: "normal",
        20000: "minor/warning",
        25000: "major",
        30000: "critical/error",
        40000: "unknown"
    }

    # Redis configuration for disabled OIDs caching
    REDIS_HOST = os.environ.get('REDIS_HOST', '192.168.56.30')
    REDIS_PORT = int(os.environ.get('REDIS_PORT', 6379))
    DISABLED_OIDS_REDIS_KEY_PREFIX = "kmx:disabled_oids:" # Use device IP as suffix
    DISABLED_OIDS_TTL_SECONDS = 24 * 60 * 60 # 24 hours TTL

    def __init__(self):
        # Initializing SnmpDispatcher. This instance will be reused across operations.
        self.snmp_dispatcher = SnmpDispatcher()
        logging.info("KMXAPIClient initialized with SnmpDispatcher.")

        # Initialize Redis client for disabled OID caching
        try:
            self.redis_client = redis.Redis(host=self.REDIS_HOST, port=self.REDIS_PORT, db=0, decode_responses=True) # decode_responses=True for strings
            self.redis_client.ping() # Test connection
            logging.info(f"KMXAPIClient successfully connected to Redis at {self.REDIS_HOST}:{self.REDIS_PORT}.")
        except redis.exceptions.ConnectionError as e:
            logging.error(f"KMXAPIClient: Failed to connect to Redis for disabled OID caching at {self.REDIS_HOST}:{self.REDIS_PORT}. Error: {e}")
            self.redis_client = None # Set to None if connection fails
        except Exception as e:
            logging.error(f"KMXAPIClient: Unexpected error during Redis connection test: {e}", exc_info=True)
            self.redis_client = None


        # Pre-compile regex patterns for performance
        self.pgm_patterns_for_filtering = [
            re.compile(r"PGM-B", re.IGNORECASE),
            re.compile(r"PGM B", re.IGNORECASE),
            re.compile(r"PGM_B", re.IGNORECASE),
            re.compile(r"PGM.*BKP", re.IGNORECASE),
            re.compile(r"PGM.* BKP", re.IGNORECASE),
            re.compile(r"PGM.*B", re.IGNORECASE)
        ]
        self.suppress_pgm_patterns_in_actual_value = [
            re.compile(r"PGM.*B", re.IGNORECASE),
            re.compile(r"PGM-B", re.IGNORECASE),
            re.compile(r"PGM.* BKP", re.IGNORECASE)
        ]
        logging.debug("DEBUG: KMXAPIClient regex patterns pre-compiled.")


    def _resolve_oid_name(self, oid):
        """
        In a real application, you would load MIBs here and resolve the OID to a human-readable name.
        For this example, we'll just return the OID itself as MIB loading is not implemented with PySNMP's high-level API.
        """
        return oid

    async def _get_snmp_value(self, ip, community, oid_tuple, original_oid_str_for_logging):
        """
        Helper method to perform an asynchronous SNMP GET operation for a single OID.
        Accepts OID as a tuple of integers for robustness.
        Returns raw value, interpreted value, and status.
        """
        result = {
            "value": "N/A",
            "interpreted": "N/A",
            "status": "Failed" # Default status
        }
        
        try:
            logging.debug(f"DEBUG: _get_snmp_value - Polling IP: {ip}, Attempting to GET OID (tuple): {oid_tuple}, Original string: '{original_oid_str_for_logging}'")
            
            # Perform SNMP GET using the snmp_dispatcher instance
            errorIndication, errorStatus, errorIndex, varBinds = await get_cmd(
                self.snmp_dispatcher,
                CommunityData(community, mpModel=1), # mpModel=1 for SNMPv2c, as in test_kmx.py
                await UdpTransportTarget.create((ip, 161)), # Asynchronous creation of transport target
                ObjectType(ObjectIdentity(oid_tuple)), # Use ObjectIdentity with tuple directly
                timeout=5, # Added timeout
                retries=2  # Added retries
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
                    result["status"] = "Error"
                logging.warning(f"SNMP GET Status Error for {ip} OID {original_oid_str_for_logging}: {error_message}")
            else:
                if varBinds:
                    snmp_oid_result, snmp_value = varBinds[0]
                    raw_value = str(snmp_value)
                    interpreted_value = raw_value

                    if isinstance(snmp_value, rfc1902.Null):
                        result["value"] = "No Such Object"
                        result["status"] = "Error"
                        logging.warning(f"SNMP GET for {ip} OID {original_oid_str_for_logging} returned Null value (No Such Object).")
                        return result

                    try:
                        int_value = int(raw_value)
                        interpreted_value = self.GSM_STATUS_MAP.get(int_value, raw_value)
                    except ValueError:
                        pass
                    
                    result["value"] = raw_value
                    result["interpreted"] = interpreted_value
                    result["status"] = "Success"
                    logging.debug(f"DEBUG: SNMP GET Success: {original_oid_str_for_logging} = {raw_value} (Interpreted: {interpreted_value})")
                else:
                    logging.warning(f"SNMP GET for {ip} OID {original_oid_str_for_logging} returned no varBinds (empty response).")
                    result["value"] = "No Data"
                    result["status"] = "No Response" # More specific status
        
        except Exception as e:
            result["value"] = f"Exception: {e}"
            result["status"] = "Error"
            logging.error(f"Unexpected error during SNMP GET for {ip} OID {original_oid_str_for_logging}: {e}", exc_info=True)
            
        return result

    async def filtered_snmp_data(self, ip, community, base_oid_str):
        """
        Performs a filtered SNMP walk based on severity OID and consolidates data,
        mirroring the Java SnmpWalkController's logic.
        This function is now asynchronous and optimized for concurrent SNMP GETs.
        """
        final_results = []
        child_ids = set()
        disabled_component_ids = set() # New set to store IDs of disabled components

        # Ensure base_oid is clean (remove leading dot if present) and convert to tuple of integers
        cleaned_base_oid_str = base_oid_str.lstrip('.')
        try:
            base_oid_tuple = tuple(int(x) for x in cleaned_base_oid_str.split('.'))
        except ValueError:
            logging.error(f"Invalid base_oid_str format: '{base_oid_str}'. Must be dot-separated integers.")
            return []
        
        logging.debug(f"DEBUG: filtered_snmp_data - Converted cleaned_base_oid_str to tuple: {base_oid_tuple}")

        # --- New Logic: Identify Disabled Components from .21.3.1.2.1.6 branch (with Redis caching) ---
        input_status_base_oid_str = "1.3.6.1.4.1.3872.21.3.1.2.1.6"
        disabled_oids_redis_key = f"{self.DISABLED_OIDS_REDIS_KEY_PREFIX}{ip}"

        if self.redis_client:
            try:
                cached_disabled_oids = self.redis_client.get(disabled_oids_redis_key)
                if cached_disabled_oids:
                    disabled_component_ids = set(json.loads(cached_disabled_oids))
                    logging.info(f"Loaded {len(disabled_component_ids)} disabled component IDs from Redis cache for {ip}.")
                else:
                    logging.info(f"No disabled component IDs found in Redis cache for {ip}. Performing SNMP walk.")
                    await self._perform_disabled_oids_snmp_walk(ip, community, input_status_base_oid_str, disabled_component_ids)
                    if disabled_component_ids:
                        self.redis_client.setex(disabled_oids_redis_key, self.DISABLED_OIDS_TTL_SECONDS, json.dumps(list(disabled_component_ids)))
                        logging.info(f"Cached {len(disabled_component_ids)} disabled component IDs to Redis for {ip} with TTL {self.DISABLED_OIDS_TTL_SECONDS}s.")
            except redis.exceptions.ConnectionError as e:
                logging.error(f"Redis connection error during disabled OID cache operation for {ip}: {e}. Falling back to SNMP walk.")
                await self._perform_disabled_oids_snmp_walk(ip, community, input_status_base_oid_str, disabled_component_ids)
            except Exception as e:
                logging.error(f"Error accessing Redis cache for disabled OIDs for {ip}: {e}", exc_info=True)
                await self._perform_disabled_oids_snmp_walk(ip, community, input_status_base_oid_str, disabled_component_ids)
        else:
            logging.warning(f"Redis client not initialized. Performing SNMP walk for disabled OIDs for {ip} without caching.")
            await self._perform_disabled_oids_snmp_walk(ip, community, input_status_base_oid_str, disabled_component_ids)

        logging.info(f"Found {len(disabled_component_ids)} disabled components for {ip}.")
        # --- End New Logic (with Redis caching) ---


        # --- Existing Alarm Processing Logic ---
        severity_branch_oid_tuple = base_oid_tuple + (3,) # .3 for alarm status/severity
        
        logging.debug(f"DEBUG: filtered_snmp_data - Constructed severity_branch_oid for walk as tuple: {severity_branch_oid_tuple}")
        severity_branch_oid = ObjectIdentity(severity_branch_oid_tuple)
        
        logging.info(f"Starting filtered SNMP walk for {ip} from base OID: {cleaned_base_oid_str}")
        logging.info(f"Walking severity branch OID (tuple form): {severity_branch_oid_tuple}")
        
        current_oid_to_walk = severity_branch_oid # Start the walk from this OID

        while True:
            # Await next_cmd to get the next variable binding in the walk
            errorIndication, errorStatus, errorIndex, varBinds = await next_cmd(
                self.snmp_dispatcher,
                CommunityData(community, mpModel=1), # mpModel=1 for SNMPv2c
                await UdpTransportTarget.create((ip, 161)), # Asynchronous creation
                ObjectType(current_oid_to_walk), # Pass the last successfully retrieved OID to continue the walk
                lexicographicalMode=True,
                timeout=5, # Added timeout
                retries=2  # Added retries
            )

            if errorIndication:
                logging.error(f"SNMP Walk Error for {ip} on severity branch: {errorIndication}")
                break # Exit loop on error
            if errorStatus:
                logging.warning(f"SNMP Walk Status Error for {ip} on severity branch: {errorStatus.prettyPrint()}")
                break # Exit loop on error

            if not varBinds: # No more variables in the walk, or no response
                logging.debug(f"DEBUG: No more varBinds returned for walk, ending.")
                break

            oid, value = varBinds[0]
            full_polled_oid_tuple = oid.getOid().asTuple()
            logging.debug(f"DEBUG: SNMP Walk found OID: {full_polled_oid_tuple} with value: {value}")
            
            # Check if the OID is still within the severity branch based on its tuple form
            if not full_polled_oid_tuple[:len(severity_branch_oid_tuple)] == severity_branch_oid_tuple:
                logging.debug(f"DEBUG: OID {oid.prettyPrint()} is outside severity branch ({severity_branch_oid_tuple}), stopping walk.")
                break # Stop if OID goes out of branch scope
            
            try:
                # Extract the child ID (the first component after the branch OID)
                child_id_parts = full_polled_oid_tuple[len(severity_branch_oid_tuple):]
                if child_id_parts:
                    child_id = str(child_id_parts[0]) # Get the first element as the child ID
                    if child_id.isdigit():
                        child_ids.add(child_id)
                        logging.debug(f"DEBUG: Added child_id: '{child_id}' from OID: {oid.prettyPrint()}")
                    else:
                        logging.warning(f"Warning: Extracted child_id '{child_id}' is not purely numeric for OID '{oid.prettyPrint()}'. Skipping.")
                else:
                    logging.warning(f"Warning: Could not extract child ID from OID {oid.prettyPrint()}. Skipping.")
            except Exception as e:
                logging.warning(f"Could not extract child ID from OID {oid.prettyPrint()}: {e}", exc_info=True)
            
            current_oid_to_walk = oid # Update last_oid to continue the walk from the next item

        logging.info(f"Found {len(child_ids)} unique child IDs for {ip}.")
        if not child_ids:
            logging.warning(f"No child IDs found during severity OID walk for {ip}. No alarms to process.")
            return final_results

        # --- OPTIMIZATION: Collect all SNMP GET tasks and run them concurrently ---
        snmp_get_tasks = []
        child_id_to_oids_map = {} # To map results back to child_id and OID type

        for child_id_str in sorted(list(child_ids), key=int):
            # Skip if this component's ID was found to be disabled (optimization to avoid unnecessary GETs)
            if child_id_str in disabled_component_ids:
                logging.info(f"Skipping alarm check for component ID '{child_id_str}' as it is disabled.")
                continue

            child_id_int = int(child_id_str)

            current_severity_oid_tuple = base_oid_tuple + (3, child_id_int)
            current_device_name_oid_tuple = base_oid_tuple + (2, child_id_int)
            current_actual_value_oid_tuple = base_oid_tuple + (5, child_id_int)

            original_severity_oid_str = f"{cleaned_base_oid_str}.3.{child_id_str}"
            original_device_name_oid_str = f"{cleaned_base_oid_str}.2.{child_id_str}"
            original_actual_value_oid_str = f"{cleaned_base_oid_str}.5.{child_id_str}"

            # Create tasks for concurrent execution
            snmp_get_tasks.append(
                self._get_snmp_value(ip, community, current_severity_oid_tuple, original_severity_oid_str)
            )
            snmp_get_tasks.append(
                self._get_snmp_value(ip, community, current_device_name_oid_tuple, original_device_name_oid_str)
            )
            snmp_get_tasks.append(
                self._get_snmp_value(ip, community, current_actual_value_oid_tuple, original_actual_value_oid_str)
            )

            # Map the current child_id_str to the indices of its corresponding tasks
            # This allows us to retrieve the correct results after asyncio.gather
            # The indices will be (len(snmp_get_tasks) - 3) for severity, -2 for device_name, -1 for actual_value
            child_id_to_oids_map[child_id_str] = {
                "severity_idx": len(snmp_get_tasks) - 3,
                "device_name_idx": len(snmp_get_tasks) - 2,
                "actual_value_idx": len(snmp_get_tasks) - 1,
                "original_severity_oid_str": original_severity_oid_str # Store for final result
            }

        logging.info(f"Collecting {len(snmp_get_tasks)} SNMP GET tasks for concurrent execution for {ip}.")
        # Run all collected SNMP GET tasks concurrently
        all_results = await asyncio.gather(*snmp_get_tasks, return_exceptions=True) # return_exceptions to handle individual task failures

        logging.info(f"Completed concurrent SNMP GETs for {ip}. Processing results.")

        # Process results and apply filtering/alarm logic (original logic remains)
        for child_id_str in sorted(list(child_id_to_oids_map.keys()), key=int):
            mapping = child_id_to_oids_map[child_id_str]
            
            # Retrieve results using stored indices
            severity_result = all_results[mapping["severity_idx"]]
            device_name_result = all_results[mapping["device_name_idx"]]
            actual_value_result = all_results[mapping["actual_value_idx"]]
            original_severity_oid_str = mapping["original_severity_oid_str"] # Get the original OID string

            # Check for exceptions from concurrent tasks
            if isinstance(severity_result, Exception):
                logging.error(f"Error fetching severity for child ID {child_id_str}: {severity_result}")
                severity_result = {"value": "N/A", "interpreted": "N/A", "status": "Error"}
            if isinstance(device_name_result, Exception):
                logging.error(f"Error fetching device name for child ID {child_id_str}: {device_name_result}")
                device_name_result = {"value": "N/A", "interpreted": "N/A", "status": "Error"}
            if isinstance(actual_value_result, Exception):
                logging.error(f"Error fetching actual value for child ID {child_id_str}: {actual_value_result}")
                actual_value_result = {"value": "N/A", "interpreted": "N/A", "status": "Error"}

            device_name = device_name_result.get("value")
            actual_value = actual_value_result.get("value", "")

            # --- ALARM FILTERING/SUPPRESSION LOGIC (PREVENTS ADDING TO final_results) ---
            is_device_name_pgm_for_filtering = False
            if device_name:
                for pattern in self.pgm_patterns_for_filtering:
                    if pattern.search(device_name):
                        is_device_name_pgm_for_filtering = True
                        break
            
            contains_source_star_for_filtering = "source " in actual_value

            if is_device_name_pgm_for_filtering and contains_source_star_for_filtering:
                logging.info(f"Filtered out alarm for PGM-related device '{device_name}' with actual value '{actual_value}' (contains 'source *') for IP {ip}, Child ID {child_id_str}.")
                continue # Skip processing this alarm and move to the next child_id
            # --- END ALARM FILTERING/SUPPRESSION LOGIC ---


            # --- ALARM CONDITION LOGIC (MODIFIED FURTHER, now applies only if not filtered above) ---
            is_pgm_related_in_device_name = False
            if device_name:
                for pattern in self.pgm_patterns_for_filtering:
                    if pattern.search(device_name):
                        is_pgm_related_in_device_name = True
                        break

            contains_source_star_in_actual_value = "source " in actual_value

            actual_value_contains_suppress_pgm_keywords = False
            if actual_value:
                for pattern in self.suppress_pgm_patterns_in_actual_value:
                    if pattern.search(actual_value):
                        actual_value_contains_suppress_pgm_keywords = True
                        break

            if (is_pgm_related_in_device_name and 
                not contains_source_star_in_actual_value and 
                not actual_value_contains_suppress_pgm_keywords):
                
                logging.info(f"Custom Alarm Triggered: PGM-related device '{device_name}' with actual value '{actual_value}' (no 'source *' AND no suppress PGM keywords) for IP {ip}, Child ID {child_id_str}.")
                
                custom_alarm_entry = {
                    "severity": "CRITICAL",
                    "deviceName": device_name_result.get("interpreted", "N/A"),
                    "actualValue": f"KMX PGM Alarm: {device_name} - {actual_value}",
                    "oid": original_severity_oid_str,
                    "status": "Success",
                    "message": f"KMX PGM Alarm: {device_name} - {actual_value}"
                }
                final_results.append(custom_alarm_entry)
                continue
            # --- END ALARM CONDITION LOGIC ---


            if severity_result.get("value") != "10000": # Only process if not "normal" severity
                special_alarm_message = None
                
                # --- UPDATED: "HCO" Device Name Special Handling ---
                if device_name is not None and "HCO" in device_name:
                    if actual_value == "source 23;source 23":
                        logging.info(f"Skipping alarm for {ip} child ID {child_id_str} due to 'HCO' in device name and actual value is 'source 23;source 23'.")
                        continue # Skip this alarm as requested
                    else:
                        special_alarm_message = "HCO is on input 2"
                        logging.info(f"Special HCO alarm message applied for {ip}: {special_alarm_message}")
                # --- END UPDATED "HCO" LOGIC ---

                entry = {}
                entry["severity"] = severity_result.get("interpreted", "N/A")
                entry["deviceName"] = device_name_result.get("interpreted", "N/A")
                
                final_actual_value = actual_value
                if "source 23;source 23" == final_actual_value:
                    final_actual_value = ""
                entry["actualValue"] = special_alarm_message if special_alarm_message else final_actual_value

                entry["oid"] = original_severity_oid_str
                
                status = "Success"
                # Check individual statuses from the fetched results
                if ("Error" == severity_result.get("status") or
                    "Error" == device_name_result.get("status") or
                    "Error" == actual_value_result.get("status")):
                    status = "Error"
                elif ("No Response" == severity_result.get("status") or
                      "No Response" == device_name_result.get("status") or
                      "No Response" == actual_value_result.get("status")):
                    status = "Partial Success"
                entry["status"] = status

                final_results.append(entry)
            else:
                logging.debug(f"Alarm for {ip} child ID {child_id_str} has 'normal' severity (10000), skipping.")
        
        logging.info(f"Completed filtered SNMP walk for {ip}. Found {len(final_results)} active alarms.")
        return final_results

    async def _perform_disabled_oids_snmp_walk(self, ip, community, input_status_base_oid_str, disabled_component_ids):
        """
        Helper method to perform the SNMP walk for disabled components.
        Populates the disabled_component_ids set.
        """
        try:
            input_status_base_oid_tuple = tuple(int(x) for x in input_status_base_oid_str.split('.'))
        except ValueError:
            logging.error(f"Invalid input_status_base_oid_str format: '{input_status_base_oid_str}'. Must be dot-separated integers.")
            return

        logging.info(f"Starting SNMP walk to identify disabled components for {ip} from base OID: {input_status_base_oid_str}")
        current_input_status_oid_to_walk = ObjectIdentity(input_status_base_oid_tuple)

        while True:
            errorIndication, errorStatus, errorIndex, varBinds = await next_cmd(
                self.snmp_dispatcher,
                CommunityData(community, mpModel=1),
                await UdpTransportTarget.create((ip, 161)),
                ObjectType(current_input_status_oid_to_walk),
                lexicographicalMode=True,
                timeout=5,
                retries=2
            )

            if errorIndication:
                logging.error(f"SNMP Walk Error for {ip} on input status branch: {errorIndication}")
                break
            if errorStatus:
                logging.warning(f"SNMP Walk Status Error for {ip} on input status branch: {errorStatus.prettyPrint()}")
                break

            if not varBinds:
                logging.debug(f"DEBUG: No more varBinds returned for input status walk, ending.")
                break

            oid, value = varBinds[0]
            full_polled_oid_tuple = oid.getOid().asTuple()

            # Check if OID is within the input status branch
            if not full_polled_oid_tuple[:len(input_status_base_oid_tuple)] == input_status_base_oid_tuple:
                logging.debug(f"DEBUG: OID {oid.prettyPrint()} is outside input status branch, stopping walk.")
                break
            
            try:
                child_id_parts = full_polled_oid_tuple[len(input_status_base_oid_tuple):]
                if child_id_parts:
                    current_child_id = str(child_id_parts[0])
                    current_input_status_specific_oid_tuple = input_status_base_oid_tuple + (int(current_child_id),)
                    original_input_status_oid_str = f"{input_status_base_oid_str}.{current_child_id}"

                    status_result = await self._get_snmp_value(ip, community, current_input_status_specific_oid_tuple, original_input_status_oid_str)
                    if status_result.get("interpreted") == "disabled":
                        disabled_component_ids.add(current_child_id)
                        logging.debug(f"DEBUG: Identified disabled component: Child ID '{current_child_id}' (OID: {original_input_status_oid_str})")
                    else:
                        logging.debug(f"DEBUG: Component {current_child_id} is not disabled (Status: {status_result.get('interpreted')})")
                else:
                    logging.warning(f"Warning: Could not extract child ID from OID {oid.prettyPrint()} for input status.")
            except Exception as e:
                logging.warning(f"Error processing input status OID {oid.prettyPrint()}: {e}", exc_info=True)
            
            current_input_status_oid_to_walk = oid


# Global instance of the client
kmx_api_client = KMXAPIClient()
