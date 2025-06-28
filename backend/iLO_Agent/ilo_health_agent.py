import sys
import os
import time
import json
import logging
import redis
from datetime import datetime
import asyncio
import aiohttp # Ensure aiohttp is imported for the API client session management

# Adjust path to import custom modules
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__))))

# Import custom modules
from mysql_client import MySQLManager
from ilo_api_client import ilo_api_client # Use the shared client instance

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- Configuration ---
MYSQL_HOST = os.environ.get('MYSQL_HOST', '192.168.56.30')
MYSQL_DATABASE = os.environ.get('MYSQL_DATABASE', 'vigil_siddhi')
MYSQL_USER = os.environ.get('MYSQL_USER', 'vigilsiddhi')
MYSQL_PASSWORD = os.environ.get('MYSQL_PASSWORD', 'vigilsiddhi')

REDIS_HOST = os.environ.get('REDIS_HOST', '192.168.56.30')
REDIS_PORT = int(os.environ.get('REDIS_PORT', 6379))

# Redis Stream Names based on device_name mapping AND Group_name
REDIS_STREAM_MAP = {
    "VSM": "vs:agent:iloM_status",
    "VSP": "vs:agent:iloP_status",
    "VSB": "vs:agent:iloB_status",
    "Compression M": "vs:agent:enc_iloM_status", # New stream for Compression M
    "Compression B": "vs:agent:enc_iloB_status", # New stream for Compression B
    "DEFAULT": "vs:agent:ilo_status" # For devices not explicitly mapped by device_name or group_name
}
POLLING_INTERVAL_SECONDS = 30 # Poll iLO configs every 30 seconds

# Initialize Redis and MySQL clients outside main
r = None
mysql_manager = None

async def initialize_clients():
    """Initializes Redis, MySQL, and ILO API clients."""
    global r, mysql_manager

    logging.info("Attempting to initialize Redis connection.")
    try:
        r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=0, decode_responses=False)
        r.ping()
        logging.info("Successfully connected to Redis.")
    except redis.exceptions.ConnectionError as e:
        logging.critical(f"FATAL: Failed to connect to Redis at {REDIS_HOST}:{REDIS_PORT}. Error: {e}")
        sys.exit(1)
    except Exception as e:
        logging.critical(f"FATAL: Unexpected error during Redis connection test: {e}", exc_info=True)
        sys.exit(1)

    logging.info("Attempting to initialize MySQLManager.")
    try:
        # Initialize MySQLManager with current credentials
        mysql_manager = MySQLManager(
            host=MYSQL_HOST,
            database=MYSQL_DATABASE,
            user=MYSQL_USER,
            password=MYSQL_PASSWORD
        )
        if not mysql_manager.connection or not mysql_manager.connection.is_connected():
            logging.critical(f"FATAL: MySQLManager initialized but connection is not active.")
            sys.exit(1)
        logging.info("Successfully initialized MySQLManager and connected to database.")
    except Exception as e:
        logging.critical(f"FATAL: Failed to initialize MySQLManager: {e}.", exc_info=True)
        sys.exit(1)
    
    # Initialize the aiohttp session for ilo_api_client
    await ilo_api_client._get_session() # This will create the session if it doesn't exist


def publish_status_to_redis(stream_name, payload):
    """Publishes a status message to the specified Redis Stream."""
    try:
        r.xadd(stream_name, {"data": json.dumps(payload).encode('utf-8')})
        logging.info(f"Published to {stream_name}: {payload.get('frontend_block_id', payload.get('device_ip'))} - {payload.get('status')}")
    except Exception as e:
        logging.error(f"Failed to publish to Redis stream {stream_name}: {e}")

