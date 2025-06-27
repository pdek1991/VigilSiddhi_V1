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

# Set logging level to DEBUG temporarily to get more verbose output
logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(levelname)s - %(message)s')

# --- Configuration ---
REDIS_HOST = os.environ.get('REDIS_HOST', '192.168.56.30')
REDIS_PORT = int(os.environ.get('REDIS_PORT', 6379))

ES_HOST = os.environ.get('ES_HOST', '192.168.56.30')
ES_PORT = int(os.environ.get('ES_PORT', 9200))

WEBSOCKET_NOTIFIER_URL = os.environ.get('WEBSOCKET_NOTIFIER_URL', 'http://127.0.0.1:8001/notify')

REDIS_STREAM_NAME = "vs:agent:pgm_routing_status" # Must match agent's stream name
CONSUMER_GROUP_NAME = "pgm_routing_es_ingester" # Unique group name for PGM routing consumer
CONSUMER_NAME = "pgm_routing_consumer_instance_1" # Corrected consumer name to remove the dot, as per common practice and potential issues

# Redis key prefix for storing PGM routing block states (per frontend block)
ALARM_STATE_REDIS_PREFIX = "vs:alarm_state:pgm:"

r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=0, decode_responses=False)
es = Elasticsearch(f"http://{ES_HOST}:{ES_PORT}")
http_session = None

async def send_websocket_notification(message_data):
    """Sends a JSON message to the WebSocket notifier's HTTP endpoint."""
    global http_session
    if http_session is None:
        http_session = aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=False))

    try:
        async with http_session.post(WEBSOCKET_NOTIFIER_URL, json=message_data) as response:
            if response.status != 200:
                logging.error(f"Failed to send WebSocket notification: {response.status} - {await response.text()}")
            else:
                logging.info(f"WebSocket notification sent successfully for {message_data.get('ip')} - {message_data.get('frontend_block_id')}.")
    except aiohttp.ClientConnectionError as e:
        logging.error(f"WebSocket notifier connection error: {e}. Is the notifier running?")
    except Exception as e:
        logging.error(f"Error sending WebSocket notification: {e}", exc_info=True)


def setup_consumer_group():
    """Ensures the consumer group exists for the stream."""
    try:
        r.xgroup_create(REDIS_STREAM_NAME, CONSUMER_GROUP_NAME, id='$', mkstream=True)
        logging.info(f"Created consumer group '{CONSUMER_GROUP_NAME}' for stream '{REDIS_STREAM_NAME}'")
    except redis.exceptions.ResponseError as e:
        if "BUSYGROUP" in str(e):
            logging.info(f"Consumer group '{CONSUMER_GROUP_NAME}' already exists for stream '{REDIS_STREAM_NAME}'.")
        else:
            logging.error(f"Error creating consumer group: {e}")
    except Exception as e:
        logging.error(f"Unhandled error during consumer group setup: {e}", exc_info=True)

def get_block_state_redis_key(device_ip, frontend_block_id):
    """Generates a unique Redis key for a PGM routing block's overall status."""
    # Ensure all components are strings and handle None values gracefully
    dev_ip_str = str(device_ip) if device_ip is not None else "N/A"
    block_id_str = str(frontend_block_id) if frontend_block_id is not None else "N/A"
    return f"{ALARM_STATE_REDIS_PREFIX}block:{dev_ip_str}_{block_id_str}"

def get_previous_block_severity(block_key):
    """Retrieves the previous overall severity of a PGM routing block from Redis."""
    try:
        state = r.get(block_key)
        if state:
            return state.decode('utf-8')
        return None
    except Exception as e:
        logging.error(f"Error getting previous block state from Redis for key {block_key}: {e}")
        return None

def set_current_block_severity(block_key, severity):
    """Sets the current overall severity of a PGM routing block in Redis."""
    try:
        r.set(block_key, severity)
        logging.debug(f"Set block state for {block_key} to {severity}")
    except Exception as e:
        logging.error(f"Error setting current block state in Redis for key {block_key}: {e}")

