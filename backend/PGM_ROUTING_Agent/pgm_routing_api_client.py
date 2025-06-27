import logging
from pysnmp.hlapi.v1arch.asyncio import (
    get_cmd,
    CommunityData,
    UdpTransportTarget,
    ObjectType,
    ObjectIdentity,
    SnmpDispatcher,
)
from pysnmp.proto import rfc1902
import asyncio

logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(levelname)s - %(message)s')

class PGMRoutingAPIClient:
    """
    A client for interacting with PGM Routing devices via SNMP.
    Handles polling for PGM routing status and comparing with expected sources.
    """
    def __init__(self):
        self.snmp_dispatcher = SnmpDispatcher()
        logging.info("PGMRoutingAPIClient initialized with SnmpDispatcher.")

    async def _get_snmp_value(self, ip, community, oid_tuple, original_oid_str_for_logging):
        """
        Helper method to perform an asynchronous SNMP GET operation for a single OID.
        Accepts OID as a tuple of integers for robustness.
        Returns raw value, interpreted value, and status.
        """
        result = {
            "value": "N/A",
            "interpreted": "N/A",
            "status": "Failed",
            "message": "" # Initialize message for clearer error reporting
        }
        
        logging.debug(f"DEBUG: _get_snmp_value - Polling IP: {ip}, Attempting to GET OID (tuple): {oid_tuple}, Original string: '{original_oid_str_for_logging}'")
        
        try:
            errorIndication, errorStatus, errorIndex, varBinds = await get_cmd(
                self.snmp_dispatcher,
                CommunityData(community, mpModel=1),
                await UdpTransportTarget.create((ip, 161)),
                ObjectType(ObjectIdentity(oid_tuple)),
                timeout=5,
                retries=2
            )

            if errorIndication:
                result["value"] = f"Error: {errorIndication}"
                result["status"] = "Error"
                result["message"] = str(errorIndication)
                logging.error(f"SNMP GET Error for {ip} OID {original_oid_str_for_logging}: {errorIndication}")
            elif errorStatus:
                error_message = f"{errorStatus.prettyPrint()} at {varBinds[int(errorIndex)-1] if errorIndex and len(varBinds) >= int(errorIndex) else original_oid_str_for_logging}"
                result["value"] = f"Error: {error_message}"
                result["status"] = "Error"
                result["message"] = error_message
                if "noSuchName" in error_message:
                    result["value"] = "No Such Object"
                    result["status"] = "Error"
                    result["message"] = "No Such Object at OID"
                logging.warning(f"SNMP GET Status Error for {ip} OID {original_oid_str_for_logging}: {error_message}")
            else:
                if varBinds:
                    snmp_oid_result, snmp_value = varBinds[0]
                    # Ensure raw_value is stripped of whitespace
                    raw_value = str(snmp_value).strip() 
                    interpreted_value = raw_value

                    # Check for Null type
                    if isinstance(snmp_value, rfc1902.Null):
                        result["value"] = "No Such Object"
                        result["status"] = "Error"
                        result["message"] = "SNMP returned Null value (No Such Object)."
                        logging.warning(f"SNMP GET for {ip} OID {original_oid_str_for_logging} returned Null value (No Such Object).")
                        return result
                    
                    result["value"] = raw_value
                    result["interpreted"] = interpreted_value
                    result["status"] = "Success"
                    result["message"] = "SNMP GET successful."
                    logging.debug(f"DEBUG: SNMP GET Success: {original_oid_str_for_logging} = {raw_value}")
                else:
                    logging.warning(f"SNMP GET for {ip} OID {original_oid_str_for_logging} returned no varBinds (empty response).")
                    result["value"] = "No Data"
                    result["status"] = "No Response"
                    result["message"] = "SNMP returned no data."
        
        except Exception as e:
            result["value"] = f"Exception: {e}"
            result["status"] = "Error"
            result["message"] = f"Unexpected SNMP GET error: {e}"
            logging.error(f"Unexpected error during SNMP GET for {ip} OID {original_oid_str_for_logging}: {e}", exc_info=True)
            
        return result

    async def check_pgm_routing_status(self, ip, community, pgm_dest_id, expected_router_source, domain):
        """
        Checks the PGM routing status for a specific pgm_dest_id.
        Compares the SNMP polled value with the expected router_source.
        Includes domain information in messages.
        """
        pgm_routing_base_oid_str = "1.3.6.1.4.1.6419.1.1.100.2.101.101.4.4.4.1.1"
        
        # Convert base OID string to a tuple of integers
        try:
            base_oid_tuple = tuple(int(x) for x in pgm_routing_base_oid_str.split('.'))
        except ValueError:
            logging.error(f"Invalid pgm_routing_base_oid_str format: '{pgm_routing_base_oid_str}'. Must be dot-separated integers.")
            return {
                "pgm_dest": pgm_dest_id,
                "polled_source": "N/A",
                "expected_source": expected_router_source,
                "status": "Error",
                "message": f"[{domain.upper()} Domain] Invalid base OID format: {pgm_routing_base_oid_str}"
            }

        # Construct the full OID for the specific pgm_dest_id
        try:
            pgm_dest_oid_tuple = base_oid_tuple + (int(pgm_dest_id),)
        except ValueError:
            logging.error(f"Invalid pgm_dest_id format: '{pgm_dest_id}'. Must be an integer.")
            return {
                "pgm_dest": pgm_dest_id,
                "polled_source": "N/A",
                "expected_source": expected_router_source,
                "status": "Error",
                "message": f"[{domain.upper()} Domain] Invalid PGM destination ID: {pgm_dest_id}"
            }

        original_pgm_dest_oid_str = f"{pgm_routing_base_oid_str}.{pgm_dest_id}"
        
        logging.debug(f"DEBUG: Checking PGM routing for {domain} IP: {ip}, PGM Dest: {pgm_dest_id}, OID: {original_pgm_dest_oid_str}")

        snmp_result = await self._get_snmp_value(ip, community, pgm_dest_oid_tuple, original_pgm_dest_oid_str)
        polled_source = snmp_result.get("value")
        status = snmp_result.get("status")
        # Initialize message with a more informative default if SNMP fails
        message = snmp_result.get("message", snmp_result.get("value")) # Use the SNMP error message if available

        # Ensure both values are stripped of all non-printable characters and whitespace for robust comparison
        polled_source_clean = ''.join(filter(str.isprintable, str(polled_source))).strip()
        expected_router_source_clean = ''.join(filter(str.isprintable, str(expected_router_source))).strip()

        # Add detailed debug for the exact strings being compared
        logging.debug(f"DEBUG: PGM Dest {pgm_dest_id} - Comparison: Polled: '{repr(polled_source_clean)}', Expected: '{repr(expected_router_source_clean)}'")


        domain_prefix = f"[{domain.upper()} Domain] "

        if status == "Success":
            if polled_source_clean == expected_router_source_clean:
                status = "OK"
                message = f"{domain_prefix}PGM Destination {pgm_dest_id} routing is correct. Current: '{polled_source_clean}', Expected: '{expected_router_source_clean}'"
            else:
                status = "MISMATCH"
                # Renamed "Polled" to "Current" in the message
                message = f"{domain_prefix}PGM Routing MISMATCH for Destination {pgm_dest_id}. Current: '{polled_source_clean}', Expected: '{expected_router_source_clean}'"
        else: # SNMP call failed or no response
            status = "Error"
            # If the SNMP call itself failed, the message will already contain error info
            # We just add the domain prefix to it.
            message = f"{domain_prefix}SNMP Error for PGM Destination {pgm_dest_id}. Current: '{polled_source_clean}'. {snmp_result.get('message', '')}"


        return {
            "pgm_dest": pgm_dest_id,
            "polled_source": polled_source_clean,
            "expected_source": expected_router_source_clean,
            "status": status,
            "message": message,
            "oid": original_pgm_dest_oid_str,
            "domain": domain # Include domain in the return for consumer if needed
        }

    async def get_source_name_by_oid(self, ip, community, oid_suffix_value):
        """
        Fetches the source name (string) associated with a specific OID suffix value.
        The full OID is constructed using a predefined base and the suffix.
        Example: base_oid.suffix_value (e.g., .1.3.6.1.4.1.6419.1.1.100.2.101.102.1.1.4.1.217)
        """
        # Base OID for PGM Source Names as per the example provided by user
        source_name_base_oid_str = "1.3.6.1.4.1.6419.1.1.100.2.101.102.1.1.4.1"

        try:
            base_oid_tuple = tuple(int(x) for x in source_name_base_oid_str.split('.'))
            source_name_oid_tuple = base_oid_tuple + (int(oid_suffix_value),)
            original_source_name_oid_str = f"{source_name_base_oid_str}.{oid_suffix_value}"
        except ValueError as e:
            logging.error(f"Invalid OID format for source name lookup: suffix '{oid_suffix_value}'. Error: {e}")
            return {"value": "N/A", "status": "Error", "message": f"Invalid OID suffix: {oid_suffix_value}"}

        logging.debug(f"DEBUG: Getting source name for IP: {ip}, OID: {original_source_name_oid_str}")
        
        snmp_result = await self._get_snmp_value(ip, community, source_name_oid_tuple, original_source_name_oid_str)
        
        # Return the interpreted value or an error message if not successful
        if snmp_result["status"] == "Success":
            return {"value": snmp_result["interpreted"], "status": "Success", "message": "Source name fetched successfully."}
        else:
            return {"value": "N/A", "status": snmp_result["status"], "message": f"Failed to fetch source name: {snmp_result.get('message', 'Unknown SNMP error.')}"}


# Global instance of the client
pgm_routing_api_client = PGMRoutingAPIClient()

