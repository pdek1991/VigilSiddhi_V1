# ird_config_agent.py
import sys
import os
import time
import json
import logging
import redis
from datetime import datetime
import concurrent.futures

# Adjust path to import MySQLManager and IRDAPIClient
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__))))

# Import custom modules
from mysql_client import MySQLManager
from ird_api_client import ird_api_client

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- Configuration ---
MYSQL_HOST = os.environ.get('MYSQL_HOST', '192.168.56.30')
MYSQL_DATABASE = os.environ.get('MYSQL_DATABASE', 'vigil_siddhi')
MYSQL_USER = os.environ.get('MYSQL_USER', 'vigilsiddhi')
MYSQL_PASSWORD = os.environ.get('MYSQL_PASSWORD', 'vigilsiddhi')

REDIS_HOST = os.environ.get('REDIS_HOST', '192.168.56.30')
REDIS_PORT = int(os.environ.get('REDIS_PORT', 6379))

REDIS_STREAM_NAME = "vs:agent:ird_config_status"
POLLING_INTERVAL_SECONDS = 30
MAX_WORKERS = 10

# Initialize Redis and MySQL clients
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

def get_overall_status_from_details(details, device_ip):
    """
    Determines overall status and severity for IRD config based primarily on faults and API errors.
    The 'message' field will only display the highest severity fault message or API error message.
    """
    overall_status = "ok"
    overall_severity = "INFO"
    overall_message = f"IRD device {device_ip} status is OK." # Default message

    # Prioritize API errors for overall status/message
    if details.get('api_errors'):
        overall_status = "error"
        overall_severity = "ERROR"
        overall_message = f"API communication errors encountered for {device_ip}: " + "; ".join(details["api_errors"])
        return overall_status, overall_severity, overall_message

    # Process faults to determine primary status and message
    if details.get('faults'):
        highest_severity_fault = None
        # Mapping from interpreted severity strings to a numerical rank for comparison
        severity_rank = {
            "INFO": 0, "normal": 0,
            "WARNING": 1, "minor": 1,
            "ALARM": 2, "major": 2,
            "CRITICAL": 3, "error": 3
        }

        for fault in details['faults']:
            # The 'type' field comes from IRDAPIClient's interpreted value (e.g., "CRITICAL")
            fault_severity_str = fault.get('type', 'INFO').upper()
            current_rank = severity_rank.get(fault_severity_str, 0) # Default to 0 (INFO)

            if highest_severity_fault is None or current_rank > severity_rank.get(highest_severity_fault['type'].upper(), 0):
                highest_severity_fault = fault
        
        if highest_severity_fault:
            payload_severity = "INFO"
            fault_severity_rank = severity_rank.get(highest_severity_fault['type'].upper(), 0)
            
            if fault_severity_rank == 3: # CRITICAL / ERROR
                payload_severity = "CRITICAL"
                overall_status = "alarm"
            elif fault_severity_rank == 2: # MAJOR / ALARM
                payload_severity = "MAJOR"
                overall_status = "alarm"
            elif fault_severity_rank == 1: # WARNING / MINOR
                payload_severity = "WARNING"
                overall_status = "warning"
            
            overall_message = f"Fault: {highest_severity_fault.get('details', 'N/A')} (Severity: {highest_severity_fault.get('type', 'N/A')}, Since: {highest_severity_fault.get('set_since', 'N/A')})"
            overall_severity = payload_severity # Set the final overall severity

    return overall_status, overall_severity, overall_message


