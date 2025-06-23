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
import asyncio # Explicitly import asyncio for async operations if needed outside hlapi

logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(levelname)s - %(message)s') # Set to DEBUG for more info

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

    def __init__(self):
        # Initializing SnmpDispatcher. This instance will be reused across operations.
        self.snmp_dispatcher = SnmpDispatcher()

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
        This function is now asynchronous.
        """
        final_results = []
        child_ids = set()

        # Ensure base_oid is clean (remove leading dot if present) and convert to tuple of integers
        cleaned_base_oid_str = base_oid_str.lstrip('.')
        try:
            base_oid_tuple = tuple(int(x) for x in cleaned_base_oid_str.split('.'))
        except ValueError:
            logging.error(f"Invalid base_oid_str format: '{base_oid_str}'. Must be dot-separated integers.")
            return []
        
        logging.debug(f"DEBUG: filtered_snmp_data - Converted cleaned_base_oid_str to tuple: {base_oid_tuple}")

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

        # 2. Now, for each collected child ID, perform specific GETs and apply filtering
        for child_id_str in sorted(list(child_ids), key=int): # Keep child_id as string for display
            child_id_int = int(child_id_str) # Convert to int for tuple concatenation

            # Construct OIDs as tuples for robust ObjectIdentity initialization
            current_severity_oid_tuple = base_oid_tuple + (3, child_id_int) # .3 for alarm status/severity
            current_device_name_oid_tuple = base_oid_tuple + (2, child_id_int) # .2 for alarm friendly name
            current_actual_value_oid_tuple = base_oid_tuple + (5, child_id_int) # .5 for alarm cause/message

            # For logging and 'oid' field in results, use the consistently cleaned base OID
            original_severity_oid_str = f"{cleaned_base_oid_str}.3.{child_id_str}"
            original_device_name_oid_str = f"{cleaned_base_oid_str}.2.{child_id_str}"
            original_actual_value_oid_str = f"{cleaned_base_oid_str}.5.{child_id_str}"


            logging.debug(f"DEBUG: Processing child ID {child_id_str} for IP: {ip}")
            logging.debug(f"DEBUG:   Calling _get_snmp_value for Severity OID (tuple): {current_severity_oid_tuple}, String: {original_severity_oid_str}")
            severity_result = await self._get_snmp_value(ip, community, current_severity_oid_tuple, original_severity_oid_str)
            
            if severity_result.get("value") != "10000": # Only process if not "normal" severity
                logging.debug(f"DEBUG:   Calling _get_snmp_value for Device Name OID (tuple): {current_device_name_oid_tuple}, String: {original_device_name_oid_str}")
                device_name_result = await self._get_snmp_value(ip, community, current_device_name_oid_tuple, original_device_name_oid_str)
                device_name = device_name_result.get("value") # Get raw value for "HCO" check

                # Check for "HCO" in device name (raw value) and specific actual value for special alarm
                special_alarm_message = None
                if device_name is not None and "HCO" in device_name:
                    logging.debug(f"Skipping alarm for {ip} child ID {child_id_str} due to 'HCO' in device name: {device_name}")
                    # Special check for "source 24;source 24" with "HCO"
                    actual_value_result = await self._get_snmp_value(ip, community, current_actual_value_oid_tuple, original_actual_value_oid_str)
                    if actual_value_result.get("value") == "source 24;source 24":
                        special_alarm_message = "HCO is on input 2"
                        logging.info(f"Special alarm detected for {ip}: {special_alarm_message}")
                    else:
                        continue # Skip if it's HCO but not the special "source 24" case

                logging.debug(f"DEBUG:   Calling _get_snmp_value for Actual Value OID (tuple): {current_actual_value_oid_tuple}, String: {original_actual_value_oid_str}")
                actual_value_result = await self._get_snmp_value(ip, community, current_actual_value_oid_tuple, original_actual_value_oid_str)

                entry = {}
                entry["severity"] = severity_result.get("interpreted", "N/A")
                entry["deviceName"] = device_name_result.get("interpreted", "N/A")
                
                final_actual_value = actual_value_result.get("value", "")
                if "source 23;source 23" == final_actual_value:
                    final_actual_value = ""
                # Use the special_alarm_message if set, otherwise original logic
                entry["actualValue"] = special_alarm_message if special_alarm_message else final_actual_value

                entry["oid"] = original_severity_oid_str # Store the original string OID for consistency in results
                
                status = "Success"
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

# Global instance of the client
kmx_api_client = KMXAPIClient()
