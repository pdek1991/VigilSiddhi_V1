import redis
import json
import time
import logging
import os
from elasticsearch import Elasticsearch
from datetime import datetime
import asyncio
import aiohttp # For async HTTP client to send messages to WebSocket notifier
import uuid # For generating unique IDs for alarms

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

# Initialize Redis client
r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=0, decode_responses=False)

# Initialize Elasticsearch client
es = Elasticsearch(f"http://{ES_HOST}:{ES_PORT}")

# Async HTTP session for WebSocket notifier communication
http_session = None

async def send_websocket_notification(message_data):
    """Sends a JSON message to the WebSocket notifier's HTTP endpoint."""
    global http_session
    if http_session is None:
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
    logging.info(f"  Timestamp: {payload.get('timestamp')}")
    logging.info(f"  Agent Type: {payload.get('agent_type')}")
    logging.info(f"  Device IP: {payload.get('device_ip')}")
    logging.info(f"  Device Name: {payload.get('device_name')}")
    logging.info(f"  Frontend Block ID: {payload.get('frontend_block_id')}")
    logging.info(f"  Group Name: {payload.get('group_name')}")
    logging.info(f"  Status: {payload.get('status')}")
    logging.info(f"  Severity: {payload.get('severity')}")
    logging.info(f"  Message: {payload.get('message')}")
    logging.info("-" * 50)

    # --- Index to monitor_historical_alarms ---
    # Only index to Elasticsearch if the status is NOT 'ok' (i.e., it's an alarm, error, or warning)
    current_status = payload.get('status')
    if current_status != 'ok':
        # The 'details' field in the payload contains the 'active_alarms' list from the agent.
        # Each item in 'active_alarms' represents a specific alarm instance.
        active_alarms = payload.get('details', {}).get('active_alarms', [])

        if active_alarms:
            logging.info(f"Found {len(active_alarms)} active alarms to index for {payload.get('device_ip')}.")
            for alarm_data in active_alarms:
                # Construct an alarm document for each individual alarm found by the agent
                alarm_doc = {
                    "alarm_id": str(uuid.uuid4()), # Generate a unique ID for each alarm instance
                    "timestamp": payload.get('timestamp'),
                    "message": f"KMX Alarm - Device: {alarm_data.get('deviceName', 'N/A')}, Value: {alarm_data.get('actualValue', 'N/A')}",
                    "device_name": payload.get('device_name'), # Overall device name from agent payload
                    "block_id": payload.get('frontend_block_id'),
                    "severity": alarm_data.get('severity', 'UNKNOWN').upper(), # Use the alarm's specific severity
                    "type": payload.get('agent_type'), # Using agent_type for the 'type' field in ES
                    "device_ip": payload.get('device_ip'),
                    "group_name": payload.get('group_name')
                }
                try:
                    es.index(index="monitor_historical_alarms", document=alarm_doc)
                    logging.info(f"Indexed historical alarm '{alarm_doc['alarm_id']}' for {payload.get('device_ip')} to Elasticsearch.")
                except Exception as e:
                    logging.error(f"Failed to index historical alarm to Elasticsearch: {e}", exc_info=True)
        else:
            # If overall status is not 'ok' but no specific 'active_alarms' details,
            # it might be an API error or a general status message. Index the main message.
            if payload.get('api_errors'):
                alarm_doc = {
                    "alarm_id": str(uuid.uuid4()),
                    "timestamp": payload.get('timestamp'),
                    "message": payload.get('message'),
                    "device_name": payload.get('device_name'),
                    "block_id": payload.get('frontend_block_id'),
                    "severity": payload.get('severity').upper(),
                    "type": payload.get('agent_type'),
                    "device_ip": payload.get('device_ip'),
                    "group_name": payload.get('group_name')
                }
                try:
                    es.index(index="monitor_historical_alarms", document=alarm_doc)
                    logging.info(f"Indexed API error/general status for {payload.get('device_ip')} to Elasticsearch.")
                except Exception as e:
                    logging.error(f"Failed to index API error/general status to Elasticsearch: {e}", exc_info=True)
            else:
                 logging.info(f"No specific active alarms or API errors found in details for {payload.get('device_ip')} despite non-'ok' status.")

    else:
        logging.info(f"Skipping Elasticsearch indexing for {payload.get('device_ip')} as status is 'ok'.")


    # --- WebSocket Notification ---
    # Only send notifications for alarm or error/warning statuses
    if current_status in ["alarm", "error", "warning"]:
        websocket_message = {
            "device_name": payload.get('device_name'),
            "ip": payload.get('device_ip'),
            "time": payload.get('timestamp'),
            "message": payload.get('message'), # Send the overall message from the agent
            "severity": payload.get('severity') # Send the overall severity
        }
        await send_websocket_notification(websocket_message)
    else:
        logging.info(f"Skipping WebSocket notification for {payload.get('device_ip')} as status is '{current_status}'.")


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
                block=5000 # Block for 5000 milliseconds (5 seconds) if no messages
            )

            if messages:
                for stream, message_list in messages:
                    for message_id, message_data in message_list:
                        try:
                            # Decode the 'data' field which holds the JSON payload
                            payload = json.loads(message_data[b'data'].decode('utf-8'))
                            await process_message(message_id, payload)

                            # Acknowledge the message after successful processing
                            r.xack(stream, CONSUMER_GROUP_NAME, message_id)
                            logging.debug(f"Acknowledged message: {message_id}")

                        except json.JSONDecodeError as jde:
                            logging.error(f"JSON Decode Error for message {message_id.decode()} in {stream.decode()}: {jde}")
                            r.xack(stream, CONSUMER_GROUP_NAME, message_id) # Acknowledge bad message to prevent re-processing
                        except Exception as e:
                            logging.error(f"Error processing message {message_id.decode()} from {stream.decode()}: {e}", exc_info=True)
                            r.xack(stream, CONSUMER_GROUP_NAME, message_id) # Acknowledge even on error to move on

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
