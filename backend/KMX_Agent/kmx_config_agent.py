import sys
import os
import time
import json
import logging
import redis
from datetime import datetime
import asyncio # New: Import asyncio for async operations

# Adjust path to import custom modules. This ensures that mysql_client.py and kmx_api_client.py
# are found if they are in the same directory as this script.
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__))))

# Import custom modules
from mysql_client import MySQLManager
from kmx_api_client import kmx_api_client # Use the shared client instance

# Set to INFO for less verbose logging in production
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- Configuration ---
MYSQL_HOST = os.environ.get('MYSQL_HOST', '192.168.56.30')
MYSQL_DATABASE = os.environ.get('MYSQL_DATABASE', 'vigil_siddhi')
MYSQL_USER = os.environ.get('MYSQL_USER', 'vigilsiddhi')
MYSQL_PASSWORD = os.environ.get('MYSQL_PASSWORD', 'vigilsiddhi')

REDIS_HOST = os.environ.get('REDIS_HOST', '192.168.56.30')
REDIS_PORT = int(os.environ.get('REDIS_PORT', 6379))

# Using the Redis stream name specified by the user
REDIS_STREAM_NAME = "vs:agent:kmx_status"
POLLING_INTERVAL_SECONDS = 300 # Poll KMX configs every 5 minutes

# Initialize Redis and MySQL clients outside main to ensure they are set up once
# and any immediate connection errors are caught at startup.
r = None
mysql_manager = None

logging.debug("DEBUG: Attempting to initialize Redis connection.")
try:
    r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=0, decode_responses=False) # Keep responses as bytes
    r.ping() # Test connection immediately
    logging.info("Successfully connected to Redis.")
except redis.exceptions.ConnectionError as e:
    logging.critical(f"FATAL: Failed to connect to Redis at {REDIS_HOST}:{REDIS_PORT}. Please ensure Redis is running and accessible. Error: {e}")
    sys.exit(1) # Exit if Redis connection fails at startup
except Exception as e:
    logging.critical(f"FATAL: Unexpected error during Redis connection test: {e}", exc_info=True)
    sys.exit(1)

logging.debug("DEBUG: Attempting to initialize MySQLManager.")
try:
    mysql_manager = MySQLManager(
        host=MYSQL_HOST,
        database=MYSQL_DATABASE,
        user=MYSQL_USER,
        password=MYSQL_PASSWORD
    )
    # Check if the MySQL connection within MySQLManager is active
    if not mysql_manager.connection or not mysql_manager.connection.is_connected():
        logging.critical(f"FATAL: MySQLManager initialized but connection is not active. Check database credentials and server status.")
        sys.exit(1)
    logging.info("Successfully initialized MySQLManager and connected to database.")
except Exception as e:
    logging.critical(f"FATAL: Failed to initialize MySQLManager: {e}. Please check MySQL configuration and connectivity.")
    sys.exit(1) # Exit if MySQL initialization fails at startup


def publish_status_to_redis(payload):
    """Publishes a status message to the Redis Stream."""
    try:
        r.xadd(REDIS_STREAM_NAME, {"data": json.dumps(payload).encode('utf-8')})
        logging.info(f"Published to {REDIS_STREAM_NAME}: {payload.get('frontend_block_id', payload.get('device_ip'))} - {payload.get('status')}")
    except Exception as e:
        logging.error(f"Failed to publish to Redis stream {REDIS_STREAM_NAME}: {e}")

