import sys
import os
import time
import json
import logging
import redis
from datetime import datetime
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

REDIS_STREAM_NAME = "vs:agent:ird_trend_status"
POLLING_INTERVAL_SECONDS = 120 # Poll IRD trend data every 15 seconds (more frequent for trend)
MAX_WORKERS = 10 # Number of parallel threads to use for fetching IRD data

# Initialize Redis and MySQL clients
r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=0, decode_responses=False)
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

def get_trend_status_from_metrics(C_N_margin, signal_strength):
    """
    Determines status and severity based on trend metrics.
    Example thresholds, adjust as needed.
    """
    status = "ok"
    severity = "INFO"
    message = "IRD trend metrics are within nominal range."

    if C_N_margin is None or signal_strength is None:
        status = "unknown"
        severity = "WARNING"
        message = "Missing C/N margin or Signal Strength data for trend. Check active RF inputs."
        return status, severity, message

    # Example thresholds (adjust these based on your device specifications)
    if C_N_margin < 3.0: # Below critical C/N
        status = "alarm"
        severity = "CRITICAL"
        message = f"Critical C/N Margin: {C_N_margin} dB. Signal degradation detected."
    elif C_N_margin < 5.0: # Below warning C/N
        status = "warning"
        severity = "WARNING"
        message = f"Low C/N Margin: {C_N_margin} dB. Potential signal issues."
    
    if signal_strength < -60.0: # Below critical signal strength (more negative is weaker)
        if status != "alarm": # Don't downgrade from alarm
            status = "alarm"
            severity = "CRITICAL"
            message = f"Critical Signal Strength: {signal_strength} dBm. Signal too weak."
    elif signal_strength < -50.0: # Below warning signal strength
        if status == "ok": # Only upgrade from OK if no other issues
            status = "warning"
            severity = "WARNING"
            message = f"Low Signal Strength: {signal_strength} dBm."
    
    return status, severity, message

def process_ird_trend(config):
    """
    Processes a single IRD configuration for trend data, fetches it, and publishes to Redis.
    This function is designed to be run in parallel.
    """
    ird_ip = config['ird_ip']
    username = config['username']
    password = config['password']
    system_id = config['system_id']
    # Default description from DB config
    description = config.get('description', f"IRD Device {ird_ip}")

    logging.info(f"Processing IRD trend for IP: {ird_ip}")
    session_id = ird_api_client.login(ird_ip, username, password)
    
    rf_status_details = []
    C_N_margin = None
    signal_strength = None
    api_errors = []
    channel_statuses = [] # Initialize channel_statuses to an empty list
    dynamic_channel_name = None # To store channel name fetched from /pe

    if session_id:
        try:
            rf_data_list = ird_api_client.get_rf_status(ird_ip, session_id)
            if rf_data_list is not None: # Check if the call itself failed
                rf_status_details = rf_data_list
                # For trend, take metrics from the first active (locked) RF input found
                first_active_rf = next((rf for rf in rf_data_list if rf.get('lock_status') == 'Lock+Sig'), None)
                if first_active_rf:
                    C_N_margin = first_active_rf.get('cn_margin')
                    signal_strength = first_active_rf.get('input_level_dbm')
                else:
                    api_errors.append("No active (locked) RF input found for trend data.")
            else:
                api_errors.append("Failed to get RF status for trend data (API call returned None).")

            # Fetch channel status from /pe endpoint
            channel_statuses = ird_api_client.get_channel_status(ird_ip, session_id)
            if channel_statuses:
                # Get the channel name from the first active program
                # Assuming the first active program's name is sufficient for Channel_Name
                dynamic_channel_name = channel_statuses[0].get('channel_name')
            else:
                logging.info(f"No active channels found for {ird_ip} from /pe endpoint.")

        except Exception as e:
            logging.error(f"Error fetching IRD RF trend data for {ird_ip}: {e}")
            api_errors.append(f"Unhandled error during trend data fetch: {e}")
    else:
        api_errors.append("Failed to obtain session ID (login failed).")
        
    # Determine status and severity based on fetched metrics
    overall_status, overall_severity, current_message = get_trend_status_from_metrics(C_N_margin, signal_strength)
    
    # If API errors exist, overwrite overall status/severity to reflect error
    if api_errors:
        overall_status = "error"
        overall_severity = "ERROR"
        if not current_message.startswith("Error:"): # Avoid duplicating prefix
            current_message = "Error: " + "; ".join(api_errors)
        else:
            current_message += "; " + "; ".join(api_errors)

    # Use dynamically fetched channel name if available, otherwise fallback to description
    channel_name_for_payload = dynamic_channel_name if dynamic_channel_name else description

    # Construct Redis payload
    redis_payload = {
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "agent_type": "ird_trend",
        "device_ip": ird_ip,
        "device_name": description,
        "frontend_block_id": f"IRD_TREND_{ird_ip.replace('.', '_')}", # Unique ID for this trend data point
        "Channel_Name": channel_name_for_payload, # Link to its channel name (dynamically fetched or fallback)
        "message": current_message,
        "severity": overall_severity,
        "status": overall_status,
        "details": {
            "C_N_margin": C_N_margin,
            "signal_strength": signal_strength,
            "system_id": system_id, # Include system_id for ES schema
            "rf_raw_data": rf_status_details, # Include all raw RF data for debug/more details
            "channel_info": channel_statuses, # Include full channel status for completeness
            "api_errors": api_errors # Log API errors in details
        }
    }
    
    publish_status_to_redis(redis_payload)


def fetch_and_publish_ird_trends():
    """
    Fetches IRD configurations from MySQL, polls each IRD device in parallel for trend data,
    and publishes the data to Redis.
    """
    logging.info("Starting IRD Trend Agent cycle.")
    ird_configs = mysql_manager.get_ird_configs()
    
    if not ird_configs:
        logging.warning("No IRD configurations found in MySQL database for trend monitoring.")
        return

    # Use ThreadPoolExecutor for parallel execution
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        # Submit each IRD config to the executor
        futures = [executor.submit(process_ird_trend, config) for config in ird_configs]
        
        # Wait for all futures to complete (optional, can also handle results here)
        for future in concurrent.futures.as_completed(futures):
            try:
                future.result() # This will re-raise any exceptions caught in process_ird_trend
            except Exception as e:
                logging.error(f"Error processing IRD trend in parallel: {e}")

def main():
    while True:
        fetch_and_publish_ird_trends()
        logging.info(f"IRD Trend Agent sleeping for {POLLING_INTERVAL_SECONDS} seconds...")
        time.sleep(POLLING_INTERVAL_SECONDS)

if __name__ == "__main__":
    main()
