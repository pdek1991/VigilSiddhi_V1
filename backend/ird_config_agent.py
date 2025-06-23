import sys
import os
import time
import json
import logging
import redis
from datetime import datetime
import re # Added for IP address regex check
import concurrent.futures # Import for parallel execution

# Adjust path to import MySQLManager and IRDAPIClient
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__))))

# Import custom modules
from mysql_client import MySQLManager
from ird_api_client import ird_api_client # Use the shared client instance

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- Configuration ---
MYSQL_HOST = os.environ.get('MYSQL_HOST', '192.168.56.30')
MYSQL_DATABASE = os.environ.get('MYSQL_DATABASE', 'vigil_siddhi')
MYSQL_USER = os.environ.get('MYSQL_USER', 'vigilsiddhi')
MYSQL_PASSWORD = os.environ.get('MYSQL_PASSWORD', 'vigilsiddhi')

REDIS_HOST = os.environ.get('REDIS_HOST', '192.168.56.30')
REDIS_PORT = int(os.environ.get('REDIS_PORT', 6379))

REDIS_STREAM_NAME = "vs:agent:ird_config_status"
POLLING_INTERVAL_SECONDS = 300 # Poll IRD configs every 5 minutes
MAX_WORKERS = 10 # Number of parallel threads to use for fetching IRD data

# Initialize Redis and MySQL clients
r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=0, decode_responses=False) # Keep responses as bytes
mysql_manager = MySQLManager(
    host=MYSQL_HOST,
    database=MYSQL_DATABASE,
    user=MYSQL_USER,
    password=MYSQL_PASSWORD
)

def publish_status_to_redis(payload):
    """Publishes a status message to the Redis Stream."""
    try:
        r.xadd(REDIS_STREAM_NAME, {"data": json.dumps(payload).encode('utf-8')})
        logging.info(f"Published to {REDIS_STREAM_NAME}: {payload.get('frontend_block_id', payload.get('device_ip'))} - {payload.get('status')}")
    except Exception as e:
        logging.error(f"Failed to publish to Redis stream {REDIS_STREAM_NAME}: {e}")

def get_overall_status_from_details(details):
    """
    Determines overall status and severity for IRD config based primarily on faults.
    The 'message' field will only display the highest severity fault message.
    """
    overall_status = "ok"
    overall_severity = "INFO"
    overall_message = "IRD config status is OK." # Default message

    # Prioritize API errors for overall status/message
    if details.get('api_errors'):
        overall_status = "error"
        overall_severity = "ERROR"
        overall_message = "API communication errors encountered: " + "; ".join(details["api_errors"])
        return overall_status, overall_severity, overall_message

    # Process faults to determine primary status and message
    if details.get('faults'):
        highest_severity_fault = None
        severity_rank = {"INFO": 0, "WARNING": 1, "MINOR": 1, "ALARM": 2, "MAJOR": 2, "CRITICAL": 3, "ERROR": 3}

        for fault in details['faults']:
            fault_type = fault.get('type', 'INFO').upper()
            if highest_severity_fault is None or severity_rank.get(fault_type, 0) > severity_rank.get(highest_severity_fault['type'].upper(), 0):
                highest_severity_fault = fault
        
        if highest_severity_fault:
            fault_severity = highest_severity_fault.get('type', '').upper()
            overall_message = f"Fault: {highest_severity_fault.get('details', 'N/A')} (Severity: {fault_severity}, Since: {highest_severity_fault.get('set_since', 'N/A')})"
            
            if fault_severity in ["CRITICAL", "MAJOR", "ALARM", "ERROR"]:
                overall_status = "alarm"
                overall_severity = "CRITICAL"
            elif fault_severity in ["WARNING", "MINOR", "INFO"]:
                overall_status = "warning"
                overall_severity = "WARNING"
    
    # Other status checks' messages are NOT combined into the primary 'message'
    # but their raw data is still available in 'details' for comprehensive logging.

    return overall_status, overall_severity, overall_message


def fetch_and_publish_ird_configs():
    """
    Fetches IRD configuration details from MySQL, polls each IRD device in parallel,
    and publishes the consolidated status to Redis.
    """
    logging.info("Starting IRD Config Agent cycle.")
    ird_configs = mysql_manager.get_ird_configs()
    
    if not ird_configs:
        logging.warning("No IRD configurations found in MySQL database.")
        return

    # Use ThreadPoolExecutor for parallel execution
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        # Submit each IRD config to the executor
        futures = [executor.submit(process_ird_config, config) for config in ird_configs]
        
        # Wait for all futures to complete (optional, can also handle results here)
        for future in concurrent.futures.as_completed(futures):
            try:
                future.result() # This will re-raise any exceptions caught in process_ird_config
            except Exception as e:
                logging.error(f"Error processing IRD config in parallel: {e}")