def fetch_and_publish_ird_configs():
    """
    Fetches IRD configuration details from MySQL, polls each IRD device in parallel,
    and publishes the consolidated status to Redis.
    """
    logging.info("Starting IRD Config Agent cycle.")
    
    # --- IMPORTANT FIX: Reconnect MySQL to fetch latest data ---
    # Only reconnect if the connection is actually closed or not established.
    if mysql_manager and (not mysql_manager.connection or not mysql_manager.connection.is_connected()):
        try:
            mysql_manager.reconnect()
            logging.info("Successfully reconnected to MySQL.")
        except Exception as e:
            logging.error(f"Failed to reconnect to MySQL: {e}. Cannot fetch IRD configurations.", exc_info=True)
            return
    elif not mysql_manager:
        logging.error("MySQLManager not initialized. Cannot fetch IRD configurations.")
        return
    
    # Ensure MySQLManager is initialized and connected after reconnect (or if it was already connected)
    if not mysql_manager.connection or not mysql_manager.connection.is_connected():
        logging.error("MySQLManager not connected after all attempts. Cannot fetch IRD configurations.")
        return

    ird_configs = mysql_manager.get_ird_configs()
    
    if ird_configs is None:
        logging.error("Received None for IRD configurations from MySQLManager. Assuming database error or no data.")
        return

    if not ird_configs:
        logging.warning("No IRD configurations found in MySQL database. Skipping polling cycle.")
        return

    logging.info(f"Found {len(ird_configs)} IRD configurations to process.")

    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = [executor.submit(process_ird_config, config) for config in ird_configs]
        
        for future in concurrent.futures.as_completed(futures):
            try:
                future.result()
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
    # system_id = config['system_id'] # Not directly used for API calls
    channel_name_from_db = config.get('channel_name', f"Channel for {ird_ip}")
    description = config.get('description', f"IRD Device {ird_ip}")
    input_type = config.get('input', 'UNKNOWN').upper() # Get input type from DB

    logging.info(f"Processing IRD config for IP: {ird_ip} with input type: {input_type}")
    
    payload_details = {
        "api_errors": []
    }

    session_id = None
    try:
        session_id = ird_api_client.login(ird_ip, username, password)
    except Exception as e:
        logging.error(f"Error logging into IRD {ird_ip}: {e}", exc_info=True)
        payload_details['api_errors'].append(f"Login failed: {e}")

    if session_id:
        try:
            # Dynamically fetch input status based on the 'input' column
            if input_type == 'RF':
                rf_status = ird_api_client.get_rf_status(ird_ip, session_id)
                if rf_status is not None: payload_details['rf_status'] = rf_status
                else: payload_details['api_errors'].append("Failed to get RF status.")
            elif input_type == 'ASI':
                asi_status = ird_api_client.get_asi_status(ird_ip, session_id)
                if asi_status is not None: payload_details['asi_status'] = asi_status
                else: payload_details['api_errors'].append("Failed to get ASI status.")
            elif input_type == 'IP': # Assuming 'IP' corresponds to MOIP input
                moip_input_status = ird_api_client.get_moip_input_status(ird_ip, session_id)
                if moip_input_status is not None: payload_details['moip_input_status'] = moip_input_status
                else: payload_details['api_errors'].append("Failed to get MOIP input status.")
            else:
                logging.warning(f"Unknown input type '{input_type}' for IRD {ird_ip}. No specific input status fetched.")

            # Remaining logic to check /ws/v2/service_cfg/output/moip, /ws/v2/status/pe,
            # /ws/v2/status/device/eth, /ws/v2/status/device/power and /ws/v2/status/faults/status remains same
            moip_output_status = ird_api_client.get_moip_output_status(ird_ip, session_id)
            if moip_output_status is not None: payload_details['moip_output_status'] = moip_output_status
            else: payload_details['api_errors'].append("Failed to get MOIP output status.")

            moip_output_config = ird_api_client.get_moip_output_config(ird_ip, session_id)
            if moip_output_config is not None: payload_details['moip_output_config'] = moip_output_config
            else: payload_details['api_errors'].append("Failed to get MOIP output configuration.")

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
            logging.error(f"Error fetching IRD details for {ird_ip}: {e}", exc_info=True)
            payload_details['api_errors'].append(f"Unhandled error during data fetch: {e}")
        # finally:
        #     # Attempt to logout, but don't fail the entire process if it doesn't work
        #     if session_id:
        #         try:
        #             ird_api_client.logout(ird_ip, session_id)
        #         except Exception as e:
        #             logging.warning(f"Failed to logout from IRD {ird_ip}: {e}")
    else:
        logging.error(f"Skipping data fetch for {ird_ip} due to prior login failure.")
            
    overall_status, overall_severity, overall_message = get_overall_status_from_details(payload_details, ird_ip)

    redis_payload = {
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "agent_type": "ird_config",
        "device_ip": ird_ip,
        "device_name": description,
        "frontend_block_id": "G.IRD", # Fixed value as requested
        "group_name": channel_name_from_db,
        "message": overall_message,
        "severity": overall_severity,
        "status": overall_status,
        "details": payload_details
    }
    
    publish_status_to_redis(redis_payload)

def main():
    while True:
        fetch_and_publish_ird_configs()
        logging.info(f"IRD Config Agent sleeping for {POLLING_INTERVAL_SECONDS} seconds...")
        time.sleep(POLLING_INTERVAL_SECONDS)

if __name__ == "__main__":
    main()