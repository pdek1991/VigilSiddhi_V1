import redis
import json
import time
import logging
import os
from elasticsearch import AsyncElasticsearch # CHANGED: Import AsyncElasticsearch
from datetime import datetime
import asyncio
import aiohttp # For async HTTP client to send messages to WebSocket notifier
import uuid # For generating unique IDs for alarms
from elasticsearch.helpers import async_bulk # For bulk indexing

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- Configuration ---
REDIS_HOST = os.environ.get('REDIS_HOST', '192.168.56.30')
REDIS_PORT = int(os.environ.get('REDIS_PORT', 6379))

ES_HOST = os.environ.get('ES_HOST', '192.168.56.30')
ES_PORT = int(os.environ.get('ES_PORT', 9200))

WEBSOCKET_NOTIFIER_URL = os.environ.get('WEBSOCKET_NOTIFIER_URL', 'http://127.0.0.1:8001/notify') # URL for the WebSocket notifier HTTP endpoint

# Using the Redis stream name specified by the user
REDIS_STREAM_NAME = "vs:agent:kmx_status"
# Choosing one of the consumer group names provided by the user
CONSUMER_GROUP_NAME = "es_ingester" 
CONSUMER_NAME = "kmx_config_consumer_instance_1" # Unique name for this consumer instance

# Redis Hash name for storing KMX block states (full state per unique device)
# Key: 'kmx_alarm_states_per_device' (Redis Hash name)
# Field: 'frontend_block_id' (e.g., 'KMX_CONFIG_192_168_56_10')
# Value: JSON string of {"status": "ALARM", "severity": "CRITICAL", "message": "..."}
KMX_ALARM_STATES_PER_DEVICE_KEY = "kmx_alarm_states_per_device"

# Initialize Redis client
r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=0, decode_responses=False)

# Initialize Elasticsearch client - Now using AsyncElasticsearch
es = AsyncElasticsearch(f"http://{ES_HOST}:{ES_PORT}") # CHANGED: Use AsyncElasticsearch

# Async HTTP session for WebSocket notifier communication
http_session = None

