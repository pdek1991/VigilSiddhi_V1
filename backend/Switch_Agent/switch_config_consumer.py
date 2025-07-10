# switch_config_consumer.py

import redis
import json
import logging
import os
from elasticsearch import AsyncElasticsearch
from elasticsearch.exceptions import ConnectionError as EsConnectionError
import asyncio
import aiohttp
import uuid
from typing import Dict, List, Any

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- Configuration ---
REDIS_HOST = os.environ.get('REDIS_HOST', '192.168.56.30')
REDIS_PORT = int(os.environ.get('REDIS_PORT', 6379))
REDIS_STREAM_NAME = "vs:agent:switch_status"
REDIS_GROUP_NAME = "vs:group:switch_processor"
CONSUMER_NAME = f"switch_consumer_{uuid.uuid4().hex[:6]}"

ES_HOST = os.environ.get('ES_HOST', '192.168.56.30')
ES_PORT = int(os.environ.get('ES_PORT', 9200))

WEBSOCKET_NOTIFIER_URL = os.environ.get('WEBSOCKET_NOTIFIER_URL', 'http://127.0.0.1:8001/notify')

# --- Elasticsearch Index Definitions ---
HISTORICAL_ALARMS_INDEX = "monitor_historical_alarms"
SWITCH_OVERVIEW_INDEX = "switch_overview"

ES_INDEX_SCHEMAS = {
    HISTORICAL_ALARMS_INDEX: {
        "mappings": {
            "properties": {
                "timestamp": {"type": "date"},
                "device_ip": {"type": "keyword"},
                "device_name": {"type": "keyword"},
                "alarm_type": {"type": "keyword"},
                "severity": {"type": "keyword"},
                "message": {"type": "text", "fields": {"keyword": {"type": "keyword", "ignore_above": 2048}}},
                "identifier": {"type": "keyword"},
                "status": {"type": "keyword"} # 'new', 'cleared'
            }
        }
    },
    SWITCH_OVERVIEW_INDEX: {
        "mappings": {
            "properties": {
                "timestamp": {"type": "date"},
                "switch_ip": {"type": "keyword"},
                "hostname": {"type": "keyword"},
                "uptime": {"type": "keyword"},
                "model": {"type": "keyword"},
                "cpu_utilization": {"type": "keyword"}, # Stored as string like "5%"
                "memory_utilization": {"type": "keyword"}, # Stored as string like "50.23%"
                "fans": {"type": "nested"},
                "power_supplies": {"type": "nested"},
                "temperature_sensors": {"type": "nested"},
                "interfaces": {
                    "type": "nested",
                    "properties": {
                        "index": {"type": "keyword"}, # Added index as keyword
                        "name": {"type": "keyword"},
                        "alias": {"type": "text"},
                        "admin_status": {"type": "keyword"},
                        "oper_status": {"type": "keyword"},
                        "in_octets": {"type": "long"},
                        "out_octets": {"type": "long"},
                        "in_kbps": {"type": "float"},
                        "out_kbps": {"type": "float"},
                    }
                }
            }
        }
    }
}

# --- Global Clients ---
r: redis.Redis = None
es: AsyncElasticsearch = None
http_session: aiohttp.ClientSession = None
last_active_alarms_redis_key = "switch_last_active_alarms"

async def initialize_clients():
    """Initializes and connects all required clients."""
    global r, es, http_session
    # Redis
    r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=0, decode_responses=True)
    await asyncio.get_event_loop().run_in_executor(None, r.ping)
    logging.info("Consumer: Successfully connected to Redis.")
    
    # Elasticsearch
    es = AsyncElasticsearch([{'host': ES_HOST, 'port': ES_PORT, 'scheme': 'http'}])
    await es.info() # Test connection
    logging.info(f"Consumer: Successfully connected to Elasticsearch.")
    
    # aiohttp
    http_session = aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=False))
    logging.info("Consumer: aiohttp ClientSession initialized.")

async def setup_es_and_redis():
    """Creates ES indices and Redis consumer group if they don't exist."""
    # ES Indices
    for index, schema in ES_INDEX_SCHEMAS.items():
        if not await es.indices.exists(index=index):
            await es.indices.create(index=index, body=schema)
            logging.info(f"Elasticsearch index '{index}' created.")
        else:
            logging.info(f"Elasticsearch index '{index}' already exists.")
    
    # Redis Group
    try:
        r.xgroup_create(REDIS_STREAM_NAME, REDIS_GROUP_NAME, id='0', mkstream=True)
        logging.info(f"Redis group '{REDIS_GROUP_NAME}' created for stream '{REDIS_STREAM_NAME}'.")
    except redis.exceptions.ResponseError as e:
        if "BUSYGROUP" in str(e):
            logging.info(f"Redis group '{REDIS_GROUP_NAME}' already exists.")
        else:
            raise

async def send_websocket_notification(payload: Dict):
    """Sends a notification to the WebSocket server."""
    try:
        async with http_session.post(WEBSOCKET_NOTIFIER_URL, json=payload) as response:
            if response.status != 200:
                logging.error(f"Failed to send WebSocket notification: {response.status} - {await response.text()}")
            else:
                logging.info(f"WebSocket notification sent for {payload.get('device_ip')}: {payload.get('alarm_type')}")
    except Exception as e:
        logging.error(f"Error sending WebSocket notification: {e}", exc_info=True)

