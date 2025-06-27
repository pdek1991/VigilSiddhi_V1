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

REDIS_STREAM_NAME = "vs:agent:pgm_routing_status" # Must match agent's stream name
CONSUMER_GROUP_NAME = "pgm_routing_es_ingester" # Unique group name for PGM routing consumer
CONSUMER_NAME = "pgm_routing_consumer_instance_1"

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

    current_status = payload.get('status')
    pgm_dest_check = payload.get('details', {}).get('pgm_dest_check', {})
    api_errors = payload.get('details', {}).get('api_errors', [])

    # Extract additional details for ES and WS
    polled_source = pgm_dest_check.get('polled_source', 'N/A')
    expected_source = pgm_dest_check.get('expected_source', 'N/A')
    source_name = pgm_dest_check.get('polled_source_name', 'N/A') # Renamed from polled_source_name to source_name

    # --- Index to monitor_historical_alarms ---
    # Only index to Elasticsearch if the overall status is NOT 'ok' AND there's a specific issue
    if current_status != 'ok':
        # Check if there's a specific routing mismatch or error
        if pgm_dest_check and pgm_dest_check.get("status") in ["MISMATCH", "Error"]:
            
            # Construct alarm message including polled, expected, and source_name values
            alarm_doc_message = (
                f"PGM Routing Alarm - Destination: {pgm_dest_check.get('pgm_dest', 'N/A')}, "
                f"Polled Source: '{polled_source}', Expected Source: '{expected_source}'"
            )
            if source_name and source_name != 'N/A':
                alarm_doc_message += f", Source Name: '{source_name}'"
            alarm_doc_message += f", OID: {pgm_dest_check.get('oid', 'N/A')}"
            
            # If the status from pgm_dest_check is "Error", it means SNMP polling itself had an issue
            if pgm_dest_check.get("status") == "Error":
                alarm_doc_message = (
                    f"PGM Routing SNMP Error - Dest: {pgm_dest_check.get('pgm_dest', 'N/A')}, "
                    f"Message: {pgm_dest_check.get('message', 'N/A')}, "
                    f"OID: {pgm_dest_check.get('oid', 'N/A')}"
                )

            alarm_doc = {
                "alarm_id": str(uuid.uuid4()),
                "timestamp": payload.get('timestamp'),
                "message": alarm_doc_message, # Use the detailed message for ES
                "device_name": payload.get('device_name'),
                "block_id": payload.get('frontend_block_id'), # Added frontend_block_id
                "severity": payload.get('severity').upper(),
                "type": payload.get('agent_type'),
                "device_ip": payload.get('device_ip'),
                "group_name": payload.get('group_name'),
                "pgm_dest": pgm_dest_check.get('pgm_dest', 'N/A'), # Add pgm_dest for filtering
                "polled_source": polled_source, # Added polled_source
                "expected_source": expected_source, # Added expected_source
            }
            if source_name and source_name != 'N/A':
                alarm_doc["source_name"] = source_name # Renamed from polled_source_name to source_name

            try:
                es.index(index="monitor_historical_alarms", document=alarm_doc)
                logging.info(f"Indexed PGM routing alarm/error '{alarm_doc['alarm_id']}' for {payload.get('device_ip')} to Elasticsearch.")
            except Exception as e:
                logging.error(f"Failed to index PGM routing alarm/error to Elasticsearch: {e}", exc_info=True)
        elif api_errors: # Index general API errors if present (e.g., if polling failed entirely)
            alarm_doc = {
                "alarm_id": str(uuid.uuid4()),
                "timestamp": payload.get('timestamp'),
                "message": payload.get('message'), # Use the main error message from agent
                "device_name": payload.get('device_name'),
                "block_id": payload.get('frontend_block_id'), # Added frontend_block_id
                "severity": payload.get('severity').upper(),
                "type": payload.get('agent_type'),
                "device_ip": payload.get('device_ip'),
                "group_name": payload.get('group_name')
            }
            try:
                es.index(index="monitor_historical_alarms", document=alarm_doc)
                logging.info(f"Indexed PGM routing API error for {payload.get('device_ip')} to Elasticsearch.")
            except Exception as e:
                logging.error(f"Failed to index PGM routing API error to Elasticsearch: {e}", exc_info=True)
        else:
            logging.info(f"No specific active alarms or API errors found in details for {payload.get('device_ip')} despite non-'ok' status. Skipping specific ES indexing.")
    else:
        logging.info(f"Skipping Elasticsearch indexing for {payload.get('device_ip')} as status is 'ok'.")

    # --- WebSocket Notification ---
    if current_status in ["alarm", "error", "warning", "mismatch"]:
        websocket_message = {
            "device_name": payload.get('device_name'),
            "ip": payload.get('device_ip'),
            "time": payload.get('timestamp'),
            "message": payload.get('message'), # Send the overall message from the agent
            "severity": payload.get('severity'), # Send the overall severity
            "frontend_block_id": payload.get('frontend_block_id'), # Added frontend_block_id
            "pgm_dest": pgm_dest_check.get('pgm_dest', 'N/A'), # Add pgm_dest for display
            "polled_value": polled_source, # Renamed to align with frontend
            "expected_value": expected_source, # Renamed to align with frontend
        }
        
        # If there's a source_name, add it as router_source to the WS message
        if source_name and source_name != 'N/A':
            websocket_message["router_source"] = source_name # ALIGNED: Sending as 'router_source' for frontend
            
            # The agent is now expected to put the "Polled Source Name" into the 'message' string.
            # So, we will not append it again here to avoid duplication.
            # The structured fields (polled_source, expected_source, source_name) are still
            # included separately for easier programmatic access in the frontend.

        await send_websocket_notification(websocket_message)
    else:
        log_message = f"Skipping WebSocket notification for {payload.get('device_ip')} as status is '{current_status}'"
        if current_status == "ok":
            log_message += " (matching)."
        logging.info(log_message)


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
