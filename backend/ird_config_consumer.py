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

REDIS_STREAM_NAME = "vs:agent:ird_config_status"
CONSUMER_GROUP_NAME = "ird_config_test_group"
CONSUMER_NAME = "ird_config_consumer_instance_1" # Unique name for this consumer instance

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
    # logging.info(f"  Details: {json.dumps(payload.get('details', {}), indent=2)}") # Commented out for brevity in logs
    logging.info("-" * 50)

    # --- Index to monitor_historical_alarms ---
    # Only index to Elasticsearch if the status is NOT 'ok'
    current_status = payload.get('status')
    if current_status != 'ok':
        alarm_doc = {
            "alarm_id": str(uuid.uuid4()), # Generate a unique ID for each alarm instance
            "timestamp": payload.get('timestamp'),
            "message": payload.get('message'),
            "device_name": payload.get('device_name'),
            "block_id": payload.get('frontend_block_id'),
            "severity": payload.get('severity'),
            "type": payload.get('agent_type'), # Using agent_type for the 'type' field in ES
            "device_ip": payload.get('device_ip'),
            "group_name": payload.get('group_name')
        }
        try:
            es.index(index="monitor_historical_alarms", document=alarm_doc)
            logging.info(f"Indexed historical alarm for {payload.get('device_ip')} (Status: {current_status}) to Elasticsearch.")
        except Exception as e:
            logging.error(f"Failed to index historical alarm to Elasticsearch: {e}", exc_info=True)
    else:
        logging.info(f"Skipping Elasticsearch indexing for {payload.get('device_ip')} as status is 'ok'.")


    # --- Upsert to ird_configurations (This should always update with latest config regardless of status) ---
    ird_ip = payload.get('device_ip')
    details = payload.get('details', {})
    rf_status = details.get('rf_status', [])
    moip_output_status = details.get('moip_output_status', {})
    asi_status = details.get('asi_status', []) # Get ASI status details
    
    # Extract representative RF metrics (from first active RF input)
    system_id_int = None
    freq = None
    SR = None
    Pol = None
    C_N = None
    signal_strength = None
    input_bitrate = None

    if rf_status and rf_status[0].get('lock_status') == 'Lock+Sig':
        rf_data = rf_status[0]
        try:
            system_id_str = str(rf_data.get('system_id')).strip()
            if system_id_str.isdigit():
                system_id_int = int(system_id_str)
            elif system_id_str.startswith('System') and system_id_str[6:].isdigit():
                system_id_int = int(system_id_str[6:])
            else:
                logging.warning(f"Could not parse system_id as integer: '{system_id_str}' for {ird_ip}")
        except Exception as e:
            logging.warning(f"Error converting system_id '{rf_data.get('system_id')}' to int for {ird_ip}: {e}")

        freq = float(rf_data.get('frequency_mhz'))
        SR = float(rf_data.get('symbol_rate'))
        Pol = rf_data.get('polarity')
        C_N = float(rf_data.get('cn_margin'))
        signal_strength = float(rf_data.get('input_level_dbm'))
        input_bitrate = float(rf_data.get('input_bw'))

    moip_status = "Locked" if moip_output_status.get('locked') else "Not Locked"
    output_bitrate = float(moip_output_status.get('ts1rate', 0.0))

    if input_bitrate is None and details.get('moip_input_status'):
        moip_input_list = details['moip_input_status']
        if moip_input_list:
            first_moip_input = moip_input_list[0]
            if first_moip_input.get('is_locked'):
                try:
                    input_bitrate = float(first_moip_input.get('inputrate', 0.0))
                except ValueError:
                    logging.warning(f"Could not convert MOIP inputrate to float for {ird_ip}")

    # --- Extract ASI status, bandwidth, and system_id ---
    asi_lock_status_for_es = None
    asi_input_bw_for_es = None
    asi_system_id_for_es = None

    if asi_status:
        # Assuming we take the status of the first ASI input if available
        first_asi_input = asi_status[0]
        asi_lock_status_for_es = first_asi_input.get('lock_status')
        # input_bw and system_id are now correctly fetched by ird_api_client
        asi_input_bw_for_es = float(first_asi_input.get('input_bw')) if first_asi_input.get('input_bw') is not None else None
        asi_system_id_for_es = first_asi_input.get('system_id')


    config_doc = {
        "ird_ip": ird_ip,
        "channel_name": payload.get('group_name'),
        "system_id": system_id_int,
        "freq": freq,
        "SR": SR,
        "Pol": Pol,
        "C_N": C_N,
        "signal_strength": signal_strength,
        "moip_status": moip_status,
        "output_bitrate": output_bitrate,
        "input_bitrate": input_bitrate,
        "asi_lock_status": asi_lock_status_for_es,
        "asi_input_bw": asi_input_bw_for_es,
        "asi_system_id": asi_system_id_for_es, # Added ASI system ID
        "last_updated": payload.get('timestamp')
    }

    config_doc = {k: v for k, v in config_doc.items() if v is not None}

    try:
        es.index(index="ird_configurations", id=ird_ip, document=config_doc)
        logging.info(f"Upserted IRD configuration for {ird_ip} to Elasticsearch.")
    except Exception as e:
        logging.error(f"Failed to upsert IRD configuration to Elasticsearch: {e}", exc_info=True)


    # --- WebSocket Notification ---
    # Only send notifications for alarm or error/warning statuses
    if current_status in ["alarm", "error", "warning"]:
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