def process_ird_config(config):
    """
    Processes a single IRD configuration, fetches data, and publishes to Redis.
    This function is designed to be run in parallel.
    """
    ird_ip = config['ird_ip']
    username = config['username']
    password = config['password']
    system_id = config['system_id']
    channel_name_from_db = config.get('channel_name', f"Channel for {ird_ip}")
    description = config.get('description', f"IRD Device {ird_ip}")

    logging.info(f"Processing IRD config for IP: {ird_ip}")
    session_id = ird_api_client.login(ird_ip, username, password)
    
    payload_details = {
        "api_errors": []
    }

    if session_id:
        try:
            # Fetch various detailed status points
            rf_status = ird_api_client.get_rf_status(ird_ip, session_id)
            if rf_status is not None: payload_details['rf_status'] = rf_status
            else: payload_details['api_errors'].append("Failed to get RF status.")

            asi_status = ird_api_client.get_asi_status(ird_ip, session_id)
            if asi_status is not None: payload_details['asi_status'] = asi_status
            else: payload_details['api_errors'].append("Failed to get ASI status.")

            moip_output_status = ird_api_client.get_moip_output_status(ird_ip, session_id)
            if moip_output_status is not None: payload_details['moip_output_status'] = moip_output_status
            else: payload_details['api_errors'].append("Failed to get MOIP output status.")

            moip_output_config = ird_api_client.get_moip_output_config(ird_ip, session_id)
            if moip_output_config is not None: payload_details['moip_output_config'] = moip_output_config
            else: payload_details['api_errors'].append("Failed to get MOIP output configuration.")

            moip_input_status = ird_api_client.get_moip_input_status(ird_ip, session_id)
            if moip_input_status is not None: payload_details['moip_input_status'] = moip_input_status
            else: payload_details['api_errors'].append("Failed to get MOIP input status.")

            channel_status = ird_api_client.get_channel_status(ird_ip, session_id)
            if channel_status is not None: payload_details['channel_status'] = channel_status
            else: payload_details['api_errors'].append("Failed to get Channel status.")

            eth_status = ird_api_client.get_ethernet_status(ird_ip, session_id)
            if eth_status is not None: payload_details['ethernet_status'] = eth_status
            else: payload_details['api_errors'].append("Failed to get Ethernet status.")

            power_status = ird_api_client.get_power_supply_status(ird_ip, session_id)
            if power_status is not None: payload_details['power_supply_status'] = power_status
            else: payload_details['api_errors'].append("Failed to get Power Supply status.")
            
            faults = ird_api_client.get_fault_status(ird_ip, session_id)
            if faults is not None: payload_details['faults'] = faults
            else: payload_details['api_errors'].append("Failed to get Faults status.")

        except Exception as e:
            logging.error(f"Error fetching IRD details for {ird_ip}: {e}")
            payload_details['api_errors'].append(f"Unhandled error during data fetch: {e}")
        finally:
            # No explicit logout, session expires automatically or is reused
            pass
    else:
        payload_details['api_errors'].append("Failed to obtain session ID (login failed).")
            
    # Determine overall status and severity for the Redis message
    overall_status, overall_severity, overall_message = get_overall_status_from_details(payload_details)

    # Construct Redis payload
    redis_payload = {
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "agent_type": "ird_config",
        "device_ip": ird_ip,
        "device_name": description, # Use the description from DB as device_name
        "frontend_block_id": f"IRD_CONFIG_{ird_ip.replace('.', '_')}", # A unique ID for this config snapshot
        "group_name": channel_name_from_db, # Link to its channel name
        "message": overall_message,
        "severity": overall_severity,
        "status": overall_status,
        "details": payload_details # Include all fetched details for historical ElasticSearch
    }
    
    publish_status_to_redis(redis_payload)

def main():
    while True:
        fetch_and_publish_ird_configs()
        logging.info(f"IRD Config Agent sleeping for {POLLING_INTERVAL_SECONDS} seconds...")
        time.sleep(POLLING_INTERVAL_SECONDS)

if __name__ == "__main__":
    main()
