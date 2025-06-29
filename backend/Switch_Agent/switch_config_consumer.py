# switch_config_consumer.py

import redis
import json
import time
import logging
import os
from elasticsearch import AsyncElasticsearch
from elasticsearch.exceptions import NotFoundError, ConnectionError as EsConnectionError
from datetime import datetime
import asyncio
import aiohttp
import uuid
from elasticsearch.helpers import async_bulk
from collections import defaultdict
from typing import Dict # Added for Dict type hint

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- Configuration ---
REDIS_HOST = os.environ.get('REDIS_HOST', '192.168.56.30')
REDIS_PORT = int(os.environ.get('REDIS_PORT', 6379))

ES_HOST = os.environ.get('ES_HOST', '192.168.56.30')
ES_PORT = int(os.environ.get('ES_PORT', 9200))

WEBSOCKET_NOTIFIER_URL = os.environ.get('WEBSOCKET_NOTIFIER_URL', 'http://127.0.0.1:8001/notify')

# Specific Redis Stream Names and Consumer Groups
REDIS_STREAMS = {
    "vs:agent:cisco_sw_status": "vs:group:cisco_sw_processor",
    "vs:agent:nexus_sw_status": "vs:group:nexus_sw_processor"
}
CONSUMER_NAME = "switch_config_consumer_instance_1" # Unique name for this consumer instance

# Elasticsearch Index Names and Schemas
HISTORICAL_ALARMS_INDEX_NAME = "monitor_historical_alarms" # Updated index name
HISTORICAL_ALARMS_INDEX_SCHEMA = {
    "mappings": {
        "properties": {
            "device_ip": {"type": "keyword"},
            "device_name": {"type": "keyword"},
            "alarm_type": {"type": "keyword"},
            "severity": {"type": "keyword"},
            "message": {"type": "text"},
            "timestamp": {"type": "date"},
            "agent_type": {"type": "keyword"},
            "frontend_block_id": {"type": "keyword"},
            "alarm_identifier": {"type": "keyword"}
        }
    }
}

# New Elasticsearch Index for Switch Overview - UPDATED SCHEMA
SWITCH_OVERVIEW_INDEX_NAME = "switch_overview"
SWITCH_OVERVIEW_INDEX_SCHEMA = {
    "mappings": {
        "properties": {
            "switch_ip": { "type": "keyword" },
            "hostname": { "type": "keyword" },
            # CPU and Memory now optional (N/A for Nexus, only Cisco fetches)
            "cpu_utilization": { "type": "keyword" }, # Store as keyword/text to preserve '%' or "N/A"
            "memory_utilization": { "type": "keyword" }, # Store as keyword/text to preserve '%' or "N/A"
            # Power Supply, Temperature, Fan, Uptime, VLANs, Multicast, Inventory removed as OIDs are no longer fetched
            "interfaces": {
                "type": "nested",
                "properties": {
                    "index": { "type": "keyword" },
                    "name": { "type": "keyword" },
                    "alias": { "type": "text" }, # New field
                    "admin_status": { "type": "keyword" },
                    "oper_status": { "type": "keyword" },
                    "is_operational_down": { "type": "boolean" }, # New field
                    "in_octets": { "type": "long" },
                    "out_octets": { "type": "long" },
                    "in_multicast_pkts": { "type": "long" }, # Still keep in schema, will be 0
                    "out_multicast_pkts": { "type": "long" }, # Still keep in schema, will be 0
                    "in_bitrate_mbps": { "type": "keyword" },
                    "out_bitrate_mbps": { "type": "keyword" },
                    "vlan": { "type": "integer" } # Still keep in schema, will be 0 if VLAN OIDs are removed
                }
            },
            "timestamp": { "type": "date" }
        }
    }
}


# Initialize Redis and Elasticsearch clients
r = None
es = None
http_session = None # Global aiohttp session

async def initialize_clients():
    """Initializes Redis, Elasticsearch, and aiohttp clients."""
    global r, es, http_session
    try:
        r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=0, decode_responses=False)
        r.ping()
        logging.info("Consumer: Successfully connected to Redis.")
    except redis.exceptions.ConnectionError as e:
        logging.critical(f"Consumer: FATAL: Failed to connect to Redis at {REDIS_HOST}:{REDIS_PORT}. Error: {e}")
        sys.exit(1)

    try:
        es = AsyncElasticsearch(
            [{'host': ES_HOST, 'port': ES_PORT, 'scheme': 'http'}]
        )
        await es.info()
        logging.info(f"Consumer: Successfully connected to Elasticsearch at {ES_HOST}:{ES_PORT}.")
    except EsConnectionError as e:
        logging.critical(f"Consumer: FATAL: Failed to connect to Elasticsearch at {ES_HOST}:{ES_PORT}. Error: {e}")
        sys.exit(1)
    except Exception as e:
        logging.critical(f"Consumer: FATAL: Unexpected error during Elasticsearch connection test: {e}", exc_info=True)
        sys.exit(1)
    
    if http_session is None or http_session.closed:
        http_session = aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=False))
        logging.info("aiohttp ClientSession initialized for WebSocket notifications.")