async def process_alarms(payload: Dict):
    """Compares current vs previous alarms, sends notifications, and logs to ES."""
    device_ip = payload['device_ip']
    current_alarms = {a['identifier']: a for a in payload.get('active_alarms', [])}
    
    # Get previous alarms from Redis
    prev_alarms_json = r.hget(last_active_alarms_redis_key, device_ip)
    prev_alarms = json.loads(prev_alarms_json) if prev_alarms_json else {}

    # Find new alarms
    for identifier, alarm in current_alarms.items():
        if identifier not in prev_alarms:
            logging.info(f"NEW ALARM for {device_ip}: {alarm['message']}")
            alarm_doc = {**alarm, "timestamp": payload['timestamp'], "device_ip": device_ip, "device_name": payload['device_name'], "status": "new"}
            await es.index(index=HISTORICAL_ALARMS_INDEX, document=alarm_doc)
            await send_websocket_notification({**alarm, "device_ip": device_ip, "device_name": payload['device_name'], "frontend_id": payload['frontend_id']})

    # Find cleared alarms
    for identifier, alarm in prev_alarms.items():
        if identifier not in current_alarms:
            logging.info(f"CLEARED ALARM for {device_ip}: {alarm['message']}")
            cleared_alarm_doc = {**alarm, "timestamp": payload['timestamp'], "device_ip": device_ip, "device_name": payload['device_name'], "status": "cleared", "severity": "INFO"}
            await es.index(index=HISTORICAL_ALARMS_INDEX, document=cleared_alarm_doc)
            await send_websocket_notification({**cleared_alarm_doc, "frontend_id": payload['frontend_id']})

    # Update Redis with current alarms
    if current_alarms:
        r.hset(last_active_alarms_redis_key, device_ip, json.dumps(current_alarms))
    else:
        r.hdel(last_active_alarms_redis_key, device_ip)

async def index_overview_data(payload: Dict):
    """Indexes the comprehensive switch overview data to Elasticsearch."""
    details = payload['details']
    
    # Prepare interfaces data for Elasticsearch, ensuring proper types
    interfaces_for_es = []
    for iface in details.get('interfaces', []):
        interfaces_for_es.append({
            "index": iface.get('index'),
            "name": iface.get('name'),
            "alias": iface.get('alias'),
            "admin_status": iface.get('admin_status'),
            "oper_status": iface.get('oper_status'),
            "in_octets": int(iface.get('in_octets', 0)),
            "out_octets": int(iface.get('out_octets', 0)),
            "in_kbps": float(iface.get('in_kbps', 0.0)),
            "out_kbps": float(iface.get('out_kbps', 0.0)),
        })

    overview_doc = {
        "timestamp": payload['timestamp'],
        "switch_ip": payload['device_ip'],
        "hostname": details.get('system_info', {}).get('hostname'),
        "uptime": details.get('system_info', {}).get('uptime'),
        "model": payload.get('model'), # Model comes from the top-level payload
        "cpu_utilization": details.get('hardware_health', {}).get('cpu_utilization'),
        "memory_utilization": details.get('hardware_health', {}).get('memory_utilization'),
        "fans": details.get('hardware_health', {}).get('fans', []),
        "power_supplies": details.get('hardware_health', {}).get('power_supplies', []),
        "temperature_sensors": details.get('hardware_health', {}).get('temperature_sensors', []),
        "interfaces": interfaces_for_es
    }
    
    # Use a unique ID for Elasticsearch document to prevent duplicates on re-indexing
    doc_id = f"{payload['device_ip']}_{payload['timestamp']}"
    await es.index(index=SWITCH_OVERVIEW_INDEX, document=overview_doc, id=doc_id)
    logging.info(f"Indexed overview data for {payload['device_ip']} to '{SWITCH_OVERVIEW_INDEX}'.")

async def process_message(message_data: str):
    """Main processing function for a single message from Redis."""
    try:
        payload = json.loads(message_data)
        logging.info(f"Processing message for device {payload.get('device_ip')}")

        # Task 1: Process alarms (new/cleared), notify, and log
        await process_alarms(payload)
        
        # Task 2: Index the full overview data
        await index_overview_data(payload)

    except json.JSONDecodeError as e:
        logging.error(f"Failed to decode JSON message: {e}")
    except Exception as e:
        logging.error(f"Error processing message for {payload.get('device_ip', 'unknown')}: {e}", exc_info=True)

async def consume_loop():
    """Continuously consumes messages from the Redis stream."""
    logging.info(f"Consumer '{CONSUMER_NAME}' starting to listen to stream '{REDIS_STREAM_NAME}'...")
    while True:
        try:
            response = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: r.xreadgroup(
                    groupname=REDIS_GROUP_NAME,
                    consumername=CONSUMER_NAME,
                    streams={REDIS_STREAM_NAME: '>'},
                    count=10,
                    block=5000
                )
            )
            if not response:
                continue

            for stream, messages in response:
                for msg_id, data in messages:
                    await process_message(data['data'])
                    r.xack(REDIS_STREAM_NAME, REDIS_GROUP_NAME, msg_id)

        except redis.exceptions.ConnectionError as e:
            logging.error(f"Redis connection error: {e}. Retrying...")
            await asyncio.sleep(5)
        except Exception as e:
            logging.error(f"Unhandled error in consumer loop: {e}", exc_info=True)
            await asyncio.sleep(1)

async def main():
    """Main entry point for the consumer application."""
    try:
        await initialize_clients()
        await setup_es_and_redis()
        await consume_loop()
    except Exception as e:
        logging.critical(f"Consumer application failed to start: {e}", exc_info=True)
    finally:
        if es: await es.close()
        if http_session: await http_session.close()
        logging.info("Consumer clients closed.")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("Consumer stopped by user.")