async def process_message(message_id, payload):
    """Processes a single message from the Redis stream, indexes to ES, and sends WS notifications."""
    logging.debug(f"Processing message ID: {message_id.decode()}")
    logging.debug(f"  Timestamp: {payload.get('timestamp')}")
    logging.debug(f"  Agent Type: {payload.get('agent_type')}")
    logging.debug(f"  Device IP: {payload.get('device_ip')}")
    logging.debug(f"  Device Name: {payload.get('device_name')}")
    logging.debug(f"  Frontend Block ID: {payload.get('frontend_block_id')}")
    logging.debug(f"  Group Name: {payload.get('group_name')}")
    logging.debug(f"  Status: {payload.get('status')}")
    logging.debug(f"  Severity: {payload.get('severity')}")
    logging.debug(f"  Message: {payload.get('message')}")
    logging.debug("-" * 50)

    current_status = payload.get('status')
    current_severity = payload.get('severity')
    frontend_block_id = payload.get('frontend_block_id')
    device_ip = payload.get('device_ip')
    timestamp = payload.get('timestamp')

    pgm_dest_check = payload.get('details', {}).get('pgm_dest_check', {})
    api_errors = payload.get('details', {}).get('api_errors', [])

    # Extract additional details for ES and WS
    polled_source = pgm_dest_check.get('polled_source', 'N/A')
    expected_source = pgm_dest_check.get('expected_source', 'N/A')
    source_name = pgm_dest_check.get('polled_source_name', 'N/A')

    # --- Alarm State Management for Frontend Notifications ---
    block_state_key = get_block_state_redis_key(device_ip, frontend_block_id)
    previous_block_severity = get_previous_block_severity(block_state_key)
    logging.debug(f"Block state for {frontend_block_id} (IP: {device_ip}): Previous='{previous_block_severity}', Current='{current_severity}'")


    send_ws_notification = False
    ws_severity = current_severity
    ws_status = current_status
    ws_message = payload.get('message')

    # Logic to determine if a WebSocket notification needs to be sent and its type
    # Normalize current and previous severity for robust comparison
    normalized_current_severity = current_severity.upper() if current_severity else 'UNKNOWN'
    normalized_previous_severity = previous_block_severity.upper() if previous_block_severity else 'UNKNOWN'

    # Define what constitutes an "alarming" state for PGM routing
    alarming_severities = ['CRITICAL', 'ERROR', 'WARNING', 'ALARM', 'MAJOR', 'MINOR']
    
    is_previous_alarm_state = normalized_previous_severity in alarming_severities
    is_current_alarm_state = normalized_current_severity in alarming_severities

    # Scenario 1: Alarm clears (was alarming, now OK/INFO/CLEARED)
    if not is_current_alarm_state: # If current state is NOT an alarm (i.e., OK, INFO, CLEARED, UNKNOWN)
        if is_previous_alarm_state:
            # Block was previously in an alarm state and is now no longer alarming -> Send CLEAR notification
            send_ws_notification = True
            ws_severity = "CLEARED" # Or "OK" as per frontend expectation
            ws_status = "cleared"
            ws_message = f"PGM Routing {payload.get('device_name')} (Block: {frontend_block_id}) is now OK."
            logging.info(f"PGM block {frontend_block_id} for {device_ip} has CLEARED. Previous severity: {previous_block_severity}. Sending WS notification.")
        else:
            logging.debug(f"PGM block {frontend_block_id} for {device_ip} is OK/INFO and no previous alarm. Skipping WS notification.")
        set_current_block_severity(block_state_key, 'OK') # Always update state to OK/INFO when not alarming
    # Scenario 2: New alarm or severity change for an active alarm
    elif is_current_alarm_state: # If current state IS an alarm
        if not is_previous_alarm_state or normalized_previous_severity != normalized_current_severity:
            # Send if it transitioned to an alarm, OR if existing alarm changed severity
            send_ws_notification = True
            logging.info(f"PGM block {frontend_block_id} for {device_ip} is in {current_severity} state. Previous: {previous_block_severity}. Sending WS notification.")
        else:
            logging.debug(f"PGM block {frontend_block_id} for {device_ip} is still {current_severity}. Severity not changed. Skipping WS notification.")
        set_current_block_severity(block_state_key, current_severity) # Update state to current severity


    # --- WebSocket Notification ---
    if send_ws_notification:
        websocket_message = {
            "device_name": payload.get('device_name'),
            "ip": device_ip,
            "time": timestamp,
            "message": ws_message, # Use the message determined by state logic
            "severity": ws_severity, # Use the severity determined by state logic
            "status": ws_status, # Use the status determined by state logic (e.g., "cleared", "mismatch", "error")
            "frontend_block_id": frontend_block_id, # Always include frontend_block_id
            "group_name": payload.get('group_name'),
            "pgm_dest": pgm_dest_check.get('pgm_dest', 'N/A'),
            "polled_value": polled_source,
            "expected_value": expected_source,
            "router_source": source_name, # Send source_name as router_source to frontend
            "oid": pgm_dest_check.get('oid', 'N/A') # Include OID if relevant
        }
        await send_websocket_notification(websocket_message)


    # --- Index to monitor_historical_alarms ---
    # Only index to Elasticsearch if the overall status is an active alarm.
    # Cleared states are typically not indexed as new alarms, but you could add separate logic
    # to log them as 'cleared events' in a different ES index if needed for audit trails.
    if is_current_alarm_state: # Only index active alarms (WARNING, CRITICAL, etc.)
        # Construct alarm message including polled, expected, and source_name values
        alarm_doc_message = payload.get('message', 'No specific alarm message.') # Default to agent's message

        if pgm_dest_check and pgm_dest_check.get("status") in ["MISMATCH", "Error"]:
            alarm_doc_message = (
                f"PGM Routing Alarm - Destination: {pgm_dest_check.get('pgm_dest', 'N/A')}, "
                f"Polled Source: '{polled_source}', Expected Source: '{expected_source}'"
            )
            if source_name and source_name != 'N/A':
                alarm_doc_message += f", Source Name: '{source_name}'"
            if pgm_dest_check.get('oid'):
                alarm_doc_message += f", OID: {pgm_dest_check.get('oid', 'N/A')}"
            
            # If the status from pgm_dest_check is "Error", it means SNMP polling itself had an issue
            if pgm_dest_check.get("status") == "Error":
                alarm_doc_message = (
                    f"PGM Routing SNMP Error - Dest: {pgm_dest_check.get('pgm_dest', 'N/A')}, "
                    f"Message: {pgm_dest_check.get('message', 'N/A')}"
                )
                if pgm_dest_check.get('oid'):
                    alarm_doc_message += f", OID: {pgm_dest_check.get('oid', 'N/A')}"


        alarm_doc = {
            "alarm_id": str(uuid.uuid4()),
            "timestamp": timestamp,
            "message": alarm_doc_message, # Use the detailed message for ES
            "device_name": payload.get('device_name'),
            "block_id": frontend_block_id,
            "severity": normalized_current_severity, # Use normalized severity
            "status": current_status.upper(),
            "type": payload.get('agent_type'),
            "device_ip": device_ip,
            "group_name": payload.get('group_name'),
            "pgm_dest": pgm_dest_check.get('pgm_dest', 'N/A'),
            "polled_source": polled_source,
            "expected_source": expected_source,
        }
        if source_name and source_name != 'N/A':
            alarm_doc["source_name"] = source_name
        if pgm_dest_check.get('oid'):
            alarm_doc["oid"] = pgm_dest_check.get('oid')


        try:
            es.index(index="monitor_historical_alarms", document=alarm_doc)
            logging.info(f"Indexed PGM routing alarm/error '{alarm_doc['alarm_id']}' for {device_ip} to Elasticsearch.")
        except Exception as e:
            logging.error(f"Failed to index PGM routing alarm/error to Elasticsearch: {e}", exc_info=True)
    else:
        logging.info(f"Skipping Elasticsearch indexing for {device_ip} as status is 'ok' or non-alarming.")


async def consume_messages():
    """Main function to consume messages from Redis Stream."""
    logging.info(f"Starting Redis consumer for stream '{REDIS_STREAM_NAME}' with group '{CONSUMER_GROUP_NAME}'.")
    while True:
        try:
            messages = r.xreadgroup(
                CONSUMER_GROUP_NAME,
                CONSUMER_NAME,
                {REDIS_STREAM_NAME: '>'},
                count=10,
                block=5000
            )

            if messages:
                for stream, message_list in messages:
                    for message_id, message_data in message_list:
                        try:
                            payload = json.loads(message_data[b'data'].decode('utf-8'))
                            await process_message(message_id, payload)
                            r.xack(stream, CONSUMER_GROUP_NAME, message_id)
                            logging.debug(f"Acknowledged message: {message_id}")
                        except json.JSONDecodeError as jde:
                            logging.error(f"JSON Decode Error for message {message_id.decode()} in {stream.decode()}: {jde}")
                            r.xack(stream, CONSUMER_GROUP_NAME, message_id)
                        except Exception as e:
                            logging.error(f"Error processing message {message_id.decode()} from {stream.decode()}: {e}", exc_info=True)
                            r.xack(stream, CONSUMER_GROUP_NAME, message_id)

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