async def fetch_and_publish_ilo_configs():
    """
    Fetches iLO device details from MySQL, groups them by IP,
    polls each unique iLO device in parallel, and then publishes
    consolidated status for each associated frontend_block_id to Redis.
    """
    logging.info("Starting iLO Health Agent cycle.")
    
    # Force MySQL connection to re-establish to pick up latest credentials/data
    if mysql_manager:
        mysql_manager.reconnect()
    else:
        logging.error("MySQLManager not initialized. Cannot reconnect.")
        return

    # Ensure MySQLManager is initialized and connected after reconnect
    if not mysql_manager.connection or not mysql_manager.connection.is_connected():
        logging.error("MySQLManager not connected after reconnect attempt. Cannot fetch device IPs.")
        return

    device_ips_configs = mysql_manager.get_all_device_ips()
    
    if device_ips_configs is None:
        logging.error("Received None for device IPs from MySQLManager. Assuming database error or no data.")
        return
    
    if not device_ips_configs:
        logging.warning("No iLO device configurations found in MySQL database (device_ips table). Skipping polling cycle.")
        return

    logging.info(f"Found {len(device_ips_configs)} iLO device configurations in total from DB.")
    # The next log line is very verbose, keeping it for debugging as per previous context
    logging.info(f"Found {device_ips_configs} iLO device configurations in total from DB.")


    # Group configurations by IP address
    # This dictionary will store:
    # {
    #   "ip_address": {
    #       "username": "...",
    #       "password": "...",
    #       "frontend_blocks": [
    #           {"frontend_block_id": "C.102", "device_name": "VSP", "group_name": "GroupA"},
    #           {"frontend_block_id": "C.202", "device_name": "VSP", "group_name": "GroupA"}
    #       ]
    #   }
    # }
    grouped_ilo_configs = {}
    for config in device_ips_configs:
        ip_address = config.get('ip_address')
        username = config.get('username')
        password = config.get('password')
        frontend_block_id = config.get('frontend_block_id')
        device_name = config.get('device_name')
        group_name = config.get('Group_name') # Retrieve the new Group_name from DB

        if not ip_address or not username or not password or not frontend_block_id or not device_name:
            logging.warning(f"Skipping malformed iLO config entry from DB: {config}")
            continue

        if ip_address not in grouped_ilo_configs:
            grouped_ilo_configs[ip_address] = {
                "username": username,
                "password": password,
                "frontend_blocks": []
            }
        grouped_ilo_configs[ip_address]["frontend_blocks"].append({
            "frontend_block_id": frontend_block_id,
            "device_name": device_name,
            "group_name": group_name # Store Group_name with the block info
        })
    
    logging.info(f"Grouped into {len(grouped_ilo_configs)} unique iLO devices for polling.")

    # Create tasks for processing each unique iLO device concurrently
    tasks = [
        process_unique_ilo_device(
            ip,
            details["username"],
            details["password"],
            details["frontend_blocks"]
        ) for ip, details in grouped_ilo_configs.items()
    ]
    await asyncio.gather(*tasks) # Wait for all concurrent polling tasks to complete
    logging.info("All iLO polling tasks completed for this cycle.")

async def process_unique_ilo_device(ip_address, username, password, frontend_blocks_info):
    """
    Processes a unique iLO device: fetches health data once and then
    publishes status for each associated frontend_block_id.
    """
    logging.info(f"Polling iLO device at IP: {ip_address}")
    
    ilo_health_data = await ilo_api_client.get_ilo_health_data(ip_address, username, password)

    overall_status = ilo_health_data.get("overall_status", "UNKNOWN")
    overall_severity = ilo_health_data.get("overall_severity", "UNKNOWN")
    overall_message = ilo_health_data.get("overall_message", "No specific status message.")
    api_errors = ilo_health_data.get("api_errors", [])
    # IML entries were removed from ilo_api_client, so this should not be populated
    # However, keeping the key in the payload structure for consistency if needed later.
    iml_alarms = ilo_health_data.get("iml_alarms", []) 
    component_health = ilo_health_data.get("components", {})


    # Now, for each frontend_block_id associated with this unique IP,
    # create and publish a separate Redis payload.
    for block_info in frontend_blocks_info:
        current_frontend_block_id = block_info["frontend_block_id"]
        current_device_name = block_info["device_name"]
        current_group_name = block_info["group_name"] # Retrieve the Group_name for this block

        logging.info(f"Publishing status for IP: {ip_address}, Frontend Block ID: {current_frontend_block_id}, Device Name: {current_device_name}, Group Name: {current_group_name}")

        # Determine Redis stream name based on current_group_name first, then current_device_name
        stream_name = REDIS_STREAM_MAP.get(current_group_name, REDIS_STREAM_MAP.get(current_device_name, REDIS_STREAM_MAP["DEFAULT"]))

        redis_payload = {
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "agent_type": "ilo_health",
            "device_ip": ip_address,
            "device_name": current_device_name,
            "frontend_block_id": current_frontend_block_id,
            "group_name": current_group_name, # Use the specific Group_name for this block
            "message": overall_message,
            "severity": overall_severity,
            "status": overall_status,
            "details": {
                "component_health": component_health,
                "iml_alarms": iml_alarms,
                "api_errors": api_errors
            }
        }
        publish_status_to_redis(stream_name, redis_payload)
        logging.debug(f"Finished processing and published payload for {current_frontend_block_id}.")

async def main_agent():
    """Main agent function for the iLO Health Monitor."""
    await initialize_clients() # Initialize all clients at startup
    logging.info("iLO Health Agent starting main polling loop.")
    while True:
        logging.info(f"Entering main agent polling cycle. Next cycle in {POLLING_INTERVAL_SECONDS} seconds.")
        await fetch_and_publish_ilo_configs()
        logging.info(f"iLO Health Agent sleeping for {POLLING_INTERVAL_SECONDS} seconds...")
        await asyncio.sleep(POLLING_INTERVAL_SECONDS)

if __name__ == "__main__":
    if sys.platform.startswith('win'):
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    try:
        asyncio.run(main_agent())
    except KeyboardInterrupt:
        logging.info("iLO Health Agent stopped by user (KeyboardInterrupt).")
        # Ensure the aiohttp session is closed on shutdown
        asyncio.run(ilo_api_client.close_session())
        if mysql_manager:
            mysql_manager.close()
        sys.exit(0)
    except Exception as e:
        logging.critical(f"iLO Health Agent terminated due to an unhandled error: {e}", exc_info=True)
        asyncio.run(ilo_api_client.close_session())
        if mysql_manager:
            mysql_manager.close()
        sys.exit(1)
