import sys
import os
import time
import json
import logging
import redis
from datetime import datetime
import asyncio
import re

# Adjust path to import custom modules
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__))))

from mysql_client import MySQLManager
from pgm_routing_api_client import pgm_routing_api_client # Import the new client

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- Configuration ---
MYSQL_HOST = os.environ.get('MYSQL_HOST', '192.168.56.30')
MYSQL_DATABASE = os.environ.get('MYSQL_DATABASE', 'vigil_siddhi')
MYSQL_USER = os.environ.get('MYSQL_USER', 'vigilsiddhi')
MYSQL_PASSWORD = os.environ.get('MYSQL_PASSWORD', 'vigilsiddhi')

REDIS_HOST = os.environ.get('REDIS_HOST', '192.168.56.30')
REDIS_PORT = int(os.environ.get('REDIS_PORT', 6379))

REDIS_STREAM_NAME = "vs:agent:pgm_routing_status" # New Redis stream name
POLLING_INTERVAL_SECONDS = 300 # Poll PGM routing configs every 5 minutes

# IPs for main and backup domains
MAIN_DOMAIN_IP = "172.19.185.81"
BACKUP_DOMAIN_IP = "172.19.220.51"
SNMP_COMMUNITY_STRING = os.environ.get('SNMP_COMMUNITY_STRING', 'public') # Assuming a common community string

# Initialize Redis and MySQL clients
r = None
mysql_manager = None

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

try:
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


def publish_status_to_redis(payload):
    """Publishes a status message to the Redis Stream."""
    try:
        r.xadd(REDIS_STREAM_NAME, {"data": json.dumps(payload).encode('utf-8')})
        logging.info(f"Published to {REDIS_STREAM_NAME}: {payload.get('frontend_block_id', payload.get('device_ip'))} - {payload.get('status')}")
    except Exception as e:
        logging.error(f"Failed to publish to Redis stream {REDIS_STREAM_NAME}: {e}")


async def fetch_and_publish_pgm_routing_configs():
    """
    Fetches PGM routing configuration details from MySQL, polls each route individually,
    and publishes the status of each route to Redis.
    """
    logging.info("Starting PGM Routing Config Agent cycle.")
    pgm_routing_configs = mysql_manager.get_pgm_routing_configs()
    
    if pgm_routing_configs is None:
        logging.error("Received None for PGM routing configurations from MySQLManager. Assuming database error or no data.")
        return
    
    if not pgm_routing_configs:
        logging.warning("No PGM routing configurations found in MySQL database. Skipping polling cycle.")
        return

    logging.info(f"Found {len(pgm_routing_configs)} PGM routing configurations to process.")

    processing_tasks = []
    for config in pgm_routing_configs:
        processing_tasks.append(process_single_pgm_routing_config(config))

    if processing_tasks:
        await asyncio.gather(*processing_tasks)
    else:
        logging.warning("No PGM routing configurations to process after initial filtering.")

