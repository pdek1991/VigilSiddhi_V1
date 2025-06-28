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

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s') # Changed to INFO for production readability

# --- Configuration ---
REDIS_HOST = os.environ.get('REDIS_HOST', '192.168.56.30')
REDIS_PORT = int(os.environ.get('REDIS_PORT', 6379))

ES_HOST = os.environ.get('ES_HOST', '192.168.56.30')
ES_PORT = int(os.environ.get('ES_PORT', 9200))

WEBSOCKET_NOTIFIER_URL = os.environ.get('WEBSOCKET_NOTIFIER_URL', 'http://127.0.0.1:8001/notify')

REDIS_STREAM_NAME = "vs:agent:pgm_routing_status" # Must match agent's stream name
CONSUMER_GROUP_NAME = "pgm_routing_es_ingester" # Unique group name for PGM routing consumer
CONSUMER_NAME = "pgm_routing_consumer_instance_1" # Corrected consumer name to remove the dot, as per common practice and potential issues

# Redis Hash name for storing PGM routing block states (full state per unique device)
# Key: 'pgm_alarm_states_per_device' (Redis Hash name)
# Field: f"{frontend_block_id}_{device_ip}" (e.g., 'C.102_192.168.1.10', 'G.COMPRESSION_M_172.19.180.72')
# Value: JSON string of {"status": "ALARM", "severity": "CRITICAL", "message": "..."}
PGM_ALARM_STATES_PER_DEVICE_KEY = "pgm_alarm_states_per_device"


r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=0, decode_responses=False)
es = Elasticsearch(f"http://{ES_HOST}:{ES_PORT}")
http_session = None

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