async def create_es_index_if_not_exists(index_name: str, index_schema: Dict):
    """Creates an Elasticsearch index with the given schema if it does not already exist."""
    try:
        if not await es.indices.exists(index=index_name):
            await es.indices.create(index=index_name, body=index_schema)
            logging.info(f"Elasticsearch index '{index_name}' created successfully.")
        else:
            logging.info(f"Elasticsearch index '{index_name}' already exists.")
    except EsConnectionError as e:
        logging.error(f"Elasticsearch connection error while checking/creating index '{index_name}': {e}")
    except Exception as e:
        logging.error(f"Error checking/creating Elasticsearch index '{index_name}': {e}", exc_info=True)


async def setup_consumer_groups():
    """Ensures consumer groups exist for all configured Redis streams."""
    for stream_name, group_name in REDIS_STREAMS.items():
        try:
            r.xgroup_create(stream_name, group_name, id='0', mkstream=True)
            logging.info(f"Ensured Redis consumer group '{group_name}' exists for stream '{stream_name}'.")
        except redis.exceptions.ResponseError as e:
            if "BUSYGROUP" in str(e):
                logging.info(f"Redis consumer group '{group_name}' already exists for stream '{stream_name}'.")
            else:
                logging.error(f"Error creating Redis consumer group '{group_name}' for stream '{stream_name}': {e}")
        except Exception as e:
            logging.error(f"Unexpected error during Redis consumer group setup for stream '{stream_name}': {e}", exc_info=True)


async def send_websocket_notification(message_data):
    """Sends a JSON message to the WebSocket notifier's HTTP endpoint."""
    try:
        async with http_session.post(WEBSOCKET_NOTIFIER_URL, json=message_data) as response:
            if response.status != 200:
                logging.error(f"Failed to send WebSocket notification: {response.status} - {await response.text()}")
            else:
                logging.debug(f"WebSocket notification sent for {message_data.get('device_ip')}.")
    except aiohttp.ClientConnectionError as e:
        logging.error(f"WebSocket notifier connection error: {e}. Is the notifier running at {WEBSOCKET_NOTIFIER_URL}?")
    except Exception as e:
        logging.error(f"Error sending WebSocket notification: {e}", exc_info=True)