async def process_single_pgm_routing_config(config):
    """
    Processes a single PGM routing configuration, fetches its status via SNMP,
    and publishes the result to Redis.
    """
    pgm_dest = str(config.get('pgm_dest'))
    router_source = config.get('router_source')
    frontend_block_id = config.get('frontend_block_id')
    channel_name = config.get('channel_name')
    domain = config.get('domain')

    if not pgm_dest or router_source is None or not frontend_block_id or not channel_name or not domain:
        logging.warning(f"Skipping PGM routing config due to missing required data: {config}")
        return

    # Normalize domain value for IP selection
    normalized_domain = str(domain).strip().lower()
    ip_to_poll = None
    if normalized_domain == 'main':
        ip_to_poll = MAIN_DOMAIN_IP
    elif normalized_domain == 'backup':
        ip_to_poll = BACKUP_DOMAIN_IP
    else:
        logging.warning(f"Unknown or invalid domain '{domain}' for pgm_dest {pgm_dest}. Skipping polling.")
        # Publish an error status for this entry
        redis_payload_error = {
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "agent_type": "pgm_routing",
            "device_ip": "N/A", # IP is unknown if domain is bad
            "device_name": channel_name,
            "frontend_block_id": frontend_block_id,
            "group_name": channel_name,
            "message": f"Invalid domain '{domain}' specified for PGM Destination {pgm_dest}.",
            "severity": "ERROR",
            "status": "error",
            "details": {
                "pgm_dest": pgm_dest,
                "expected_source": router_source,
                "domain": domain,
                "api_errors": [f"Invalid domain '{domain}' for polling."]
            }
        }
        publish_status_to_redis(redis_payload_error)
        return

    logging.info(f"Processing PGM routing for Dest: {pgm_dest}, Domain: {domain} (IP: {ip_to_poll})")
    
    routing_check_result = None
    api_errors = []

    try:
        routing_check_result = await pgm_routing_api_client.check_pgm_routing_status(
            ip_to_poll, SNMP_COMMUNITY_STRING, pgm_dest, router_source, domain
        )

        # If a mismatch is detected, attempt to fetch the source_name for the polled value
        if routing_check_result and routing_check_result.get("status") == "MISMATCH":
            polled_source_value = routing_check_result.get("polled_source")
            if polled_source_value and polled_source_value != "N/A":
                logging.debug(f"Mismatch detected. Attempting to fetch source name for polled value: {polled_source_value}")
                source_name_result = await pgm_routing_api_client.get_source_name_by_oid(
                    ip_to_poll, SNMP_COMMUNITY_STRING, polled_source_value
                )
                if source_name_result.get("status") == "Success":
                    routing_check_result["polled_source_name"] = source_name_result.get("value")
                    logging.info(f"Fetched source name '{source_name_result.get('value')}' for polled value '{polled_source_value}'.")
                else:
                    routing_check_result["polled_source_name_status"] = source_name_result.get("status")
                    routing_check_result["polled_source_name_message"] = source_name_result.get("message")
                    logging.warning(f"Could not fetch source name for polled value '{polled_source_value}': {source_name_result.get('message')}")

    except Exception as e:
        logging.error(f"Error fetching PGM routing SNMP data for {pgm_dest} (IP: {ip_to_poll}): {e}", exc_info=True)
        api_errors.append(f"Unhandled error during SNMP data fetch: {e}")

    # Construct Redis payload for this specific PGM destination
    payload_status = "ok"
    payload_severity = "INFO"
    payload_message = f"PGM Routing {pgm_dest} is OK."
    
    if routing_check_result:
        payload_status = routing_check_result.get("status", "error").lower() # e.g., "ok", "mismatch", "error"
        payload_message = routing_check_result.get("message", "Unknown status.")
        
        if payload_status == "mismatch":
            payload_severity = "CRITICAL"
        elif payload_status == "error":
            payload_severity = "ERROR"
        else:
            payload_severity = "INFO" # For "ok" status

    elif api_errors: # If routing_check_result is None but api_errors exist
        payload_status = "error"
        payload_severity = "ERROR"
        payload_message = f"API communication error for PGM Dest {pgm_dest} (IP: {ip_to_poll}): " + "; ".join(api_errors)
    else: # Should not happen if result is None and no API errors, but as fallback
        payload_status = "unknown"
        payload_severity = "WARNING"
        payload_message = f"Failed to get PGM routing status for {pgm_dest} (IP: {ip_to_poll})."


    redis_payload = {
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "agent_type": "pgm_routing",
        "device_ip": ip_to_poll,
        "device_name": channel_name, # Specific channel name for this pgm_dest
        "frontend_block_id": frontend_block_id, # Specific frontend_block_id for this pgm_dest
        "group_name": channel_name, # Specific channel name as group name
        "message": payload_message,
        "severity": payload_severity,
        "status": payload_status,
        "details": {
            "pgm_dest_check": routing_check_result if routing_check_result else {}, # Single check result, now potentially includes polled_source_name
            "api_errors": api_errors
        }
    }
    publish_status_to_redis(redis_payload)
    logging.debug(f"DEBUG: Finished processing single PGM routing config for Dest: {pgm_dest}, IP: {ip_to_poll}")


async def main_agent():
    logging.info("PGM Routing Config Agent starting main polling loop.")
    while True:
        await fetch_and_publish_pgm_routing_configs()
        logging.info(f"PGM Routing Config Agent sleeping for {POLLING_INTERVAL_SECONDS} seconds...")
        await asyncio.sleep(POLLING_INTERVAL_SECONDS)

if __name__ == "__main__":
    if sys.platform.startswith('win'):
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    try:
        asyncio.run(main_agent())
    except KeyboardInterrupt:
        logging.info("PGM Routing Config Agent stopped by user (KeyboardInterrupt).")
        sys.exit(0)
    except Exception as e:
        logging.critical(f"PGM Routing Config Agent terminated due to an unhandled error: {e}", exc_info=True)
        sys.exit(1)

