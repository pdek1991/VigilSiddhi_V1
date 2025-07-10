import redis
import json
import time
import logging
import os
from elasticsearch import AsyncElasticsearch
from datetime import datetime
import asyncio
import aiohttp
import uuid
from elasticsearch.helpers import async_bulk

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- Configuration ---
REDIS_HOST = os.environ.get('REDIS_HOST', '192.168.56.30')
REDIS_PORT = int(os.environ.get('REDIS_PORT', 6379))

ES_HOST = os.environ.get('ES_HOST', '192.168.56.30')
ES_PORT = int(os.environ.get('ES_PORT', 9200))

WEBSOCKET_NOTIFIER_URL = os.environ.get('WEBSOCKET_NOTIFIER_URL', 'http://127.0.0.1:8001/notify')

REDIS_STREAM_NAME = "vs:agent:switch_status"
CONSUMER_GROUP_NAME = "es_ingester_switch"
CONSUMER_NAME = "switch_config_consumer_instance_1"

# Redis Hash name for storing switch block states (full state per unique device)
SWITCH_STATUS_STATES_PER_DEVICE_KEY = "switch_status_states_per_device"

# Initialize Redis client
r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=0, decode_responses=False)

# Initialize Elasticsearch client - Now using AsyncElasticsearch
es = AsyncElasticsearch(f"http://{ES_HOST}:{ES_PORT}")

# Async HTTP session for WebSocket notifier communication
http_session = None

async def send_websocket_notification(message_data):
    """Sends a JSON message to the WebSocket notifier's HTTP endpoint."""
    global http_session
    if http_session is None or http_session.closed:
        http_session = aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=False))

    try:
        async with http_session.post(WEBSOCKET_NOTIFIER_URL, json=message_data) as response:
            if response.status != 200:
                logging.error(f"Failed to send WebSocket notification: {response.status} - {await response.text()}")
            else:
                logging.info(f"WebSocket notification sent successfully for {message_data.get('hostname')}.")
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
    
    # Extract relevant data from payload
    timestamp = payload.get('timestamp')
    agent_type = payload.get('agent_type')
    switch_ip = payload.get('switch_ip')
    hostname = payload.get('hostname')
    model = payload.get('model')
    cpu_utilization = payload.get('cpu_utilization')
    memory_utilization = payload.get('memory_utilization')
    uptime = payload.get('uptime')
    interfaces = payload.get('interfaces', [])
    overall_interface_status = payload.get('overall_interface_status')
    problem_interfaces = payload.get('problem_interfaces', [])

    # --- State Management for Frontend Notifications ---
    device_state_field = f"{hostname}_{switch_ip}" 

    last_state_json = r.hget(SWITCH_STATUS_STATES_PER_DEVICE_KEY, device_state_field)
    last_state = json.loads(last_state_json.decode('utf-8')) if last_state_json else None

    send_ws_notification = False
    ws_message_type = "UPDATE" # Default type

    # Check if status has changed or if there are new problem interfaces
    if not last_state or \
       last_state.get('overall_interface_status') != overall_interface_status or \
       set(last_state.get('problem_interfaces', [])) != set(problem_interfaces):
        
        send_ws_notification = True
        if not last_state:
            ws_message_type = "INITIAL_LOAD"
        elif overall_interface_status == "OK" and last_state.get('overall_interface_status') == "Problem":
            ws_message_type = "STATUS_CLEARED"
            logging.info(f"Switch {hostname} ({switch_ip}) status CLEARED. Sending WS notification.")
        elif overall_interface_status == "Problem" and last_state.get('overall_interface_status') == "OK":
            ws_message_type = "NEW_PROBLEM"
            logging.info(f"Switch {hostname} ({switch_ip}) has NEW PROBLEM. Sending WS notification.")
        else:
            ws_message_type = "STATUS_UPDATE"
            logging.info(f"Switch {hostname} ({switch_ip}) status UPDATED. Sending WS notification.")

        # Update state in Redis
        r.hset(SWITCH_STATUS_STATES_PER_DEVICE_KEY, device_state_field, json.dumps(payload))
    else:
        logging.debug(f"Switch {hostname} ({switch_ip}) status unchanged. Skipping WS notification.")

    # --- WebSocket Notification ---
    if send_ws_notification:
        websocket_message = {
            "timestamp": timestamp,
            "hostname": hostname,
            "switch_ip": switch_ip,
            "cpu_utilization": cpu_utilization,
            "memory_utilization": memory_utilization,
            "uptime": uptime,
            "overall_interface_status": overall_interface_status,
            "problem_interfaces": problem_interfaces,
            "type": ws_message_type # Indicate the type of update
        }
        await send_websocket_notification(websocket_message)

    # --- Index to Elasticsearch ---
    # Create a document for the overall switch status
    overall_switch_doc = {
        "_id": f"switch_status_{hostname}_{datetime.now().strftime('%Y%m%d%H%M%S%f')}", # Unique ID for each poll
        "_index": "monitor_switch_status",
        "timestamp": timestamp,
        "agent_type": agent_type,
        "switch_ip": switch_ip,
        "hostname": hostname,
        "model": model,
        "cpu_utilization": cpu_utilization,
        "memory_utilization": memory_utilization,
        "uptime": uptime,
        "overall_interface_status": overall_interface_status,
        "problem_interfaces": problem_interfaces,
        # Store detailed interfaces as a nested object/array in ES if needed for historical analysis
        "interfaces_details": interfaces 
    }

    try:
        await es.index(index="monitor_switch_status", document=overall_switch_doc)
        logging.info(f"Indexed overall switch status for {hostname} to Elasticsearch.")
    except Exception as e:
        logging.error(f"Failed to index overall switch status for {hostname} to Elasticsearch: {e}", exc_info=True)


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
                processing_tasks = []
                for stream, message_list in messages:
                    for message_id, message_data in message_list:
                        try:
                            payload = json.loads(message_data[b'data'].decode('utf-8'))
                            processing_tasks.append(process_message(message_id, payload))
                        except json.JSONDecodeError as jde:
                            logging.error(f"JSON Decode Error for message {message_id.decode()} in {stream.decode()}: {jde}")
                            r.xack(stream, CONSUMER_GROUP_NAME, message_id)
                        except Exception as e:
                            logging.error(f"Error preparing message {message_id.decode()} for processing: {e}", exc_info=True)
                            r.xack(stream, CONSUMER_GROUP_NAME, message_id)

                await asyncio.gather(*processing_tasks)

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
        logging.info("Switch Consumer stopped by user.")
    finally:
        if http_session:
            asyncio.run(http_session.close())
        if es:
           asyncio.run(es.close())
