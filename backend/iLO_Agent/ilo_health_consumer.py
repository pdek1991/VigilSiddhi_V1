import redis
import json
import time
import logging
import os
from elasticsearch import Elasticsearch
from datetime import datetime
import asyncio
import aiohttp
import uuid

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- Configuration ---
REDIS_HOST = os.environ.get('REDIS_HOST', '192.168.56.30')
REDIS_PORT = int(os.environ.get('REDIS_PORT', 6379))

ES_HOST = os.environ.get('ES_HOST', '192.168.56.30')
ES_PORT = int(os.environ.get('ES_PORT', 9200))

WEBSOCKET_NOTIFIER_URL = os.environ.get('WEBSOCKET_NOTIFIER_URL', 'http://127.0.0.1:8001/notify')

# Redis Stream Names to listen to
REDIS_STREAM_NAMES = [
    "vs:agent:iloM_status",
    "vs:agent:iloP_status",
    "vs:agent:iloB_status",
    "vs:agent:enc_iloM_status", # New stream for Compression M
    "vs:agent:enc_iloB_status", # New stream for Compression B
    "vs:agent:ilo_status" # For devices not explicitly mapped
]
CONSUMER_GROUP_NAME = "es_ingester_ilo_health"
CONSUMER_NAME = "ilo_health_consumer_instance_1"

# Initialize Redis client
r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=0, decode_responses=False)

# Initialize Elasticsearch client
es = Elasticsearch(f"http://{ES_HOST}:{ES_PORT}")

# Async HTTP session for WebSocket notifier communication
http_session = None

# --- Local state management for frontend block alarms ---
# This dictionary will store the last known status and details for each frontend_block_id
# to prevent sending redundant notifications and to detect alarm clearing.
# Format: { "frontend_block_id": {"status": "OK", "severity": "INFO", "message": "..."} }
last_known_states = {}

async def send_websocket_notification(message_data):
    """Sends a JSON message to the WebSocket notifier's HTTP endpoint."""
    global http_session
    if http_session is None or http_session.closed:
        # Explicitly set ssl=False for the connector as the notifier is HTTP
        http_session = aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=False))

    try:
        async with http_session.post(WEBSOCKET_NOTIFIER_URL, json=message_data) as response:
            if response.status != 200:
                logging.error(f"Failed to send WebSocket notification: {response.status} - {await response.text()}")
            else:
                logging.info(f"WebSocket notification sent successfully for {message_data.get('frontend_block_id')} (IP: {message_data.get('ip')}).")
    except aiohttp.ClientConnectionError as e:
        logging.error(f"WebSocket notifier connection error: {e}. Is the notifier running?")
    except Exception as e:
        logging.error(f"Error sending WebSocket notification: {e}", exc_info=True)


def setup_consumer_group():
    """Ensures the consumer group exists for all relevant streams."""
    for stream_name in REDIS_STREAM_NAMES:
        try:
            r.xgroup_create(stream_name, CONSUMER_GROUP_NAME, id='$', mkstream=True)
            logging.info(f"Created consumer group '{CONSUMER_GROUP_NAME}' for stream '{stream_name}'")
        except redis.exceptions.ResponseError as e:
            if "BUSYGROUP" in str(e):
                logging.info(f"Consumer group '{CONSUMER_GROUP_NAME}' already exists for stream '{stream_name}'.")
            else:
                logging.error(f"Error creating consumer group for {stream_name}: {e}")
        except Exception as e:
            logging.error(f"Unhandled error during consumer group setup for {stream_name}: {e}", exc_info=True)

