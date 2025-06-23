import asyncio
import logging
import sys
from prettytable import PrettyTable
from datetime import datetime

# Imports for PySNMP
try:
    from pysnmp.hlapi.v1arch.asyncio import (
        get_cmd,
        SnmpDispatcher,
        CommunityData,
        UdpTransportTarget, # This will now be used with .create()
        ObjectType,
        ObjectIdentity
    )
    from pysnmp.proto import rfc1902 # For Null object check
    SNMP_AVAILABLE = True
    logging.info("PySNMP successfully imported.")
except ImportError:
    logging.error("PySNMP not found. Please install it: pip install pysnmp")
    SNMP_AVAILABLE = False
except Exception as e:
    logging.error(f"Error importing PySNMP: {e}", exc_info=True)
    SNMP_AVAILABLE = False

# --- Configuration ---
TARGET_KMX_IP = '172.19.185.230'
SNMP_COMMUNITY_STRING = 'public' # Replace with your SNMP community string if different
POLLING_INTERVAL_SECONDS = 300 # How often to poll

# Define specific OIDs to poll for the KMX device.
# Corrected OIDs based on your snmpwalk output (using specific instances).
# Ensured OID strings are clean and do not contain IP addresses.
KMX_TEST_OIDS = {
    "sysDescr_instance1": "1.3.6.1.4.1.3872.21.6.2.1.2.1", # Example instance from your snmpwalk for sysDescr
    "sysUpTime_instance1": "1.3.6.1.4.1.3872.21.6.2.1.3.1", # Example instance from your snmpwalk for sysUpTime
    "sysContact_instance6": "1.3.6.1.4.1.3872.21.6.2.1.5.1", # Example instance "Video freeze"
    # Add more specific OID instances as needed for your KMX device.
}

# --- Asynchronous SNMP GET Function ---
async def async_fetch_single_snmp_value(ip, oid, community_string, oid_label):
    """
    Performs an asynchronous SNMP GET operation for a single OID.
    Returns a tuple: (oid_label, value) or (oid_label, None) on error.
    """
    if not SNMP_AVAILABLE:
        logging.warning(f"SNMP polling skipped for {oid_label}: PySNMP not available.")
        return oid_label, "PySNMP Not Available"

    dispatcher = SnmpDispatcher()
    
    logging.debug(f"Attempting SNMP GET for {oid_label} at IP: {ip}, OID: '{oid}'")
    try:
        # CORRECTED: Use await UdpTransportTarget.create()
        errorIndication, errorStatus, errorIndex, varBinds = await get_cmd(
            dispatcher,
            CommunityData(community_string, mpModel=1), # mpModel=1 for SNMPv2c
            await UdpTransportTarget.create((ip, 161)), # Use .create() here
            ObjectType(ObjectIdentity(oid)), # This is where the OID string is processed
            timeout=5, # Passed to get_cmd
            retries=2  # Passed to get_cmd
        )

        if errorIndication:
            logging.error(f"SNMP Engine Error for {oid_label} ({ip}, {oid}): {errorIndication}")
            return oid_label, f"Error: {errorIndication}"
        elif errorStatus:
            # errorStatus is an integer, errorIndex is the problematic varBind index (1-based)
            error_msg = errorStatus.prettyPrint()
            # Try to get the problematic OID if errorIndex is valid
            var_bind_detail = varBinds[int(errorIndex)-1][0].prettyPrint() if errorIndex and len(varBinds) >= int(errorIndex) else oid
            logging.error(f"SNMP Error for {oid_label} ({ip}, {oid}): {error_msg} at {var_bind_detail}")
            return oid_label, f"SNMP Error: {error_msg}"
        else:
            if varBinds:
                snmp_oid_result, snmp_value = varBinds[0]
                if isinstance(snmp_value, rfc1902.Null):
                    logging.warning(f"SNMP GET for {oid_label} ({ip}, {oid}) returned Null value (No Such Object/Instance).")
                    return oid_label, "Null/No Such Object"
                logging.info(f"SNMP GET Success for {oid_label} ({ip}, {oid}): {snmp_value}")
                return oid_label, str(snmp_value)
            else:
                logging.warning(f"SNMP GET for {oid_label} ({ip}, {oid}) returned no varBinds (empty response).")
                return oid_label, "No Data"
    except Exception as e:
        # This catches "ObjectIdentity object not properly initialized" and other unexpected errors
        logging.critical(f"Critical error during SNMP poll for {oid_label} ({ip}, {oid}): {type(e).__name__}: {e}", exc_info=True)
        return oid_label, f"Exception: {e}"
    finally:
        # Corrected deprecated method calls for closing dispatcher
        if hasattr(dispatcher, 'transport_dispatcher') and callable(dispatcher.transport_dispatcher.close_dispatcher):
            dispatcher.transport_dispatcher.close_dispatcher()
        else:
            # Fallback for older pysnmp versions if needed
            if hasattr(dispatcher, 'transportDispatcher') and callable(dispatcher.transportDispatcher.closeDispatcher):
                dispatcher.transportDispatcher.closeDispatcher()
            logging.debug("Could not call close_dispatcher or closeDispatcher on dispatcher.")


async def main():
    """Main function to perform SNMP polling and display results."""
    logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(levelname)s - %(message)s')
    logging.info(f"Starting standalone SNMP poll for IP: {TARGET_KMX_IP}")

    if not SNMP_AVAILABLE:
        logging.error("SNMP polling cannot proceed without PySNMP. Exiting.")
        return

    # Prepare SNMP requests
    if not KMX_TEST_OIDS:
        logging.warning("No OIDs defined for SNMP polling. Please configure KMX_TEST_OIDS.")
        return

    print(f"\n--- SNMP Polling Results for {TARGET_KMX_IP} ---")
    
    while True:
        results_table = PrettyTable()
        results_table.field_names = ["OID Label", "OID", "Value"]
        results_table.align["OID Label"] = "l"
        results_table.align["OID"] = "l"
        results_table.align["Value"] = "l"

        polled_data = {}
        try:
            logging.debug(f"Initiating asyncio.gather for {len(KMX_TEST_OIDS)} SNMP requests.")
            
            # Recreate tasks for each loop iteration as await asyncio.gather consumes them
            current_tasks = [
                async_fetch_single_snmp_value(TARGET_KMX_IP, oid_string, SNMP_COMMUNITY_STRING, oid_label)
                for oid_label, oid_string in KMX_TEST_OIDS.items()
            ]
            results = await asyncio.gather(*current_tasks)

            for oid_label, value in results:
                polled_data[oid_label] = value
                oid_string = KMX_TEST_OIDS.get(oid_label, "N/A") # Get the actual OID string
                results_table.add_row([oid_label, oid_string, value])
            
            print(f"\nLast Updated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
            print(results_table)

        except Exception as e:
            logging.critical(f"An unhandled error occurred in main polling loop: {e}", exc_info=True)
            print(f"\nError during polling: {e}")

        logging.info(f"Sleeping for {POLLING_INTERVAL_SECONDS} seconds before next poll...")
        await asyncio.sleep(POLLING_INTERVAL_SECONDS)

if __name__ == "__main__":
    # On Windows, need to set the event loop policy for asyncio
    if sys.platform.startswith('win'):
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("SNMP Polling script stopped by user (KeyboardInterrupt).")
    except Exception as e:
        logging.critical(f"Script terminated due to an unhandled error: {e}", exc_info=True)