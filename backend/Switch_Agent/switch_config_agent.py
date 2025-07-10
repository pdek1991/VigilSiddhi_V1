# switch_config_agent.py

import sys
import os
import json
import logging
import redis
from datetime import datetime
import asyncio
from typing import Dict, Any, List
import time

# Adjust path to import custom modules
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__))))

from mysql_client import MySQLManager
from switch_api_client import switch_api_client # Import the updated client

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- Configuration ---
MYSQL_HOST = os.environ.get('MYSQL_HOST', '192.168.56.30')
MYSQL_DATABASE = os.environ.get('MYSQL_DATABASE', 'vigil_siddhi')
MYSQL_USER = os.environ.get('MYSQL_USER', 'vigilsiddhi')
MYSQL_PASSWORD = os.environ.get('MYSQL_PASSWORD', 'vigilsiddhi')

REDIS_HOST = os.environ.get('REDIS_HOST', '192.168.56.30')
REDIS_PORT = int(os.environ.get('REDIS_PORT', 6379))

REDIS_STREAM_NAME = "vs:agent:switch_status"
LAST_POLL_DATA_KEY_PREFIX = "switch_last_poll_data:"

POLLING_INTERVAL_SECONDS = 60

# Initialize Redis and MySQL clients
try:
    r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=0, decode_responses=True)
    r.ping()
    logging.info("Switch Config Agent: Successfully connected to Redis.")
except redis.exceptions.ConnectionError as e:
    logging.critical(f"FATAL: Failed to connect to Redis: {e}")
    sys.exit(1)

try:
    mysql_manager = MySQLManager(host=MYSQL_HOST, database=MYSQL_DATABASE, user=MYSQL_USER, password=MYSQL_PASSWORD)
    logging.info("Switch Config Agent: Successfully initialized MySQLManager.")
except Exception as e:
    logging.critical(f"FATAL: Failed to initialize MySQLManager: {e}")
    sys.exit(1)

from typing import Tuple

def get_alarms_and_overall_status(details: Dict[str, Any]) -> Tuple[str, List[Dict[str, Any]]]:
    """
    Determines overall status and generates a list of active alarms based on
    the 'api_errors' collected by the switch_api_client.
    """
    active_alarms = []
    
    # Use the 'api_errors' list from switch_api_client as the source for alarms
    for error_message in details.get("api_errors", []):
        # This is a simplified mapping. In a real scenario, you might parse
        # the error_message more granularly to assign specific alarm types/severities.
        alarm_type = "SNMP Data Issue" 
        severity = "WARNING" # Default severity

        if "operationally DOWN" in error_message or "High Temperature" in error_message or "Fan" in error_message and "state" in error_message or "Power Supply" in error_message and "state" in error_message:
            severity = "CRITICAL"
            if "Interface" in error_message:
                alarm_type = "Interface Status Alert"
            elif "Temperature" in error_message:
                alarm_type = "High Temperature Alert"
            elif "Fan" in error_message:
                alarm_type = "Fan Status Alert"
            elif "Power Supply" in error_message:
                alarm_type = "Power Supply Status Alert"
        elif "missing" in error_message:
            severity = "ERROR"
            alarm_type = "Missing SNMP Data"

        active_alarms.append({
            "alarm_type": alarm_type,
            "severity": severity,
            "message": error_message,
            "identifier": f"{details['ip']}_{alarm_type.replace(' ', '_')}_{hash(error_message)}" # Unique identifier for the alarm
        })

    overall_status = "alarm" if active_alarms else "ok"
    return overall_status, active_alarms

async def process_switch_config(config: Dict[str, Any]):
    """Processes a single switch, fetches data, calculates metrics, and publishes to Redis."""
    switch_ip = config.get('switch_ip')
    community = config.get('community')
    model = config.get('model')
    frontend_id = config.get('frontend_id', f"SW_{switch_ip}")

    if not all([switch_ip, community, model]):
        logging.error(f"Skipping switch config due to missing info: {config}")
        return

    logging.info(f"Processing switch: {switch_ip} (Model: {model})")
    
    try:
        # 1. Retrieve previous data for bitrate calculation
        last_poll_data_key = f"{LAST_POLL_DATA_KEY_PREFIX}{switch_ip}"
        last_poll_json = r.get(last_poll_data_key)
        last_poll_data = json.loads(last_poll_json) if last_poll_json else None

        # 2. Fetch current data using the updated switch_api_client
        # Pass last_poll_data to the client for in-client bitrate calculation
        current_details = await switch_api_client.get_switch_details(switch_ip, community, model, last_poll_data)
        
        # 3. Store current data for next poll
        # Only store necessary data for bitrate calculation and timestamp
        data_to_cache = {
            "timestamp": current_details["timestamp"],
            "interfaces": [{"index": i["index"], "in_octets": i["in_octets"], "out_octets": i["out_octets"]} for i in current_details.get("interfaces", [])]
        }
        r.set(last_poll_data_key, json.dumps(data_to_cache), ex=POLLING_INTERVAL_SECONDS * 5)

        # 4. Determine overall status and alarms based on collected details
        overall_status, active_alarms = get_alarms_and_overall_status(current_details)

        # 5. Prepare and publish payload to Redis Stream
        payload = {
            "timestamp": datetime.utcnow().isoformat() + "Z", # Use UTC for consistency
            "agent_type": "switch_monitor",
            "device_ip": switch_ip,
            "device_name": current_details.get("system_info", {}).get("hostname", switch_ip),
            "frontend_id": frontend_id,
            "model": model,
            "overall_status": overall_status,
            "active_alarms": active_alarms,
            "details": current_details # Send all fetched and processed details
        }
        
        r.xadd(REDIS_STREAM_NAME, {"data": json.dumps(payload)})
        logging.info(f"Published data for {switch_ip} to stream '{REDIS_STREAM_NAME}'. Status: {overall_status}, Alarms: {len(active_alarms)}")

    except Exception as e:
        logging.error(f"Error processing switch {switch_ip}: {e}", exc_info=True)

async def main_loop():
    """Main polling loop for the agent."""
    logging.info("Switch Config Agent starting main polling loop.")
    while True:
        logging.info(f"Starting new polling cycle. Interval: {POLLING_INTERVAL_SECONDS}s")
        try:
            switch_configs = mysql_manager.get_switch_configs()
            if not switch_configs:
                logging.warning("No switch configurations found in database.")
            else:
                tasks = [process_switch_config(config) for config in switch_configs]
                await asyncio.gather(*tasks)
        except Exception as e:
            logging.error(f"Error in polling cycle: {e}", exc_info=True)
            # Attempt to reconnect MySQL if connection is lost
            if "MySQL Connection not available" in str(e):
                mysql_manager.reconnect()

        await asyncio.sleep(POLLING_INTERVAL_SECONDS)

if __name__ == "__main__":
    if sys.platform.startswith('win'):
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    try:
        asyncio.run(main_loop())
    except KeyboardInterrupt:
        logging.info("Switch Config Agent stopped by user.")
    except Exception as e:
        logging.critical(f"Agent terminated with unhandled error: {e}", exc_info=True)
