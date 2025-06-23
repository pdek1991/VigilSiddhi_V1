import logging
# Import SnmpEngine from its direct source, as it's not always re-exported by hlapi
from pysnmp.entity.engine import SnmpEngine
# Corrected imports as per the user's provided app.py, using v1arch.asyncio
from pysnmp.hlapi.v1arch.asyncio import (
    get_cmd, # Corrected from getCmd to get_cmd
    next_cmd, # Corrected from nextCmd to next_cmd
    CommunityData,
    UdpTransportTarget,
    ObjectType,
    ObjectIdentity,
    ContextData, # Re-added ContextData here, assuming it's available in this specific import context
)
from pysnmp.proto import rfc1902 # For Null object check, consistent with hlapi.v1arch

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

    def __init__(self):
        # SnmpEngine instance (can be reused for multiple operations)
        self.snmp_engine = SnmpEngine()

    def _resolve_oid_name(self, oid):
        """
        In a real application, you would load MIBs here and resolve the OID to a human-readable name.
        For this example, we'll just return the OID itself as MIB loading is not implemented with PySNMP's high-level API.
        """
        return oid

    def _get_snmp_value_sync(self, ip, community, oid_str):
        """
        Helper method to perform a synchronous SNMP GET operation for a single OID.
        Returns raw value, interpreted value, and status.
        """
        result = {
            "value": "N/A",
            "interpreted": "N/A",
            "status": "Failed" # Default status
        }
        
        try:
            # Perform SNMP GET
            # Note: get_cmd is typically for asyncio, but the next() call makes it synchronous.
            # We are using get_cmd from v1arch.asyncio which returns an iterator.
            errorIndication, errorStatus, errorIndex, varBinds = next(
                get_cmd(
                    self.snmp_engine,
                    CommunityData(community), # SNMPv2c
                    UdpTransportTarget((ip, 161)),
                    ContextData(), # ContextData is now imported from pysnmp.hlapi.v1arch.asyncio
                    ObjectType(ObjectIdentity(oid_str))
                )
            )

            if errorIndication:
                result["value"] = f"Error: {errorIndication}"
                result["status"] = "Error"
                logging.error(f"SNMP GET Error for {ip} OID {oid_str}: {errorIndication}")
            elif errorStatus:
                # SNMP error status (e.g., noSuchName)
                error_message = f"{errorStatus.prettyPrint()} at {varBinds[int(errorIndex)-1] if errorIndex and len(varBinds) >= int(errorIndex) else oid_str}"
                result["value"] = f"Error: {error_message}"
                result["status"] = "Error"
                if "noSuchName" in error_message:
                    result["value"] = "No Such Object"
                    result["status"] = "Error" # Consistent with Java's "No Such Object"
                logging.warning(f"SNMP GET Status Error for {ip} OID {oid_str}: {error_message}")
            else:
                for oid, value in varBinds:
                    raw_value = str(value)
                    interpreted_value = raw_value # Default to raw

                    # Check for Null type (equivalent to Java's instanceof Null)
                    if isinstance(value, rfc1902.Null):
                        result["value"] = "No Such Object"
                        result["status"] = "Error"
                        logging.warning(f"SNMP GET for {ip} OID {oid_str} returned Null value (No Such Object).")
                        return result

                    try:
                        # Attempt to interpret as integer and map if applicable
                        int_value = int(raw_value)
                        interpreted_value = self.GSM_STATUS_MAP.get(int_value, raw_value)
                    except ValueError:
                        # Not an integer, keep raw value as interpreted
                        pass
                    
                    result["value"] = raw_value
                    result["interpreted"] = interpreted_value
                    result["status"] = "Success"
        
        except Exception as e:
            result["value"] = f"Exception: {e}"
            result["status"] = "Error"
            logging.error(f"Unexpected error during SNMP GET for {ip} OID {oid_str}: {e}", exc_info=True)
            
        return result

    def filtered_snmp_data(self, ip, community, base_oid):
        """
        Performs a filtered SNMP walk based on severity OID and consolidates data,
        mirroring the Java SnmpWalkController's logic.

        Args:
            ip (str): The IP address of the KMX device.
            community (str): The SNMP community string.
            base_oid (str): The base OID for KMX alarms (e.g., .1.3.6.1.4.1.3872.21.6.2.1).

        Returns:
            list: A list of dictionaries, each representing a filtered alarm entry.
        """
        final_results = []
        child_ids = set()

        severity_branch_oid = ObjectIdentity(f"{base_oid}.3") # OID for severity, e.g., .1.3.6.1.4.1.3872.21.6.2.1.3
        
        logging.info(f"Starting filtered SNMP walk for {ip} from base OID: {base_oid}")
        logging.info(f"Walking severity branch OID: {severity_branch_oid}")

        # 1. First, walk the severity OID branch to find all child IDs (instance identifiers)
        # Using next_cmd for walk, iterate until OID is out of branch or no more results
        for (errorIndication,
             errorStatus,
             errorIndex,
             varBinds) in next_cmd(
                self.snmp_engine,
                CommunityData(community),
                UdpTransportTarget((ip, 161)),
                ContextData(), # ContextData is now imported from pysnmp.hlapi.v1arch.asyncio
                ObjectType(severity_branch_oid),
                lexicographicalMode=True # Ensure proper walk behavior
             ):

            if errorIndication:
                logging.error(f"SNMP Walk Error for {ip} on severity branch: {errorIndication}")
                break
            if errorStatus:
                logging.warning(f"SNMP Walk Status Error for {ip} on severity branch: {errorStatus.prettyPrint()}")
                break

            for oid, value in varBinds:
                # Break condition: OID is no longer under the branch
                if not str(oid).startswith(str(severity_branch_oid)):
                    break
                
                full_oid = str(oid)
                try:
                    # Extract the child ID (the last numerical segment after the last dot)
                    child_id = full_oid.split('.')[-1]
                    if child_id.isdigit(): # Ensure it's a valid numerical ID
                        child_ids.add(child_id)
                except Exception as e:
                    logging.warning(f"Could not extract child ID from OID {full_oid}: {e}")
            else: # continue loop if no break
                continue
            # Break outer loop if inner loop broke
            break 
        
        logging.info(f"Found {len(child_ids)} child IDs for {ip}.")
        if not child_ids:
            logging.warning(f"No child IDs found during severity OID walk for {ip}. No alarms to process.")
            return final_results

        # 2. Now, for each collected child ID, perform specific GETs and apply filtering
        for child_id in sorted(list(child_ids), key=int): # Sort for consistent output
            current_severity_oid = f"{base_oid}.3.{child_id}"
            current_device_name_oid = f"{base_oid}.2.{child_id}"
            current_actual_value_oid = f"{base_oid}.5.{child_id}"

            severity_result = self._get_snmp_value_sync(ip, community, current_severity_oid)
            
            # --- APPLY SEVERITY FILTERING ---
            # Only process if raw value of severity OID is NOT "10000" (which maps to "normal")
            if severity_result.get("value") != "10000":
                device_name_result = self._get_snmp_value_sync(ip, community, current_device_name_oid)
                device_name = device_name_result.get("interpreted")

                # --- APPLY DEVICE NAME FILTERING ---
                # Skip this entry if "HCO" is found in the device name
                if device_name is not None and "HCO" in device_name:
                    logging.debug(f"Skipping alarm for {ip} child ID {child_id} due to 'HCO' in device name: {device_name}")
                    continue # Skip to the next childId

                actual_value_result = self._get_snmp_value_sync(ip, community, current_actual_value_oid)

                entry = {}
                entry["severity"] = severity_result.get("interpreted", "N/A") # Interpreted value of .3 OID
                entry["deviceName"] = device_name # Use the deviceName after filtering check
                
                # --- APPLY ACTUAL VALUE FILTERING ---
                final_actual_value = actual_value_result.get("value", "")
                # If the actual value is "source 23;source 23", replace it with an empty string
                if "source 23;source 23" == final_actual_value:
                    final_actual_value = ""
                entry["actualValue"] = final_actual_value # Use the potentially modified value

                entry["oid"] = current_severity_oid # The OID primarily associated with this entry (severity OID)
                
                # Determine overall status for this combined entry
                status = "Success"
                if ("Error" == severity_result.get("status") or
                    "Error" == device_name_result.get("status") or
                    "Error" == actual_value_result.get("status")):
                    status = "Error" # If any individual GET failed
                elif ("No Response" == severity_result.get("status") or
                      "No Response" == device_name_result.get("status") or
                      "No Response" == actual_value_result.get("status")):
                    status = "Partial Success" # If any individual GET had no response
                entry["status"] = status

                final_results.append(entry)
            else:
                logging.debug(f"Alarm for {ip} child ID {child_id} has 'normal' severity (10000), skipping.")
        
        logging.info(f"Completed filtered SNMP walk for {ip}. Found {len(final_results)} active alarms.")
        return final_results

# Global instance of the client
kmx_api_client = KMXAPIClient()