async def process_redis_message(message: dict):
    """
    Processes a single message from a Redis stream.
    Parses it, stores historical alarms in Elasticsearch, and indexes data to the `switch_overview` index.
    """
    try:
        payload_bytes = message.get('data')
        if not payload_bytes:
            logging.warning(f"Received Redis message with no 'data' field: {message}")
            return

        try:
            payload_str = payload_bytes.decode('utf-8')
        except UnicodeDecodeError:
            logging.error(f"Could not decode message payload as UTF-8. Attempting Latin-1. Raw: {payload_bytes[:50]}...")
            payload_str = payload_bytes.decode('latin-1', errors='ignore')

        payload = json.loads(payload_str)
        logging.info(f"Consumer: Processing message for device {payload.get('device_ip')} (Agent Type: {payload.get('agent_type')})...")

        # --- 1. Store Historical Alarms in Elasticsearch ---
        if payload.get("overall_status") in ["alarm", "warning", "ok"]:
            alarm_doc = {
                "device_ip": payload.get("device_ip"),
                "device_name": payload.get("device_name"),
                "alarm_type": payload.get("alarm_type", "Overall Status"),
                "severity": payload.get("overall_severity"),
                "message": payload.get("overall_message"),
                "timestamp": payload.get("timestamp"),
                "agent_type": payload.get("agent_type"),
                "frontend_block_id": payload.get("frontend_block_id"),
                "alarm_identifier": payload.get("alarm_identifier", "overall")
            }

            logging.info(f"Consumer: Attempting to index historical alarm for {payload.get('device_ip')} (Severity: {alarm_doc['severity']})...")
            if payload.get("details", {}).get("active_individual_alarms"):
                for individual_alarm in payload["details"]["active_individual_alarms"]:
                    individual_alarm_doc = {
                        "device_ip": payload.get("device_ip"),
                        "device_name": payload.get("device_name"),
                        "alarm_type": individual_alarm.get("type", "Component Alarm"),
                        "severity": individual_alarm.get("severity"),
                        "message": individual_alarm.get("message"),
                        "timestamp": payload.get("timestamp"),
                        "agent_type": payload.get("agent_type"),
                        "frontend_block_id": payload.get("frontend_block_id"),
                        "alarm_identifier": individual_alarm.get("identifier")
                    }
                    try:
                        await es.index(index=HISTORICAL_ALARMS_INDEX_NAME, document=individual_alarm_doc, id=str(uuid.uuid4()))
                        logging.info(f"Consumer: Successfully indexed individual alarm for {payload.get('device_ip')}: {individual_alarm.get('message')} to {HISTORICAL_ALARMS_INDEX_NAME}.")
                    except EsConnectionError as es_conn_e:
                        logging.error(f"Consumer: Elasticsearch connection error during individual alarm indexing for {payload.get('device_ip')}: {es_conn_e}")
                    except Exception as es_e:
                        logging.error(f"Consumer: Error indexing individual alarm for {payload.get('device_ip')}: {es_e}", exc_info=True)
            else:
                if payload.get("overall_status") == "ok":
                     try:
                        await es.index(index=HISTORICAL_ALARMS_INDEX_NAME, document=alarm_doc, id=str(uuid.uuid4()))
                        logging.info(f"Consumer: Successfully indexed overall OK status for {payload.get('device_ip')}: {alarm_doc.get('message')} to {HISTORICAL_ALARMS_INDEX_NAME}.")
                     except EsConnectionError as es_conn_e:
                        logging.error(f"Consumer: Elasticsearch connection error during overall OK status indexing for {payload.get('device_ip')}: {es_conn_e}")
                     except Exception as es_e:
                        logging.error(f"Consumer: Error indexing overall OK status for {payload.get('device_ip')}: {es_e}", exc_info=True)


        # --- 2. Store Switch Overview Data in Elasticsearch ---
        switch_details_data = payload.get("details", {}).get("full_switch_details")
        
        if switch_details_data: 
            switch_ip = switch_details_data.get("ip")
            hostname = switch_details_data.get("system_info", {}).get("hostname", "N/A")

            # Since VLAN OIDs are removed, the vlan_map will be empty, and interface_vlan will default to 0.
            # This is expected behavior given the strict OID requirement.
            vlan_map = {} # No VLAN OIDs, so this will remain empty
            # for vlan_entry in switch_details_data.get("vlans", []):
            #     vlan_id = vlan_entry.get("vlan_id")
            #     for port in vlan_entry.get("ports", []):
            #         if_index = port.get("if_index")
            #         if if_index is not None and vlan_id is not None:
            #             vlan_map[str(if_index)] = vlan_id

            interfaces_for_es = []
            for iface in switch_details_data.get("interfaces", []):
                if_index = iface.get("index")
                interface_vlan = int(vlan_map.get(str(if_index), 0)) # Will be 0

                interfaces_for_es.append({
                    "index": iface.get("index"),
                    "name": iface.get("name"),
                    "alias": iface.get("alias", ""),
                    "admin_status": iface.get("admin_status"),
                    "oper_status": iface.get("oper_status"),
                    "is_operational_down": iface.get("is_operational_down", False),
                    "in_octets": iface.get("in_octets"),
                    "out_octets": iface.get("out_octets"),
                    "in_multicast_pkts": iface.get("in_multicast_pkts", 0), # Will be 0
                    "out_multicast_pkts": iface.get("out_multicast_pkts", 0), # Will be 0
                    "in_bitrate_mbps": iface.get("in_bitrate_mbps"),
                    "out_bitrate_mbps": iface.get("out_bitrate_mbps"),
                    "vlan": interface_vlan,
                })
            
            hardware_health = switch_details_data.get("hardware_health", {})

            # Construct the document for the switch_overview index
            overview_doc = {
                "switch_ip": switch_ip,
                "hostname": hostname,
                "cpu_utilization": hardware_health.get("cpu_utilization"),
                "memory_utilization": hardware_health.get("memory_utilization"),
                # Other hardware health fields removed as OIDs are no longer fetched
                # "power_supply_status": hardware_health.get("power_supply_status", []),
                # "temperature_sensors": hardware_health.get("temperature_sensors", []),
                # "fan_status": hardware_health.get("fan_status", []),
                # "uptime": switch_details_data.get("uptime"),
                "interfaces": interfaces_for_es,
                # "vlans": switch_details_data.get("vlans", []),
                # "multicast_info": switch_details_data.get("multicast_info", {}),
                # "inventory": switch_details_data.get("inventory", {}),
                "timestamp": payload["timestamp"]
            }

            logging.info(f"Consumer: Attempting to index switch_overview data for {switch_ip} to {SWITCH_OVERVIEW_INDEX_NAME}...")
            try:
                doc_id = f"{switch_ip}-{payload['timestamp']}" 
                await es.index(
                    index=SWITCH_OVERVIEW_INDEX_NAME,
                    document=overview_doc,
                    id=doc_id
                )
                logging.info(f"Consumer: Successfully indexed switch_overview data for {switch_ip} to {SWITCH_OVERVIEW_INDEX_NAME}.")
            except EsConnectionError as es_conn_e:
                logging.error(f"Consumer: Elasticsearch connection error during switch_overview indexing for {switch_ip}: {es_conn_e}")
            except Exception as es_e:
                logging.error(f"Consumer: Error indexing switch_overview data for {switch_ip}: {es_e}", exc_info=True)
        else:
            logging.warning(f"Consumer: No 'full_switch_details' found in payload for {payload.get('device_ip')}. Skipping switch_overview indexing for this message.")


    except json.JSONDecodeError as e:
        logging.error(f"Consumer: Failed to decode JSON from Redis message: {e}. Raw message: {message.get('data')}", exc_info=True)
    except Exception as e:
        logging.error(f"Consumer: Error processing Redis message: {e}", exc_info=True)