async def process_message(stream_name, message_id, payload):
    """Processes a single message from the Redis stream, indexes to ES, and sends WS notifications based on state changes."""
    logging.info(f"Processing message ID: {message_id.decode()} from stream: {stream_name}")
    
    device_ip = payload.get('device_ip')
    device_name = payload.get('device_name')
    frontend_block_id = payload.get('frontend_block_id') # Extract frontend_block_id
    overall_status = payload.get('status').upper() # Ensure status is uppercase for consistent comparison
    overall_severity = payload.get('severity').upper() # Ensure severity is uppercase
    overall_message = payload.get('message')
    timestamp = payload.get('timestamp')
    agent_type = payload.get('agent_type')
    group_name = payload.get('group_name') # Retrieve the group_name

    logging.info(f"  Device IP: {device_ip}, Status: {overall_status}, Severity: {overall_severity}, Group: {group_name}, Frontend Block ID: {frontend_block_id}")

    # Retrieve the last known state for this frontend_block_id
    # Default to an 'OK' state if not seen before.
    last_state = last_known_states.get(frontend_block_id, {
        "status": "OK",
        "severity": "INFO",
        "message": ""
    })

    # Determine if the new status constitutes an alarm (not 'OK')
    current_is_alarm = overall_status in ["ALARM", "ERROR", "WARNING"]
    last_was_alarm = last_state["status"] in ["ALARM", "ERROR", "WARNING"]

    # --- Index to monitor_historical_alarms ---
    # Only index to Elasticsearch if the status is an alarm (NOT 'OK')
    if current_is_alarm:
        component_health = payload.get('details', {}).get('component_health', {})
        iml_alarms = payload.get('details', {}).get('iml_alarms', [])
        api_errors = payload.get('details', {}).get('api_errors', [])

        # Create a base alarm document
        alarm_doc = {
            "alarm_id": str(uuid.uuid4()),
            "timestamp": timestamp,
            "device_name": device_name,
            "block_id": frontend_block_id, # Use frontend_block_id as the device ID
            "severity": overall_severity,
            "status": overall_status,
            "type": agent_type,
            "device_ip": device_ip,
            "group_name": group_name, # Include the group_name in ES document
            "message": overall_message,
            "details": {
                "component_health": component_health,
                "iml_alarms": iml_alarms,
                "api_errors": api_errors
            }
        }
        
        try:
            es.index(index="monitor_historical_alarms", document=alarm_doc)
            logging.info(f"Indexed iLO health alarm '{alarm_doc['alarm_id']}' for {frontend_block_id} (Group: {group_name}) to Elasticsearch.")
        except Exception as e:
            logging.error(f"Failed to index iLO health alarm to Elasticsearch for {frontend_block_id}: {e}", exc_info=True)
    else:
        logging.info(f"Skipping Elasticsearch indexing for {frontend_block_id} as status is 'OK'.")


    # --- WebSocket Notification Logic ---
    websocket_message = None

    # Determine group_id based on group_name for WebSocket notification
    group_id = None
    if group_name == "VERSIO M":
        group_id = "G.iloM"
    elif group_name == "VERSIO P":
        group_id = "G.iloP"
    elif group_name == "VERSIO B":
        group_id = "G.iloB"
    elif group_name == "Compression M":
        group_id = "G.COMPRESSION_M"
    elif group_name == "Compression B":
        group_id = "G.COMPRESSION_B"

    # Scenario 1: Transition from OK to Alarm state
    if current_is_alarm and not last_was_alarm:
        logging.info(f"New alarm detected for {frontend_block_id}: {overall_status} - {overall_message}")
        websocket_message = {
            "device_name": device_name,
            "ip": device_ip,
            "time": timestamp,
            "message": overall_message,
            "severity": overall_severity,
            "group_name": group_name,
            "frontend_block_id": frontend_block_id,
            "group_id": group_id
        }
    # Scenario 2: Ongoing alarm state, check if message or severity changed
    elif current_is_alarm and last_was_alarm:
        if (overall_status != last_state["status"] or
            overall_severity != last_state["severity"] or
            overall_message != last_state["message"]):
            
            logging.info(f"Alarm details changed for {frontend_block_id}. Sending update: {overall_status} - {overall_message}")
            websocket_message = {
                "device_name": device_name,
                "ip": device_ip,
                "time": timestamp,
                "message": overall_message,
                "severity": overall_severity,
                "group_name": group_name,
                "frontend_block_id": frontend_block_id,
                "group_id": group_id
            }
        else:
            logging.info(f"Alarm for {frontend_block_id} is ongoing with no change. Skipping WebSocket notification.")
    # Scenario 3: Transition from Alarm state to OK
    elif not current_is_alarm and last_was_alarm:
        logging.info(f"Alarm cleared for {frontend_block_id}. Sending 'OK cleared' notification.")
        websocket_message = {
            "device_name": device_name,
            "ip": device_ip,
            "time": timestamp,
            "message": f"iLO health for {device_name} ({device_ip}) is OK. All previous alarms cleared.",
            "severity": "INFO", # Always INFO for cleared messages
            "group_name": group_name,
            "frontend_block_id": frontend_block_id,
            "group_id": group_id
        }
    # Scenario 4: Already OK and remains OK (do nothing, no notification needed)
    else:
        logging.info(f"Status for {frontend_block_id} is OK and was already OK. Skipping WebSocket notification.")

    # Send the WebSocket message if it was prepared
    if websocket_message:
        await send_websocket_notification(websocket_message)
    
    # Update the last known state for this frontend_block_id
    last_known_states[frontend_block_id] = {
        "status": overall_status,
        "severity": overall_severity,
        "message": overall_message
    }


async def consume_messages():
    """Main function to consume messages from Redis Streams."""
    logging.info(f"Starting Redis consumer for streams: {', '.join(REDIS_STREAM_NAMES)} with group '{CONSUMER_GROUP_NAME}'.")
    
    # Initialize streams_to_read dictionary for xreadgroup
    streams_to_read = {stream_name: '>' for stream_name in REDIS_STREAM_NAMES}

    while True:
        try:
            messages = r.xreadgroup(
                CONSUMER_GROUP_NAME,
                CONSUMER_NAME,
                streams_to_read, # Use the dictionary of streams
                count=10,
                block=5000 # Block for 5000 milliseconds (5 seconds) if no messages
            )

            if messages:
                for stream_bytes, message_list in messages:
                    stream_name = stream_bytes.decode('utf-8') # Decode stream name
                    for message_id, message_data in message_list:
                        try:
                            payload = json.loads(message_data[b'data'].decode('utf-8'))
                            await process_message(stream_name, message_id, payload)

                            r.xack(stream_bytes, CONSUMER_GROUP_NAME, message_id)
                            logging.debug(f"Acknowledged message: {message_id} from {stream_name}")

                        except json.JSONDecodeError as jde:
                            logging.error(f"JSON Decode Error for message {message_id.decode()} in {stream_name}: {jde}")
                            r.xack(stream_bytes, CONSUMER_GROUP_NAME, message_id)
                        except Exception as e:
                            logging.error(f"Error processing message {message_id.decode()} from {stream_name}: {e}", exc_info=True)
                            r.xack(stream_bytes, CONSUMER_GROUP_NAME, message_id)

        except redis.exceptions.ConnectionError as e:
            logging.error(f"Redis connection error: {e}. Retrying in 5 seconds...")
            time.sleep(5)
        except Exception as e:
            logging.error(f"Unhandled error in Redis stream listener: {e}", exc_info=True)
            time.sleep(1)

async def main_async():
    """Main asynchronous entry point."""
    setup_consumer_group()
    await consume_messages()

if __name__ == "__main__":
    try:
        asyncio.run(main_async())
    except KeyboardInterrupt:
        logging.info("Consumer stopped by user.")
    finally:
        if http_session:
            asyncio.run(http_session.close())