def get_overall_status_from_details(device_ip, active_alarms, api_errors):
    """
    Determines overall status and severity for KMX device based on fetched alarms.
    The 'message' field will only display the highest severity alarm message.
    """
    overall_status = "ok"
    overall_severity = "INFO"
    overall_message = f"KMX device {device_ip} status is OK." # Default message

    # Prioritize API errors for overall status/message
    if api_errors:
        overall_status = "error"
        overall_severity = "ERROR"
        overall_message = f"API communication errors encountered for {device_ip}: " + "; ".join(api_errors)
        return overall_status, overall_severity, overall_message

    # Process active alarms to determine primary status and message
    if active_alarms:
        highest_severity_alarm = None
        # Mapping from interpreted severity strings to a numerical rank for comparison
        severity_rank = {
            
            "INFO": 0, "normal": 0,
            "WARNING": 1, "minor/warning": 1,
            "MAJOR": 2,
            "CRITICAL": 3, "critical/error": 3,
            "disabled": 0, "unknown": 0 # Treat these as info/non-critical for overall status
        }

        for alarm in active_alarms:
            # The 'severity' field comes from KMXAPIClient's interpreted value (e.g., "critical/error")
            alarm_severity_str = alarm.get('severity', 'INFO').lower()
            current_rank = severity_rank.get(alarm_severity_str, 0) # Default to 0 (INFO)

            if highest_severity_alarm is None or current_rank > severity_rank.get(highest_severity_alarm['severity'].lower(), 0):
                highest_severity_alarm = alarm
        
        if highest_severity_alarm:
            # Map the interpreted severity string back to a generalized severity for the payload
            # This is to match the ES 'severity' field which expects broader categories.
            payload_severity = "INFO"
            alarm_severity_rank = severity_rank.get(highest_severity_alarm['severity'].lower(), 0)
            if alarm_severity_rank == 3: # CRITICAL
                payload_severity = "CRITICAL"
                overall_status = "alarm"
            elif alarm_severity_rank == 2: # MAJOR
                payload_severity = "MAJOR"
                overall_status = "alarm"
            elif alarm_severity_rank == 1: # WARNING
                payload_severity = "WARNING"
                overall_status = "warning"
            
            # The message should combine device name and actual value if available
            alarm_message_parts = []
            if highest_severity_alarm.get('deviceName'):
                alarm_message_parts.append(f"Device: {highest_severity_alarm['deviceName']}")
            if highest_severity_alarm.get('actualValue'):
                alarm_message_parts.append(f"Value: {highest_severity_alarm['actualValue']}")
            
            # Construct message.
            if alarm_message_parts:
                overall_message = f"{'; '.join(alarm_message_parts)}." 
            else:
                overall_message = f"Alarm detected for {device_ip}." # Simpler generic message

            overall_severity = payload_severity # Set the final overall severity

    return overall_status, overall_severity, overall_message


async def fetch_and_publish_kmx_configs():
    """
    Fetches KMX configuration details from MySQL, polls each KMX device in parallel (asynchronously),
    and publishes the consolidated status and active alarms to Redis.
    """
    logging.info("Starting KMX Config Agent cycle.")
    logging.debug("DEBUG: Attempting to fetch KMX configurations from MySQL.")
    
    # --- IMPORTANT FIX: Reconnect MySQL to fetch latest data ---
    # Only reconnect if the connection is actually closed or not established.
    if mysql_manager and (not mysql_manager.connection or not mysql_manager.connection.is_connected()):
        try:
            mysql_manager.reconnect()
            logging.info("Successfully reconnected to MySQL.")
        except Exception as e:
            logging.error(f"Failed to reconnect to MySQL: {e}. Cannot fetch KMX configurations.", exc_info=True)
            return
    elif not mysql_manager:
        logging.error("MySQLManager not initialized. Cannot fetch KMX configurations.")
        return
    
    # Ensure MySQLManager is initialized and connected after reconnect (or if it was already connected)
    if not mysql_manager.connection or not mysql_manager.connection.is_connected():
        logging.error("MySQLManager not connected after all attempts. Cannot fetch KMX configurations.")
        return

    kmx_configs = mysql_manager.get_kmx_configs() # This is a synchronous call to MySQLManager
    
    if kmx_configs is None:
        logging.error("Received None for KMX configurations from MySQLManager. Assuming database error or no data.")
        return
    
    if not kmx_configs:
        logging.warning("No KMX configurations found in MySQL database. Skipping polling cycle.")
        return

    logging.info(f"Found {len(kmx_configs)} KMX configurations to process.")
    logging.debug("DEBUG: Creating asyncio tasks for parallel KMX polling.")

    # Use asyncio.gather to run process_kmx_config for each device concurrently
    tasks = [process_kmx_config(config) for config in kmx_configs]
    await asyncio.gather(*tasks) # Wait for all concurrent polling tasks to complete
    logging.debug("DEBUG: All KMX polling tasks completed for this cycle.")

