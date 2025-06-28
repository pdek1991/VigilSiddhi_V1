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

REDIS_STREAM_NAME = "vs:agent:ird_trend_status"
POLLING_INTERVAL_SECONDS = 300 # Poll IRD trend data every 2 minutes
MAX_WORKERS = 10

# Initialize Redis and MySQL clients
r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=0, decode_responses=False)
mysql_manager = MySQLManager(
    host=MYSQL_HOST,
    database=MYSQL_DATABASE,
    user=MYSQL_USER,
    password=MYSQL_PASSWORD
)

def publish_status_to_redis(payload):
    """Publishes a trend status message to the Redis Stream."""
    try:
        r.xadd(REDIS_STREAM_NAME, {"data": json.dumps(payload).encode('utf-8')})
        logging.info(f"Published to {REDIS_STREAM_NAME}: {payload.get('frontend_block_id', payload.get('device_ip'))}")
    except Exception as e:
        logging.error(f"Failed to publish to Redis stream {REDIS_STREAM_NAME}: {e}")

def process_ird_trend(config):
    """
    Processes a single IRD configuration, fetches trend data, and publishes to Redis.
    This function is designed to be run in parallel.
    """
    ird_ip = config['ird_ip']
    username = config['username']
    password = config['password']
    system_id = config.get('system_id', 'N/A')
    channel_name_from_db = config.get('channel_name', f"Channel for {ird_ip}")
    description = config.get('description', f"IRD Device {ird_ip}")
    input_type = config.get('input', 'UNKNOWN').upper() # Get input type from DB

    logging.info(f"Processing IRD trend for IP: {ird_ip} with input type: {input_type}")
    session_id = ird_api_client.login(ird_ip, username, password)

    api_errors = []
    trend_data = {}

    if session_id:
        try:
            # Dynamically fetch input status based on the 'input' column
            if input_type == 'RF':
                rf_status = ird_api_client.get_rf_status(ird_ip, session_id)
                if rf_status is not None: trend_data['rf_status'] = rf_status
                else: api_errors.append("Failed to get RF status for trend.")
            elif input_type == 'ASI':
                asi_status = ird_api_client.get_asi_status(ird_ip, session_id)
                if asi_status is not None: trend_data['asi_status'] = asi_status
                else: api_errors.append("Failed to get ASI status for trend.")
            elif input_type == 'IP': # Assuming 'IP' corresponds to MOIP input
                moip_input_status = ird_api_client.get_moip_input_status(ird_ip, session_id)
                if moip_input_status is not None: trend_data['moip_input_status'] = moip_input_status
                else: api_errors.append("Failed to get MOIP input status for trend.")
            else:
                logging.warning(f"Unknown input type '{input_type}' for IRD {ird_ip}. No specific input trend status fetched.")

            # Additional trend data points (e.g., channel, eth, power, faults) - these remain unchanged
            channel_status = ird_api_client.get_channel_status(ird_ip, session_id)
            if channel_status is not None: trend_data['channel_status'] = channel_status
            else: api_errors.append("Failed to get Channel status for trend.")
            
            eth_status = ird_api_client.get_ethernet_status(ird_ip, session_id)
            if eth_status is not None: trend_data['ethernet_status'] = eth_status
            else: api_errors.append("Failed to get Ethernet status for trend.")

            power_status = ird_api_client.get_power_supply_status(ird_ip, session_id)
            if power_status is not None: trend_data['power_supply_status'] = power_status
            else: api_errors.append("Failed to get Power Supply status for trend.")
            
            faults = ird_api_client.get_fault_status(ird_ip, session_id)
            if faults is not None: trend_data['faults'] = faults
            else: api_errors.append("Failed to get Faults status for trend.")

        except Exception as e:
            logging.error(f"Error fetching IRD trend details for {ird_ip}: {e}")
            api_errors.append(f"Unhandled error during trend data fetch: {e}")
        finally:
            pass
    else:
        api_errors.append("Failed to obtain session ID (login failed) for trend data.")

    # Construct Redis payload for trend data
    redis_payload = {
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "agent_type": "ird_trend",
        "device_ip": ird_ip,
        "device_name": description,
        "frontend_block_id": f"IRD_TREND_{ird_ip.replace('.', '_')}",
        "group_name": channel_name_from_db,
        "system_id": system_id,
        "trend_data": trend_data,
        "api_errors": api_errors
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

    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = [executor.submit(process_ird_trend, config) for config in ird_configs]
        
        for future in concurrent.futures.as_completed(futures):
            try:
                future.result()
            except Exception as e:
                logging.error(f"Error processing IRD trend in parallel: {e}")

def main():
    while True:
        fetch_and_publish_ird_trends()
        logging.info(f"IRD Trend Agent sleeping for {POLLING_INTERVAL_SECONDS} seconds...")
        time.sleep(POLLING_INTERVAL_SECONDS)

if __name__ == "__main__":
    main()