async def consume_messages():
    """Continuously consumes messages from all configured Redis streams."""
    stream_ids = {stream_name: '>' for stream_name in REDIS_STREAMS.keys()}
    loop = asyncio.get_event_loop()

    while True:
        processing_tasks = []
        for stream_name, group_name in REDIS_STREAMS.items():
            try:
                response = await loop.run_in_executor(
                    None,
                    lambda: r.xreadgroup(
                        groupname=group_name,
                        consumername=CONSUMER_NAME,
                        streams={stream_name.encode('utf-8'): stream_ids[stream_name].encode('utf-8')},
                        count=10,
                        block=500
                    )
                )
                
                if response:
                    for _, messages in response:
                        for msg_id, message_data in messages:
                            message_id_str = msg_id.decode('utf-8') if isinstance(msg_id, bytes) else msg_id
                            decoded_message_data = {k.decode('utf-8') if isinstance(k, bytes) else k: v for k, v in message_data.items()}
                            
                            logging.debug(f"Consumer: Received message from stream '{stream_name}', ID: {message_id_str}")
                            processing_tasks.append(process_redis_message(decoded_message_data))
                            
                            await loop.run_in_executor(
                                None,
                                lambda: r.xack(stream_name.encode('utf-8'), group_name.encode('utf-8'), msg_id)
                            )
                            logging.debug(f"Consumer: Acknowledged message {message_id_str} from stream {stream_name}.")
                            
                            stream_ids[stream_name] = message_id_str

            except redis.exceptions.ConnectionError as e:
                logging.error(f"Consumer: Redis connection error for stream {stream_name}: {e}. Retrying in 5 seconds...")
                await asyncio.sleep(5)
            except Exception as e:
                logging.error(f"Consumer: Unhandled error reading from Redis stream {stream_name}: {e}", exc_info=True)
                await asyncio.sleep(1)

        if processing_tasks:
            await asyncio.gather(*processing_tasks, return_exceptions=True)
            logging.info(f"Consumer: Completed processing {len(processing_tasks)} tasks across all streams.")
        else:
            logging.debug("Consumer: No new messages from Redis streams after checking all configured streams.")
        
        if not processing_tasks:
            await asyncio.sleep(1)


async def main_async():
    """Main asynchronous entry point."""
    await initialize_clients()
    await create_es_index_if_not_exists(HISTORICAL_ALARMS_INDEX_NAME, HISTORICAL_ALARMS_INDEX_SCHEMA)
    await create_es_index_if_not_exists(SWITCH_OVERVIEW_INDEX_NAME, SWITCH_OVERVIEW_INDEX_SCHEMA)
    await setup_consumer_groups()
    await consume_messages()

if __name__ == "__main__":
    try:
        asyncio.run(main_async())
    except KeyboardInterrupt:
        logging.info("Switch Config Consumer stopped by user.")
    finally:
        if http_session:
            asyncio.run(http_session.close())
        if r:
            r.close()
        if es:
            asyncio.run(es.close())