async def process_kmx_config(config):
    """
    Processes a single KMX configuration, fetches alarm data via SNMP (asynchronously),
    and publishes the consolidated status to Redis.
    This function is designed to be run concurrently as an asyncio task.
    """
    kmx_ip = config.get('kmx_ip')
    community = config.get('community')
    device_name_from_db = config.get('device_name', f"KMX Device {kmx_ip}")
    description = device_name_from_db 
    kmx_base_oid = config.get('base_oid', ".1.3.6.1.4.1.3872.21.6.2.1")

    if not kmx_ip or not community:
        logging.error(f"Skipping KMX config due to missing IP or community: {config}")
        return

    logging.info(f"Processing KMX config for IP: {kmx_ip}")
    logging.debug(f"DEBUG: KMX config details for {kmx_ip}: Community='{community}', Base OID='{kmx_base_oid}'")
    
    api_errors = []
    active_alarms_data = []
    
    try:
        logging.debug(f"DEBUG: Calling kmx_api_client.filtered_snmp_data for {kmx_ip}.")
        active_alarms_data = await kmx_api_client.filtered_snmp_data(kmx_ip, community, kmx_base_oid) # Await the async client call
        logging.debug(f"DEBUG: kmx_api_client.filtered_snmp_data for {kmx_ip} returned {len(active_alarms_data)} alarms.")
        
        if not active_alarms_data and not api_errors: # Check api_errors to differentiate no alarms vs client error
             logging.info(f"No active alarms found for {kmx_ip} after filtering, or SNMP query returned no relevant data.")

    except Exception as e:
        logging.error(f"Error fetching KMX SNMP data for {kmx_ip}: {e}", exc_info=True)
        api_errors.append(f"Unhandled error during SNMP data fetch: {e}")
            
    overall_status, overall_severity, overall_message = get_overall_status_from_details(kmx_ip, active_alarms_data, api_errors)

    redis_payload = {
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "agent_type": "kmx_config",
        "device_ip": kmx_ip,
        "device_name": description,
        "frontend_block_id": "G.KMX", # Updated to a fixed value as requested
        "group_name": device_name_from_db,
        "message": overall_message,
        "severity": overall_severity,
        "status": overall_status,
        "details": {
            "active_alarms": active_alarms_data,
            "api_errors": api_errors
        }
    }
    
    publish_status_to_redis(redis_payload)
    logging.debug(f"DEBUG: Finished processing KMX config for IP: {kmx_ip}")

async def main_agent(): # Main agent function is now async
    logging.info("KMX Config Agent starting main polling loop.")
    while True:
        logging.debug(f"DEBUG: Entering main agent polling cycle. Next cycle in {POLLING_INTERVAL_SECONDS} seconds.")
        await fetch_and_publish_kmx_configs() # Await the async function
        logging.info(f"KMX Config Agent sleeping for {POLLING_INTERVAL_SECONDS} seconds...")
        await asyncio.sleep(POLLING_INTERVAL_SECONDS) # Use asyncio.sleep

if __name__ == "__main__":
    if sys.platform.startswith('win'):
        # For Windows, use the appropriate event loop policy
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    try:
        logging.debug("DEBUG: asyncio.run(main_agent()) called.")
        asyncio.run(main_agent()) # Run the main async function
    except KeyboardInterrupt:
        logging.info("Consumer stopped by user (KeyboardInterrupt).")
        sys.exit(0)
    except Exception as e:
        logging.critical(f"KMX Config Agent terminated due to an unhandled error: {e}", exc_info=True)
        sys.exit(1)
