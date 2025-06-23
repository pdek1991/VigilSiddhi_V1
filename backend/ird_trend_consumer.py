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
import re # Import regex for system_id parsing

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- Configuration ---
REDIS_HOST = os.environ.get('REDIS_HOST', '192.168.56.30')
REDIS_PORT = int(os.environ.get('REDIS_PORT', 6379))

ES_HOST = os.environ.get('ES_HOST', '192.168.56.30')
ES_PORT = int(os.environ.get('ES_PORT', 9200))

WEBSOCKET_NOTIFIER_URL = os.environ.get('WEBSOCKET_NOTIFIER_URL', 'http://127.0.0.1:8001/notify') # URL for the WebSocket notifier HTTP endpoint

REDIS_STREAM_NAME = "vs:agent:ird_trend_status"
CONSUMER_GROUP_NAME = "ird_trend_test_group"
CONSUMER_NAME = "ird_trend_consumer_instance_1" # Unique name for this consumer instance

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
        # This connector will be reused across requests
        http_session = aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=False))

    # Log the URL being used to ensure it's http and not https
    logging.debug(f"Attempting to send WebSocket notification to: {WEBSOCKET_NOTIFIER_URL}")

    try:
        async with http_session.post(WEBSOCKET_NOTIFIER_URL, json=message_data) as response:
            if response.status != 200:
                logging.error(f"Failed to send WebSocket notification: {response.status} - {await response.text()}")
    except aiohttp.ClientConnectionError as e:
        logging.error(f"WebSocket notifier connection error: {e}. Is the notifier running and accessible at {WEBSOCKET_NOTIFIER_URL}?")
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
    """Processes a single message from the Redis stream and indexes to ES."""
    logging.info(f"Processing message ID: {message_id.decode()}")
    logging.info(f"  Timestamp: {payload.get('timestamp')}")
    logging.info(f"  Agent Type: {payload.get('agent_type')}")
    logging.info(f"  Device IP: {payload.get('device_ip')}")
    logging.info(f"  Device Name: {payload.get('device_name')}")
    logging.info(f"  Frontend Block ID: {payload.get('frontend_block_id')}")
    logging.info(f"  Channel Name: {payload.get('Channel_Name')}")
    logging.info(f"  Status: {payload.get('status')}")
    logging.info(f"  Severity: {payload.get('severity')}")
    logging.info(f"  Message: {payload.get('message')}")
    # logging.info(f"  Trend Details: {json.dumps(payload.get('details', {}), indent=2)}") # Commented out for brevity in logs
    logging.info("=" * 50)

    # --- Index to ird_trend_data ---
    details = payload.get('details', {})
    current_status = payload.get('status') # Get current status for indexing condition
    
    # Robustly parse system_id
    system_id_raw = details.get('system_id')
    system_id_int = None
    if system_id_raw is not None:
        try:
            system_id_str = str(system_id_raw).strip()
            if system_id_str.isdigit():
                system_id_int = int(system_id_str)
            elif system_id_str.startswith('System') and system_id_str[6:].isdigit():
                system_id_int = int(system_id_str[6:]) # Extract number from "System1"
            else:
                logging.warning(f"Could not parse system_id as integer: '{system_id_str}' for {payload.get('device_ip')}")
        except Exception as e:
            logging.warning(f"Error converting system_id '{system_id_raw}' to int for {payload.get('device_ip')}: {e}")

    trend_doc = {
        "channel_name": payload.get('Channel_Name'),
        "system_id": system_id_int, # Use the parsed integer system_id
        "C_N_margin": float(details.get('C_N_margin')) if details.get('C_N_margin') is not None else None,
        "signal_strength": float(details.get('signal_strength')) if details.get('signal_strength') is not None else None,
        "timestamp": payload.get('timestamp'),
        "ip_address": payload.get('device_ip')
    }
    
    # Remove None values to avoid potential ES mapping issues with null floats/integers if not explicitly allowed
    trend_doc = {k: v for k, v in trend_doc.items() if v is not None}

    # Only index to Elasticsearch if the status is NOT 'ok'
    if current_status != 'ok':
        try:
            es.index(index="ird_trend_data", document=trend_doc)
            logging.info(f"Indexed trend data for {payload.get('device_ip')} (Status: {current_status}) to Elasticsearch.")
        except Exception as e:
            logging.error(f"Failed to index trend data to Elasticsearch: {e}", exc_info=True)
    else:
        logging.info(f"Skipping Elasticsearch indexing for {payload.get('device_ip')} as status is 'ok'.")

    # --- WebSocket Notification ---
    # Only send notifications for alarm or error statuses
    # The condition below already filters out 'ok' status messages
    if current_status in ["alarm", "error"]:
        websocket_message = {
            "device_name": payload.get('device_name'),
            "ip": payload.get('device_ip'),
            "time": payload.get('timestamp'),
            "message": payload.get('message'),
            "severity": payload.get('severity')
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
        # Ensure the aiohttp session is closed properly on exit
        if http_session:
            asyncio.run(http_session.close())