async def process_message(message_id, payload):
    """Processes a single message from the Redis stream, indexes to ES, and sends WS notifications."""
    logging.debug(f"Processing message ID: {message_id.decode()}")
    logging.debug(f"  Payload: {json.dumps(payload, indent=2)}")

    device_ip = payload.get('device_ip')
    device_name = payload.get('device_name')
    frontend_block_id = payload.get('frontend_block_id')
    
    # These are the *actual* status/severity/message from the agent's poll
    overall_status_from_agent = payload.get('status', 'UNKNOWN').upper() 
    overall_severity_from_agent = payload.get('severity', 'UNKNOWN').upper() 
    overall_message_from_agent = payload.get('message', 'No specific status message.')
    timestamp = payload.get('timestamp')
    agent_type = payload.get('agent_type')
    group_name = payload.get('group_name')

    pgm_dest_check = payload.get('details', {}).get('pgm_dest_check', {})
    api_errors = payload.get('details', {}).get('api_errors', [])

    polled_source = pgm_dest_check.get('polled_source', 'N/A')
    expected_source = pgm_dest_check.get('expected_source', 'N/A')
    source_name = pgm_dest_check.get('polled_source_name', 'N/A')

    logging.info(f"  Device IP: {device_ip}, Status (Agent): {overall_status_from_agent}, Severity (Agent): {overall_severity_from_agent}, Frontend Block ID: {frontend_block_id}")


    # --- Alarm State Management for Frontend Notifications ---
    # Construct a unique field for Redis state tracking for this specific device
    device_state_field = f"{frontend_block_id}_{device_ip}"

    # Fetch last known alarm state from Redis Hash using the unique device field
    last_state_json = r.hget(PGM_ALARM_STATES_PER_DEVICE_KEY, device_state_field)
    last_state = json.loads(last_state_json.decode('utf-8')) if last_state_json else None

    # Determine if the current status constitutes an "alarming" state (not OK/INFO)
    alarming_severities = ['CRITICAL', 'ERROR', 'WARNING', 'ALARM', 'MAJOR', 'MINOR']
    
    is_current_alarm_state = overall_severity_from_agent in alarming_severities
    is_previous_alarm_state = False
    if last_state:
        is_previous_alarm_state = last_state.get('severity', 'UNKNOWN').upper() in alarming_severities

    logging.debug(f"Block state for {device_state_field}: Previous Alarm='{is_previous_alarm_state}', Current Alarm='{is_current_alarm_state}'")
    logging.debug(f"  Previous State: {last_state}")


    send_ws_notification = False
    ws_severity = overall_severity_from_agent
    ws_status = overall_status_from_agent
    ws_message = overall_message_from_agent
    ws_type = "UNKNOWN" # Default type

    # Scenario 1: Alarm clears (was alarming, now OK/INFO)
    if not is_current_alarm_state:
        if is_previous_alarm_state:
            # Block was previously in an alarm state and is now no longer alarming -> Send CLEAR notification
            send_ws_notification = True
            ws_severity = "INFO" # Represent as INFO for cleared in frontend
            ws_status = "CLEARED" # Specific status for cleared alarms
            ws_message = f"PGM Routing for {device_name} (Block: {frontend_block_id}, IP: {device_ip}) is now OK. All previous alarms cleared."
            ws_type = "ALARM_CLEARED"
            logging.info(f"PGM block {device_state_field} has CLEARED. Previous severity: {last_state.get('severity')}. Sending WS notification.")
            r.hdel(PGM_ALARM_STATES_PER_DEVICE_KEY, device_state_field) # Remove state from Redis
        else:
            logging.debug(f"PGM block {device_state_field} is OK/INFO and no previous alarm. Skipping WS notification.")
            # No need to update state if it's already OK and remains OK, or was never an alarm
            
    # Scenario 2: New alarm or existing alarm changed severity/message
    elif is_current_alarm_state:
        if not is_previous_alarm_state:
            # Transitioned from non-alarming to alarming -> New alarm
            send_ws_notification = True
            ws_type = "NEW_ALARM"
            logging.info(f"NEW ALARM detected for {device_state_field}: {overall_severity_from_agent} - {overall_message_from_agent}. Sending WS notification.")
        elif (overall_severity_from_agent != last_state.get('severity').upper() or 
              overall_message_from_agent != last_state.get('message')):
            # Existing alarm changed severity or message -> Update alarm
            send_ws_notification = True
            ws_type = "ALARM_UPDATE"
            logging.info(f"ALARM UPDATED for {device_state_field}. Previous severity: {last_state.get('severity')}, Current: {overall_severity_from_agent}. Sending WS notification.")
        else:
            logging.debug(f"PGM block {device_state_field} is still {overall_severity_from_agent}. Severity/Message not changed. Skipping WS notification.")
        
        # Always update state for active alarms
        r.hset(PGM_ALARM_STATES_PER_DEVICE_KEY, device_state_field, json.dumps({
            "status": overall_status_from_agent,
            "severity": overall_severity_from_agent,
            "message": overall_message_from_agent
        }))


    # --- WebSocket Notification ---
    if send_ws_notification:
        group_id = None
        # Determine group_id based on group_name for WebSocket notification (similar to iLO consumer)
        if group_name == "main": # Assuming agent sends 'main' or 'backup' as group_name for domain
            group_id = "G.PGM_MAIN"
        elif group_name == "backup":
            group_id = "G.PGM_BACKUP"
        
        websocket_message = {
            "device_name": device_name,
            "ip": device_ip,
            "time": timestamp,
            "message": ws_message, # Use the message determined by state logic
            "severity": ws_severity, # Use the severity determined by state logic
            "status": ws_status, # Use the status determined by state logic (e.g., "cleared", "mismatch", "error")
            "frontend_block_id": frontend_block_id, # Always include frontend_block_id
            "group_name": group_name,
            "group_id": group_id, # Add group_id
            "type": ws_type, # Type of notification (NEW_ALARM, ALARM_UPDATE, ALARM_CLEARED)
            "pgm_dest": pgm_dest_check.get('pgm_dest', 'N/A'),
            "polled_value": polled_source,
            "expected_value": expected_source,
            "router_source": source_name, # Send source_name as router_source to frontend
            # OID IS EXPLICITLY REMOVED FROM WEBSOCKET MESSAGE
        }
        await send_websocket_notification(websocket_message)


    # --- Index to monitor_historical_alarms ---
    # Only index to Elasticsearch if the overall status is an active alarm.
    if is_current_alarm_state: 
        # Construct alarm message including polled, expected, and source_name values
        alarm_doc_message = overall_message_from_agent # Start with agent's overall message

        # Enhance message based on details, but exclude OID
        if pgm_dest_check:
            if pgm_dest_check.get("status") == "MISMATCH":
                alarm_doc_message = (
                    f"PGM Routing MISMATCH - Destination: {pgm_dest_check.get('pgm_dest', 'N/A')}, "
                    f"Current: '{polled_source}', Expected: '{expected_source}'"
                )
                if source_name and source_name != 'N/A':
                    alarm_doc_message += f", Source Name: '{source_name}'"
            elif pgm_dest_check.get("status") == "Error": # SNMP polling error
                alarm_doc_message = (
                    f"PGM Routing SNMP Error - Dest: {pgm_dest_check.get('pgm_dest', 'N/A')}, "
                    f"Message: {pgm_dest_check.get('message', 'N/A')}"
                )

        alarm_doc = {
            "alarm_id": str(uuid.uuid4()),
            "timestamp": timestamp,
            "message": alarm_doc_message, # Use the detailed message for ES
            "device_name": device_name,
            "block_id": frontend_block_id,
            "severity": overall_severity_from_agent, 
            "status": overall_status_from_agent,
            "type": agent_type,
            "device_ip": device_ip,
            "group_name": group_name,
            "pgm_dest": pgm_dest_check.get('pgm_dest', 'N/A'),
            "polled_source": polled_source,
            "expected_source": expected_source,
        }
        if source_name and source_name != 'N/A':
            alarm_doc["source_name"] = source_name
        # OID IS EXPLICITLY REMOVED FROM ELASTICSEARCH MESSAGE


        try:
            es.index(index="monitor_historical_alarms", document=alarm_doc)
            logging.info(f"Indexed PGM routing alarm/error '{alarm_doc['alarm_id']}' for {device_ip} (Block: {frontend_block_id}) to Elasticsearch.")
        except Exception as e:
            logging.error(f"Failed to index PGM routing alarm/error to Elasticsearch for {device_ip} (Block: {frontend_block_id}): {e}", exc_info=True)
    else:
        logging.info(f"Skipping Elasticsearch indexing for {device_ip} (Block: {frontend_block_id}) as status is 'ok' or non-alarming.")


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