async def send_websocket_notification(message_data):
    """Sends a JSON message to the WebSocket notifier's HTTP endpoint."""
    global http_session
    if http_session is None or http_session.closed: # Re-initialize if closed
        # Explicitly set ssl=False for the connector as the notifier is HTTP
        http_session = aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=False))

    try:
        async with http_session.post(WEBSOCKET_NOTIFIER_URL, json=message_data) as response:
            if response.status != 200:
                logging.error(f"Failed to send WebSocket notification: {response.status} - {await response.text()}")
            else:
                logging.info(f"WebSocket notification sent successfully for {message_data.get('ip')}.")
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
    logging.info(f"Processing message ID: {message_id.decode()}")
    logging.debug(f"  Payload: {payload}") # Changed to debug for less verbosity

    device_ip = payload.get('device_ip')
    frontend_block_id = payload.get('frontend_block_id')
    
    # These are the *actual* status/severity/message from the agent's poll
    overall_status_from_agent = payload.get('status', 'UNKNOWN').upper() 
    overall_severity_from_agent = payload.get('severity', 'UNKNOWN').upper() 
    overall_message_from_agent = payload.get('message', 'No specific status message.')
    timestamp = payload.get('timestamp')
    agent_type = payload.get('agent_type')
    group_name = payload.get('group_name')
    device_name = payload.get('device_name')

    # --- Alarm State Management for Frontend Notifications ---
    # Construct a unique field for Redis state tracking for this specific device
    # This combines frontend_block_id (e.g., "G.KMX") with device_ip for uniqueness
    device_state_field = f"{frontend_block_id}_{device_ip}" 

    # Fetch last known alarm state from Redis Hash using the unique device field
    last_state_json = r.hget(KMX_ALARM_STATES_PER_DEVICE_KEY, device_state_field)
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
    ws_message = overall_message_from_agent # Default to agent's message
    ws_type = "UNKNOWN" # Default type

    # Scenario 1: Alarm clears (was alarming, now OK/INFO)
    if not is_current_alarm_state: # Current status is 'ok' or 'info'
        if is_previous_alarm_state:
            # Block was previously in an alarm state and is now no longer alarming -> Send CLEAR notification
            send_ws_notification = True
            ws_severity = "INFO" # Represent as INFO for cleared in frontend
            ws_status = "CLEARED" # Specific status for cleared alarms
            ws_message = f"KMX device {device_name} (IP: {device_ip}) is now OK. All previous alarms cleared."
            ws_type = "ALARM_CLEARED"
            logging.info(f"KMX block {device_state_field} has CLEARED. Previous severity: {last_state.get('severity')}. Sending WS notification.")
            r.hdel(KMX_ALARM_STATES_PER_DEVICE_KEY, device_state_field) # Remove state from Redis
        else:
            logging.debug(f"KMX block {device_state_field} is OK/INFO and no previous alarm. Skipping WS notification.")
            # No need to update state if it's already OK and remains OK, or was never an alarm
            
    # Scenario 2: New alarm or existing alarm changed severity/message
    elif is_current_alarm_state: # Current status is alarming
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
            logging.debug(f"KMX block {device_state_field} is still {overall_severity_from_agent}. Severity/Message not changed. Skipping WS notification.")
        
        # Always update state for active alarms
        r.hset(KMX_ALARM_STATES_PER_DEVICE_KEY, device_state_field, json.dumps({
            "status": overall_status_from_agent,
            "severity": overall_severity_from_agent,
            "message": overall_message_from_agent
        }))


    # --- WebSocket Notification ---
    if send_ws_notification:
        websocket_message = {
            "device_name": device_name,
            "ip": device_ip,
            "time": timestamp,
            "message": ws_message, # Uses the cleaned message from agent
            "severity": ws_severity, # Uses the severity determined by state logic (CRITICAL, INFO for cleared, etc.)
            "status": ws_status, # Uses the status determined by state logic (ALARM, CLEARED, etc.)
            "frontend_block_id": frontend_block_id, # Always include frontend_block_id
            "group_name": group_name,
            "type": ws_type, # Type of notification (NEW_ALARM, ALARM_UPDATE, ALARM_CLEARED)
            # OID IS EXPLICITLY REMOVED FROM WEBSOCKET MESSAGE
        }
        await send_websocket_notification(websocket_message)
    else:
        logging.info(f"Skipping WebSocket notification for {device_ip} as status is '{overall_status_from_agent}' and no state change.")


    # --- Index to monitor_historical_alarms ---
    # Only index to Elasticsearch if the status is NOT 'ok' (i.e., it's an alarm, error, or warning)
    if overall_status_from_agent != 'OK': # Check the original status from the agent payload
        active_alarms = payload.get('details', {}).get('active_alarms', [])
        api_errors = payload.get('details', {}).get('api_errors', [])
        
        es_actions = [] # List to store actions for bulk indexing

        if active_alarms:
            logging.info(f"Found {len(active_alarms)} active alarms to index for {device_ip}.")
            for alarm_data in active_alarms:
                # Construct an alarm document for each individual alarm found by the agent
                alarm_doc = {
                    "_id": str(uuid.uuid4()), # Use _id for explicit document ID in bulk indexing
                    "_index": "monitor_historical_alarms", # Specify index for bulk
                    "timestamp": timestamp,
                    # Clean the message: Remove "KMX Alarm -" and "Severity: ..."
                    "message": f"Device: {alarm_data.get('deviceName', 'N/A')}, Value: {alarm_data.get('actualValue', 'N/A')}", 
                    "device_name": device_name, # Overall device name from agent payload
                    "block_id": frontend_block_id,
                    "severity": alarm_data.get('severity', 'UNKNOWN').upper(), # Use the alarm's specific severity
                    "type": agent_type, # Using agent_type for the 'type' field in ES
                    "device_ip": device_ip,
                    "group_name": group_name
                    # OID IS EXPLICITLY REMOVED FROM ELASTICSEARCH MESSAGE
                }
                es_actions.append(alarm_doc)
        elif api_errors:
            # If overall status is not 'ok' due to API errors, index the main message.
            alarm_doc = {
                "_id": str(uuid.uuid4()),
                "_index": "monitor_historical_alarms",
                "timestamp": timestamp,
                "message": overall_message_from_agent,
                "device_name": device_name,
                "block_id": frontend_block_id,
                "severity": overall_severity_from_agent,
                "type": agent_type,
                "device_ip": device_ip,
                "group_name": group_name
            }
            es_actions.append(alarm_doc)
        else:
            logging.info(f"No specific active alarms or API errors found in details for {device_ip} despite non-'ok' status. Skipping specific alarm indexing.")
        
        if es_actions:
            try:
                # Perform bulk indexing for all collected alarm documents
                await async_bulk(es, es_actions) # `es` is now AsyncElasticsearch, so await is correct.
                logging.info(f"Indexed {len(es_actions)} historical alarms/errors for {device_ip} to Elasticsearch via bulk API.")
            except Exception as e:
                logging.error(f"Failed to bulk index historical alarms/errors to Elasticsearch: {e}", exc_info=True)
    else:
        logging.info(f"Skipping Elasticsearch indexing for {device_ip} as status is 'ok'.")


async def consume_messages():
    """Main function to consume messages from Redis Stream."""
    logging.info(f"Starting Redis consumer for stream '{REDIS_STREAM_NAME}' with group '{CONSUMER_GROUP_NAME}'.")
    while True:
        try:
            messages = r.xreadgroup(
                CONSUMER_GROUP_NAME,
                CONSUMER_NAME,
                {REDIS_STREAM_NAME: '>'},
                count=10, # Fetch up to 10 messages at once
                block=5000 # Block for 5000 milliseconds (5 seconds) if no messages
            )

            if messages:
                # Create a list of tasks for processing messages concurrently
                processing_tasks = []
                for stream, message_list in messages:
                    for message_id, message_data in message_list:
                        try:
                            payload = json.loads(message_data[b'data'].decode('utf-8'))
                            processing_tasks.append(process_message(message_id, payload))
                        except json.JSONDecodeError as jde:
                            logging.error(f"JSON Decode Error for message {message_id.decode()} in {stream.decode()}: {jde}")
                            r.xack(stream, CONSUMER_GROUP_NAME, message_id) # Acknowledge bad message to prevent re-processing
                        except Exception as e:
                            logging.error(f"Error preparing message {message_id.decode()} for processing: {e}", exc_info=True)
                            r.xack(stream, CONSUMER_GROUP_NAME, message_id) # Acknowledge bad message to prevent re-processing

                # Run all message processing tasks concurrently
                await asyncio.gather(*processing_tasks)

                # Acknowledge all processed messages after they are done
                # This could be batched for even more efficiency if needed, but per-message ACK is safer
                for stream, message_list in messages:
                    for message_id, message_data in message_list:
                         r.xack(stream, CONSUMER_GROUP_NAME, message_id)
                         logging.debug(f"Acknowledged message: {message_id}")
            else:
                logging.debug("No new messages from Redis stream.")

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
            asyncio.run(http_session.close()) # Close aiohttp session properly
        if es: # Ensure the AsyncElasticsearch client is closed
           asyncio.run(es.close())
