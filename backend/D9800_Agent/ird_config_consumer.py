# ird_config_consumer.py
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

REDIS_STREAM_NAME = "vs:agent:ird_config_status"
CONSUMER_GROUP_NAME = "ird_config_es_notifier_group"
CONSUMER_NAME = "ird_config_consumer_instance_1"

IRD_ALARM_STATES_PER_DEVICE_KEY = "ird_alarm_states_per_device"

# Initialize Redis client at module level, as it's synchronous
r = None
try:
    r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=0, decode_responses=False)
    r.ping()
    logging.info("Successfully connected to Redis.")
except redis.exceptions.ConnectionError as e:
    logging.critical(f"FATAL: Failed to connect to Redis at {REDIS_HOST}:{REDIS_PORT}. Please ensure Redis is running and accessible. Error: {e}")
    exit(1)
except Exception as e:
    logging.critical(f"FATAL: Unexpected error during Redis connection test: {e}", exc_info=True)
    exit(1)

async def send_websocket_notification(http_session, message_data):
    """Sends a JSON message to the WebSocket notifier's HTTP endpoint."""
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

async def process_message(es_client, http_session_client, message_id, payload):
    """Processes a single message from the Redis stream, indexes to ES, and sends WS notifications."""
    logging.info(f"Processing message ID: {message_id.decode()}")
    logging.debug(f"  Payload: {payload}")

    device_ip = payload.get('device_ip')
    frontend_block_id = payload.get('frontend_block_id')
    
    overall_status_from_agent = payload.get('status', 'UNKNOWN').upper() 
    overall_severity_from_agent = payload.get('severity', 'UNKNOWN').upper() 
    overall_message_from_agent = payload.get('message', 'No specific status message.')
    timestamp = payload.get('timestamp')
    agent_type = payload.get('agent_type')
    group_name = payload.get('group_name')
    device_name = payload.get('device_name')

    # --- Alarm State Management for Frontend Notifications ---
    device_state_field = f"{frontend_block_id}_{device_ip}" 

    last_state_json = r.hget(IRD_ALARM_STATES_PER_DEVICE_KEY, device_state_field)
    last_state = json.loads(last_state_json.decode('utf-8')) if last_state_json else None

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
    ws_type = "UNKNOWN"

    # Scenario 1: Alarm clears (was alarming, now OK/INFO)
    if not is_current_alarm_state:
        if is_previous_alarm_state:
            send_ws_notification = True
            ws_severity = "INFO"
            ws_status = "CLEARED"
            ws_message = f"IRD device {device_name} (IP: {device_ip}) is now OK. All previous alarms cleared."
            ws_type = "ALARM_CLEARED"
            logging.info(f"IRD block {device_state_field} has CLEARED. Previous severity: {last_state.get('severity')}. Sending WS notification.")
            r.hdel(IRD_ALARM_STATES_PER_DEVICE_KEY, device_state_field)
        else:
            logging.debug(f"IRD block {device_state_field} is OK/INFO and no previous alarm. Skipping WS notification.")
            
    # Scenario 2: Current status is alarming
    elif is_current_alarm_state:
        send_ws_notification = True # Always send WS notification if currently alarming

        if not is_previous_alarm_state:
            ws_type = "NEW_ALARM"
            logging.info(f"NEW ALARM detected for {device_state_field}: {overall_severity_from_agent} - {overall_message_from_agent}. Sending WS notification.")
        elif (overall_severity_from_agent != last_state.get('severity').upper() or 
              overall_message_from_agent != last_state.get('message')):
            ws_type = "ALARM_UPDATE"
            logging.info(f"ALARM UPDATED for {device_state_field}. Previous severity: {last_state.get('severity')}, Current: {overall_severity_from_agent}. Sending WS notification.")
        else:
            ws_type = "ALARM_CONTINUES" # New type for continuing alarms
            logging.info(f"IRD block {device_state_field} is still {overall_severity_from_agent}. Severity/Message not changed. Sending WS notification (ALARM_CONTINUES).")
        
        r.hset(IRD_ALARM_STATES_PER_DEVICE_KEY, device_state_field, json.dumps({
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
            "message": ws_message,
            "severity": ws_severity,
            "status": ws_status,
            "frontend_block_id": frontend_block_id,
            "group_name": group_name,
            "type": ws_type,
        }
        await send_websocket_notification(http_session_client, websocket_message)
    else:
        logging.info(f"Skipping WebSocket notification for {device_ip} as status is '{overall_status_from_agent}' and no state change.")


    # --- Index to monitor_historical_alarms ---
    if overall_status_from_agent != 'OK':
        details = payload.get('details', {})
        faults = details.get('faults', [])
        api_errors = details.get('api_errors', [])
        
        es_actions = []

        if faults:
            logging.info(f"Found {len(faults)} faults to index for {device_ip}.")
            for fault_data in faults:
                alarm_doc = {
                    "_id": str(uuid.uuid4()),
                    "_index": "monitor_historical_alarms",
                    "timestamp": timestamp,
                    "message": f"Fault: {fault_data.get('details', 'N/A')} (Severity: {fault_data.get('type', 'N/A')}, Since: {fault_data.get('set_since', 'N/A')})", 
                    "device_name": device_name,
                    "block_id": frontend_block_id,
                    "severity": fault_data.get('type', 'UNKNOWN').upper(),
                    "type": agent_type,
                    "device_ip": device_ip,
                    "group_name": group_name
                }
                es_actions.append(alarm_doc)
        elif api_errors:
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
            logging.info(f"No specific faults or API errors found in details for {device_ip} despite non-'ok' status. Skipping specific alarm indexing.")
        
        if es_actions:
            try:
                await async_bulk(es_client, es_actions)
                logging.info(f"Indexed {len(es_actions)} historical alarms/errors for {device_ip} to Elasticsearch via bulk API.")
            except Exception as e:
                logging.error(f"Failed to bulk index historical alarms/errors to Elasticsearch: {e}", exc_info=True)
    else:
        logging.info(f"Skipping Elasticsearch indexing for {device_ip} as status is 'ok'.")


    # --- Insert/Update to ird_configurations ---
    ird_ip = payload.get('device_ip')
    details = payload.get('details', {})
    rf_status = details.get('rf_status', [])
    moip_output_status = details.get('moip_output_status', {})
    asi_status = details.get('asi_status', [])
    moip_input_status_list = details.get('moip_input_status', [])
    
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
                logging.warning(f"Could not parse system_id as integer: '{system_id_str}' for {ird_ip} (RF)")
        except Exception as e:
            logging.warning(f"Error converting system_id '{rf_data.get('system_id')}' to int for {ird_ip} (RF): {e}")

        try: freq = float(rf_data.get('frequency_mhz')) if rf_data.get('frequency_mhz') is not None else None
        except ValueError: logging.warning(f"Could not convert frequency_mhz to float for {ird_ip} (RF)")
        try: SR = float(rf_data.get('symbol_rate')) if rf_data.get('symbol_rate') is not None else None
        except ValueError: logging.warning(f"Could not convert symbol_rate to float for {ird_ip} (RF)")
        Pol = rf_data.get('polarity')
        try: C_N = float(rf_data.get('cn_margin')) if rf_data.get('cn_margin') is not None else None
        except ValueError: logging.warning(f"Could not convert cn_margin to float for {ird_ip} (RF)")
        try: signal_strength = float(rf_data.get('input_level_dbm')) if rf_data.get('input_level_dbm') is not None else None
        except ValueError: logging.warning(f"Could not convert input_level_dbm to float for {ird_ip} (RF)")
        try: input_bitrate = float(rf_data.get('input_bw')) if rf_data.get('input_bw') is not None else None
        except ValueError: logging.warning(f"Could not convert input_bw to float for {ird_ip} (RF)")

    moip_status = "Locked" if moip_output_status.get('locked') else "Not Locked"
    output_bitrate = float(moip_output_status.get('ts1rate', 0.0))

    if input_bitrate is None and moip_input_status_list:
        first_moip_input = moip_input_status_list[0]
        if first_moip_input.get('is_locked'):
            try:
                input_bitrate = float(first_moip_input.get('inputrate', 0.0))
            except ValueError:
                logging.warning(f"Could not convert MOIP inputrate to float for {ird_ip}")

    asi_lock_status_for_es = None
    asi_input_bw_for_es = None
    asi_system_id_for_es = None

    if asi_status:
        first_asi_input = asi_status[0]
        asi_lock_status_for_es = first_asi_input.get('lock_status')
        try: asi_input_bw_for_es = float(first_asi_input.get('input_bw')) if first_asi_input.get('input_bw') is not None else None
        except ValueError: logging.warning(f"Could not convert ASI input_bw to float for {ird_ip}")
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
        "asi_system_id": asi_system_id_for_es,
        "last_updated": payload.get('timestamp')
    }

    config_doc = {k: v for k, v in config_doc.items() if v is not None}

    try:
        await es_client.index(index="ird_configurations", id=ird_ip, document=config_doc)
        logging.info(f"Inserted/Updated IRD configuration for {ird_ip} to Elasticsearch.")
    except Exception as e:
        logging.error(f"Failed to Insert/Update IRD configuration to Elasticsearch: {e}", exc_info=True)


async def consume_messages(es_client, http_session_client):
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
                            processing_tasks.append(process_message(es_client, http_session_client, message_id, payload))
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
            await asyncio.sleep(5)
        except Exception as e:
            logging.error(f"Unhandled error in Redis stream listener: {e}", exc_info=True)
            await asyncio.sleep(1)

async def main_async():
    """Main asynchronous entry point."""
    es_client = AsyncElasticsearch(f"http://{ES_HOST}:{ES_PORT}")
    http_session_client = aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=False))

    try:
        await es_client.info()
        logging.info("Successfully connected to Elasticsearch.")

        setup_consumer_group()
        await consume_messages(es_client, http_session_client)
    except Exception as e:
        logging.critical(f"Unhandled error in main_async: {e}", exc_info=True)
    finally:
        if http_session_client:
            await http_session_client.close()
            logging.info("aiohttp session closed.")
        if es_client:
            await es_client.close()
            logging.info("Elasticsearch client closed.")

if __name__ == "__main__":
    try:
        asyncio.run(main_async())
    except KeyboardInterrupt:
        logging.info("Consumer stopped by user.")
    except Exception as e:
        logging.critical(f"IRD Config Consumer terminated due to an unhandled error: {e}", exc_info=True)
        exit(1)

