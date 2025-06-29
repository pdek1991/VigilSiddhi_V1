# switch_config_agent.py

import sys
import os
import json
import logging
import redis
from datetime import datetime
import asyncio
from typing import List, Dict, Any, Tuple, Optional
import aiohttp # Added for WebSocket notifications
from collections import defaultdict # Added to resolve NameError for defaultdict

# Adjust path to import custom modules
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__))))

# Import custom modules
from mysql_client import MySQLManager
from switch_api_client import switch_api_client # Use the shared client instance

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- Configuration ---
MYSQL_HOST = os.environ.get('MYSQL_HOST', '192.168.56.30')
MYSQL_DATABASE = os.environ.get('MYSQL_DATABASE', 'vigil_siddhi')
MYSQL_USER = os.environ.get('MYSQL_USER', 'vigilsiddhi')
MYSQL_PASSWORD = os.environ.get('MYSQL_PASSWORD', 'vigilsiddhi')

REDIS_HOST = os.environ.get('REDIS_HOST', '192.168.56.30')
REDIS_PORT = int(os.environ.get('REDIS_PORT', 6379))

# Dynamic Redis Stream Names based on model
CISCO_STREAM_NAME = "vs:agent:cisco_sw_status"
NEXUS_STREAM_NAME = "vs:agent:nexus_sw_status"

# Redis Hash name for storing overall device status for comparison
SWITCH_OVERALL_STATUS_TRACKING_KEY = "switch_overall_status_agent_tracking"

WEBSOCKET_NOTIFIER_URL = os.environ.get('WEBSOCKET_NOTIFIER_URL', 'http://127.0.0.1:8001/notify')

# Polling Intervals (in seconds)
CRITICAL_POLLING_INTERVAL_SECONDS = 300 # Poll interfaces, power supply every 5 minutes
SENSOR_POLLING_INTERVAL_SECONDS = 600   # Poll CPU, memory, temperature every 10 minutes

# Initialize Redis and MySQL clients outside main
r = None
mysql_manager = None
http_session = None # Global aiohttp session

try:
    r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=0, decode_responses=False)
    r.ping()
    logging.info("Switch Config Agent: Successfully connected to Redis.")
except redis.exceptions.ConnectionError as e:
    logging.critical(f"Switch Config Agent: FATAL: Failed to connect to Redis at {REDIS_HOST}:{REDIS_PORT}. Error: {e}")
    sys.exit(1)
except Exception as e:
    logging.critical(f"Switch Config Agent: FATAL: Unexpected error during Redis connection test: {e}", exc_info=True)
    sys.exit(1)

try:
    mysql_manager = MySQLManager(
        host=MYSQL_HOST,
        database=MYSQL_DATABASE,
        user=MYSQL_USER,
        password=MYSQL_PASSWORD
    )
    if not mysql_manager.connection or not mysql_manager.connection.is_connected():
        logging.critical(f"Switch Config Agent: FATAL: MySQLManager initialized but connection is not active. Check database credentials and server status.")
        sys.exit(1)
    logging.info("Switch Config Agent: Successfully initialized MySQLManager and connected to database.")
except Exception as e:
    logging.critical(f"Switch Config Agent: FATAL: Failed to initialize MySQLManager: {e}. Check MySQL config and connectivity.")
    sys.exit(1)


async def initialize_http_session():
    """Initializes the aiohttp client session for WebSocket notifications."""
    global http_session
    if http_session is None or http_session.closed:
        http_session = aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=False))
        logging.info("aiohttp ClientSession initialized for WebSocket notifications.")

async def close_http_session():
    """Closes the aiohttp client session."""
    global http_session
    if http_session and not http_session.closed:
        await http_session.close()
        logging.info("aiohttp ClientSession closed.")

async def send_websocket_notification(message_data):
    """Sends a JSON message to the WebSocket notifier's HTTP endpoint."""
    await initialize_http_session() # Ensure session is active
    try:
        async with http_session.post(WEBSOCKET_NOTIFIER_URL, json=message_data) as response:
            if response.status != 200:
                logging.error(f"Failed to send WebSocket notification: {response.status} - {await response.text()}")
            else:
                logging.info(f"WebSocket overall status notification sent for {message_data.get('device_ip')}. Type: {message_data.get('type')}")
    except aiohttp.ClientConnectionError as e:
        logging.error(f"WebSocket notifier connection error: {e}. Is the notifier running at {WEBSOCKET_NOTIFIER_URL}?")
    except Exception as e:
        logging.error(f"Error sending WebSocket notification: {e}", exc_info=True)


def publish_status_to_redis(stream_name, payload):
    """Publishes a status message to the specified Redis Stream."""
    try:
        r.xadd(stream_name, {"data": json.dumps(payload).encode('utf-8')})
        logging.info(f"Published to {stream_name}: {payload.get('frontend_block_id', payload.get('device_ip'))} - {payload.get('status')} ({payload.get('agent_type')}) for consumer.")
    except Exception as e:
        logging.error(f"Failed to publish to Redis stream {stream_name}: {e}")

def get_overall_status_and_message(switch_details: Dict[str, Any], api_errors: List[str], hostname: str):
    """
    Determines overall status and severity for a switch based on fetched details and API errors.
    Prioritizes critical alarms.
    This function will gracefully handle cases where `switch_details` might be partial
    due to selective data fetching.
    """
    overall_status = "ok"
    overall_severity = "INFO"
    overall_message = f"Switch {switch_details.get('ip', 'N/A')} status is OK."
    active_alarms = [] # Collect all specific alarms to determine overall status
    summary_messages = defaultdict(int) # For summarizing repeated issues

    # Severity ranking
    severity_rank = {
        "INFO": 0, "OK": 0, "NORMAL": 0, "CLEARED": 0,
        "WARNING": 1, "MINOR": 1,
        "MAJOR": 2, "ALARM": 2,
        "CRITICAL": 3, "ERROR": 3, "FATAL": 3,
        "UNKNOWN": -1
    }

    current_highest_rank = 0

    # Evaluate API errors and adjust severity for "data not found" issues
    for err_msg in api_errors:
        error_severity = "ERROR" # Default to ERROR
        # Treat "No Such Object", "OID_NOT_FOUND", "NO_RESPONSE", "No data found" as WARNING for less critical impact
        if any(keyword in err_msg for keyword in ["No Such Object", "OID_NOT_FOUND", "NO_RESPONSE", "No data found"]):
            error_severity = "WARNING" 
            if "No IGMP snooping data" in err_msg:
                summary_messages["No IGMP snooping data found"] += 1
                continue 
            if "No generic memory data" in err_msg:
                summary_messages["No generic memory data found"] += 1
                continue 
            if "No power supply data" in err_msg:
                summary_messages["No power supply data found"] += 1
                continue 
            if "No temperature sensor data" in err_msg:
                summary_messages["No temperature sensor data found"] += 1
                continue
            if "No fan status data" in err_msg: # Added for fan data
                summary_messages["No fan status data found"] += 1
                continue
            if "No valid generic CPU data found" in err_msg:
                summary_messages["No valid generic CPU data found"] += 1
                continue
            if "No VLAN data found" in err_msg:
                summary_messages["No VLAN data found"] += 1
                continue
            if "No inventory descriptions found" in err_msg:
                summary_messages["No inventory descriptions found"] += 1
                continue
            if "No inventory names found" in err_msg:
                summary_messages["No inventory names found"] += 1
                continue
            if "No inventory serials found" in err_msg:
                summary_messages["No inventory serials found"] += 1
                continue

        active_alarms.append({"type": "API Error", "message": err_msg, "severity": error_severity, "identifier": f"api_error_{len(active_alarms)}"})
        current_highest_rank = max(current_highest_rank, severity_rank[error_severity])


    hardware_health = switch_details.get("hardware_health", {})

    # Evaluate CPU Utilization (only if data is present from "sensors" scope)
    if hardware_health.get("cpu_utilization") != "N/A":
        cpu_util = hardware_health.get("cpu_utilization")
        try:
            cpu_percent = float(str(cpu_util).replace('%', '')) # Ensure it's a string before replace
            if cpu_percent > 90: # High CPU
                active_alarms.append({"type": "CPU", "message": f"CPU Utilization HIGH: {cpu_util}", "severity": "CRITICAL", "identifier": "cpu"})
                current_highest_rank = max(current_highest_rank, severity_rank["CRITICAL"])
            elif cpu_percent > 70: # Warning CPU
                active_alarms.append({"type": "CPU", "message": f"CPU Utilization WARNING: {cpu_util}", "severity": "WARNING", "identifier": "cpu"})
                current_highest_rank = max(current_highest_rank, severity_rank["WARNING"])
        except ValueError:
            active_alarms.append({"type": "CPU", "message": f"Invalid CPU data: {cpu_util}", "severity": "ERROR", "identifier": "cpu"})
            current_highest_rank = max(current_highest_rank, severity_rank["ERROR"])


    # Evaluate Memory Utilization (only if data is present from "sensors" scope)
    if hardware_health.get("memory_utilization") != "N/A":
        mem_util = hardware_health.get("memory_utilization")
        try:
            mem_percent = float(str(mem_util).replace('%', '')) # Ensure it's a string before replace
            if mem_percent > 90: # High Memory
                active_alarms.append({"type": "Memory", "message": f"Memory Utilization HIGH: {mem_util}", "severity": "CRITICAL", "identifier": "memory"})
                current_highest_rank = max(current_highest_rank, severity_rank["CRITICAL"])
            elif mem_percent > 70: # Warning Memory
                active_alarms.append({"type": "Memory", "message": f"Memory Utilization WARNING: {mem_util}", "severity": "WARNING", "identifier": "memory"})
                current_highest_rank = max(current_highest_rank, severity_rank["WARNING"])
        except ValueError:
            active_alarms.append({"type": "Memory", "message": f"Invalid Memory data: {mem_util}", "severity": "ERROR", "identifier": "memory"})
            current_highest_rank = max(current_highest_rank, severity_rank["ERROR"])

    # Evaluate Power Supply Status (only if data is present from "sensors" or "critical" scope)
    for ps in hardware_health.get("power_supply_status", []):
        if ps.get("status") in ["Critical", "Shutdown", "Not Functioning"]:
            active_alarms.append({"type": "Power Supply", "message": f"Power Supply {ps.get('index')} Status: {ps.get('status')}", "severity": "CRITICAL", "identifier": f"ps_{ps.get('index')}"})
            current_highest_rank = max(current_highest_rank, severity_rank["CRITICAL"])
        elif ps.get("status") == "Warning":
            active_alarms.append({"type": "Power Supply", "message": f"Power Supply {ps.get('index')} Status: {ps.get('status')}", "severity": "WARNING", "identifier": f"ps_{ps.get('index')}"})
            current_highest_rank = max(current_highest_rank, severity_rank["WARNING"])

    # Evaluate Temperature Sensors (only if data is present from "sensors" scope)
    for temp_s in hardware_health.get("temperature_sensors", []):
        try:
            temp_value = temp_s.get("temperature")
            if isinstance(temp_value, (int, float)):
                if temp_value > 70:
                    active_alarms.append({"type": "Temperature", "message": f"Temp Sensor {temp_s.get('index')} Critical: {temp_value} {temp_s.get('unit', '')}", "severity": "CRITICAL", "identifier": f"temp_{temp_s.get('index')}"})
                    current_highest_rank = max(current_highest_rank, severity_rank["CRITICAL"])
                elif temp_value > 60:
                    active_alarms.append({"type": "Temperature", "message": f"Temp Sensor {temp_s.get('index')} Warning: {temp_value} {temp_s.get('unit', '')}", "severity": "WARNING", "identifier": f"temp_{temp_s.get('index')}"})
                    current_highest_rank = max(current_highest_rank, severity_rank["WARNING"])
        except Exception: # Handle non-numeric temperature values or missing keys
            active_alarms.append({"type": "Temperature", "message": f"Invalid Temp Sensor {temp_s.get('index')} data: {temp_s.get('temperature')}", "severity": "ERROR", "identifier": f"temp_{temp_s.get('index')}"})
            current_highest_rank = max(current_highest_rank, severity_rank["ERROR"])

    # Evaluate Fan Status (only if data is present from "sensors" scope)
    for fan in hardware_health.get("fan_status", []):
        if fan.get("status") in ["Critical", "Shutdown", "Not Functioning"]:
            active_alarms.append({"type": "Fan", "message": f"Fan {fan.get('index')} Status: {fan.get('status')}", "severity": "CRITICAL", "identifier": f"fan_{fan.get('index')}"})
            current_highest_rank = max(current_highest_rank, severity_rank["CRITICAL"])
        elif fan.get("status") == "Warning":
            active_alarms.append({"type": "Fan", "message": f"Fan {fan.get('index')} Status: {fan.get('status')}", "severity": "WARNING", "identifier": f"fan_{fan.get('index')}"})
            current_highest_rank = max(current_highest_rank, severity_rank["WARNING"])

    # Evaluate Interface Status (oper_status "down" and not admin_down) (only if data is present from "critical" scope)
    na_oper_status_count = 0
    for iface in switch_details.get("interfaces", []):
        # The switch_api_client already filters out admin_down ports.
        # So, if we see oper_status "down" here, it's a real issue.
        # Alarm if operational status is 'down' or similar problem states
        if iface.get("oper_status") in ["down", "lowerLayerDown"]:
            # Use hostname_interfaceName as alarm_identifier
            active_alarms.append({"type": "Interface", "message": f"Interface {iface.get('name')} is DOWN", "severity": "CRITICAL", "identifier": f"{hostname}_{iface.get('name')}"})
            current_highest_rank = max(current_highest_rank, severity_rank["CRITICAL"])
        elif iface.get("oper_status") in ["unknown", "notPresent"]:
            active_alarms.append({"type": "Interface", "message": f"Interface {iface.get('name')} Status: {iface.get('oper_status')}", "severity": "WARNING", "identifier": f"{hostname}_{iface.get('name')}"})
            current_highest_rank = max(current_highest_rank, severity_rank["WARNING"])
        # If operational status is 'N/A' (due to parsing issues or missing data), treat as WARNING
        elif iface.get("oper_status") == "N/A":
             na_oper_status_count += 1
             current_highest_rank = max(current_highest_rank, severity_rank["WARNING"]) # Still contributes to overall warning


    if na_oper_status_count > 0:
        summary_messages[f"{na_oper_status_count} interfaces reported N/A operational status"] += 1

    # Compile overall message from active alarms and summary messages
    detailed_alarm_messages = []
    for alarm in active_alarms:
        # Only include messages that are not meant for summary or are actual critical issues
        # The API errors related to "No data found" are summarized separately.
        is_summarized_api_error = False
        if "API Error" in alarm["type"]:
            if any(keyword in alarm["message"] for keyword in ["No Such Object", "OID_NOT_FOUND", "NO_RESPONSE", "No data found"]):
                is_summarized_api_error = True
        
        if not is_summarized_api_error:
            detailed_alarm_messages.append(alarm["message"])
    
    for msg, count in summary_messages.items():
        if count > 0: # Ensure we only add if there's at least one instance
            detailed_alarm_messages.append(msg if count == 1 else f"{msg} ({count} instances)")

    # Determine overall severity and status based on highest rank
    if current_highest_rank == severity_rank["CRITICAL"]:
        overall_severity = "CRITICAL"
        overall_status = "alarm"
        if detailed_alarm_messages:
            overall_message = "Critical alarms detected: " + ", ".join(detailed_alarm_messages)
        else:
            overall_message = f"Switch {switch_details.get('ip')} is in a CRITICAL state with unspecified issues."
    elif current_highest_rank == severity_rank["MAJOR"]:
        overall_severity = "MAJOR"
        overall_status = "alarm"
        if detailed_alarm_messages:
            overall_message = "Major alarms detected: " + ", ".join(detailed_alarm_messages)
        else:
            overall_message = f"Switch {switch_details.get('ip')} is in a MAJOR state with unspecified issues."
    elif current_highest_rank == severity_rank["WARNING"]:
        overall_severity = "WARNING"
        overall_status = "warning"
        if detailed_alarm_messages:
            overall_message = "Warnings detected: " + ", ".join(detailed_alarm_messages)
        else:
            overall_message = f"Switch {switch_details.get('ip')} is in a WARNING state with unspecified issues."
    elif current_highest_rank == severity_rank["ERROR"]: # If only ERRORs and no higher severity
        overall_severity = "ERROR"
        overall_status = "error"
        if detailed_alarm_messages:
            overall_message = "Errors detected: " + ", ".join(detailed_alarm_messages)
        else:
            overall_message = f"Switch {switch_details.get('ip')} is in an ERROR state with unspecified issues."
    else:
        # If no alarms, ensure message indicates OK.
        overall_message = f"Switch {switch_details.get('ip')} status is OK."


    return overall_status, overall_severity, overall_message, active_alarms

async def _get_all_switch_configs():
    """Fetches switch configurations from MySQL, handling reconnection logic."""
    if mysql_manager and (not mysql_manager.connection or not mysql_manager.connection.is_connected()):
        try:
            mysql_manager.reconnect()
            logging.info("Switch Config Agent: Successfully reconnected to MySQL.")
        except Exception as e:
            logging.error(f"Switch Config Agent: Failed to reconnect to MySQL: {e}. Cannot fetch switch configurations.", exc_info=True)
            return None
    elif not mysql_manager:
        logging.error("Switch Config Agent: MySQLManager not initialized. Cannot fetch switch configurations.")
        return None

    if not mysql_manager.connection or not mysql_manager.connection.is_connected():
        logging.error("Switch Config Agent: MySQLManager not connected after all attempts. Cannot fetch switch configurations.")
        return None

    switch_configs = mysql_manager.get_switch_configs()

    if switch_configs is None:
        logging.error("Switch Config Agent: Received None for switch configurations from MySQLManager. Assuming database error or no data.")
        return [] # Return empty list to prevent iteration errors

    if not switch_configs:
        logging.warning("Switch Config Agent: No switch configurations found in MySQL database. Skipping polling cycle.")
        return []

    return switch_configs

async def process_switch_config(config: Dict[str, Any], data_scope: str) -> Optional[Dict[str, Any]]:
    """
    Processes a single switch configuration, fetches details via SNMP (asynchronously)
    based on data_scope, publishes the consolidated status to Redis for historical,
    and sends overall status changes directly to WebSocket.

    Returns the switch_details_data if data_scope is "critical", otherwise returns None.
    """
    switch_ip = config.get('switch_ip')
    community = config.get('community')
    model = config.get('model')
    hostname = config.get('hostname', f"Switch Device {switch_ip}")
    device_name = hostname # Define device_name here

    # Consistent frontend_block_id for the device type for overall status notifications
    base_frontend_block_id = f"G.{model.upper()}SW" if model else "G.UNKNOWN_SW"

    # Determine the Redis stream name based on the switch model
    if model and model.lower() == 'cisco':
        target_redis_stream = CISCO_STREAM_NAME
    elif model and model.lower() == 'nexus':
        target_redis_stream = NEXUS_STREAM_NAME
    else:
        logging.warning(f"Unknown switch model '{model}' for IP {switch_ip}. Skipping processing.")
        return None # Return None for unknown models

    if not switch_ip or not community or not model:
        logging.error(f"Skipping switch config due to missing IP, community, or model: {config}")
        return None # Return None for incomplete config

    logging.info(f"Processing {data_scope} data for IP: {switch_ip} (Model: {model})")

    api_errors = []
    switch_details_data = {}
    active_individual_alarms = [] # Alarms for individual components (ports, CPU, etc.)

    try:
        # Fetch data based on the data_scope
        switch_details_data, errors_from_client = await switch_api_client.get_switch_details(
            switch_ip, community, model, data_scope=data_scope
        )
        api_errors.extend(errors_from_client)

        overall_status, overall_severity, overall_message, active_individual_alarms = \
            get_overall_status_and_message(switch_details_data, api_errors, hostname)

    except Exception as e:
        logging.error(f"Error fetching switch SNMP data for {switch_ip} ({data_scope}): {e}", exc_info=True)
        api_errors.append(f"Unhandled error during SNMP data fetch ({data_scope}): {e}")
        overall_status = "error"
        overall_severity = "ERROR"
        overall_message = f"Unhandled error fetching {data_scope} data for {switch_ip}: {e}"
        switch_details_data = {} # Clear details if unhandled error

    # --- Handle Overall Device Status Changes and Send WebSocket Notification (AGENT RESPONSIBILITY) ---
    # Fetch last known overall status from Redis for comparison
    last_overall_status_json = r.hget(SWITCH_OVERALL_STATUS_TRACKING_KEY, switch_ip)
    last_overall_status = json.loads(last_overall_status_json.decode('utf-8')) if last_overall_status_json else {"status": "UNKNOWN", "severity": "UNKNOWN"}

    # Severity order for comparison
    severity_order = {
        'INFO': 0, 'OK': 0, 'NORMAL': 0, 'CLEARED': 0,
        'WARNING': 1, 'MINOR': 1,
        'MAJOR': 2, 'ALARM': 2,
        'CRITICAL': 3, 'ERROR': 3,
        'UNKNOWN': -1
    }

    is_current_alarming = severity_order.get(overall_severity, 0) > severity_order['OK']
    is_previous_alarming = severity_order.get(last_overall_status['severity'], 0) > severity_order['OK']

    overall_status_changed_for_ws = False
    ws_type_for_overall_status = "NO_CHANGE"
    ws_message_for_overall_status = overall_message # Default to agent's message

    if is_current_alarming and not is_previous_alarming:
        # Transitioned from OK to alarming
        overall_status_changed_for_ws = True
        ws_type_for_overall_status = "DEVICE_ALARM"
        ws_message_for_overall_status = f"Device {device_name} is now in a {overall_severity} state. Details: {overall_message}"
        logging.info(f"AGENT: OVERALL DEVICE ALARM for {switch_ip}: {overall_severity} - {ws_message_for_overall_status}")
    elif not is_current_alarming and is_previous_alarming:
        # Transitioned from alarming to OK/cleared
        overall_status_changed_for_ws = True
        ws_type_for_overall_status = "DEVICE_CLEARED"
        ws_message_for_overall_status = f"Device {device_name} is now OK. All previous alarms cleared."
        logging.info(f"AGENT: OVERALL DEVICE CLEARED for {switch_ip}.")
    elif is_current_alarming and is_previous_alarming and \
         (overall_severity != last_overall_status['severity'] or overall_message != last_overall_status['message']):
        # Alarm status still active, but severity or message changed
        overall_status_changed_for_ws = True
        ws_type_for_overall_status = "DEVICE_ALARM_UPDATE"
        ws_message_for_overall_status = f"Device {device_name} is still in an alarmed state (Severity: {overall_severity}). Details: {overall_message}"
        logging.info(f"AGENT: OVERALL DEVICE ALARM UPDATED for {switch_ip}: Old Severity:{last_overall_status['severity']}, New Severity:{overall_severity}")

    if overall_status_changed_for_ws:
        websocket_message_overall = {
            "device_name": device_name,
            "device_ip": switch_ip,
            "time": datetime.utcnow().isoformat() + "Z",
            "message": ws_message_for_overall_status,
            "severity": overall_severity if ws_type_for_overall_status != "DEVICE_CLEARED" else "OK", # Send OK severity for cleared
            "status": overall_status if ws_type_for_overall_status != "DEVICE_CLEARED" else "OK", # Send OK status for cleared
            "frontend_block_id": base_frontend_block_id, # This is the G.CISCOSW or G.NEXUSSW block
            "group_name": f"{model.upper()} Switch Group", # Generic group name
            "type": ws_type_for_overall_status, # DEVICE_ALARM, DEVICE_CLEARED, DEVICE_ALARM_UPDATE
            "alarm_type": "Overall Device Status",
            "alarm_identifier": "overall"
        }
        await send_websocket_notification(websocket_message_overall)
    
    # Always update the last known overall status in Redis for the agent's tracking
    r.hset(SWITCH_OVERALL_STATUS_TRACKING_KEY, switch_ip, json.dumps({
        "status": overall_status,
        "severity": overall_severity,
        "message": overall_message,
        "timestamp": datetime.utcnow().isoformat() + "Z"
    }))


    # --- Publish full data and individual alarms to Redis Stream for CONSUMER (for historical storage) ---
    redis_payload = {
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "agent_type": f"switch_config_{data_scope}", # Distinguish agent type for different polls
        "device_ip": switch_ip,
        "device_name": hostname,
        "frontend_block_id": base_frontend_block_id, # Consistent frontend_block_id for the device type
        "group_name": f"{model.upper()} Switch Group", # Generic group name
        "overall_status": overall_status, # Still send overall status for consumer to archive
        "overall_severity": overall_severity,
        "overall_message": overall_message,
        "details": {
            "full_switch_details": switch_details_data, # Send all raw details to ES
            "active_individual_alarms": active_individual_alarms, # Individual alarms for consumer to assess
            "api_errors": api_errors
        }
    }

    # Publish to the model-specific Redis stream
    publish_status_to_redis(target_redis_stream, redis_payload)
    logging.debug(f"Finished processing {data_scope} data for IP: {switch_ip}")

    if data_scope == "critical":
        return switch_details_data
    return None


async def _poll_critical_data_loop():
    """Polls critical switch data (interfaces, power supply) at the defined interval."""
    logging.info("Starting critical data polling loop.")
    while True:
        logging.info(f"Entering critical data polling cycle. Next cycle in {CRITICAL_POLLING_INTERVAL_SECONDS} seconds.")
        switch_configs = await _get_all_switch_configs()
        if switch_configs:
            critical_tasks = [process_switch_config(config, "critical") for config in switch_configs]
            
            # Execute tasks and get results
            # The processed_results are no longer used for printing a table,
            # but still useful if other logic needs access to the returned switch_details_data.
            await asyncio.gather(*critical_tasks) 

            logging.info(f"Completed {len(critical_tasks)} tasks for critical data cycle.")

            # Removed the table printing logic from here.
            # for switch_data in processed_results:
            #     if switch_data and switch_data.get("interfaces"):
            #         ip = switch_data.get("ip", "N/A")
            #         hostname = switch_data.get("system_info", {}).get("hostname", "N/A")
            #         
            #         logging.info(f"\n--- Interface Status Table for {hostname} ({ip}) ---")
            #         print(f"{'Interface Name':<30} | {'Index':<8} | {'Admin Status':<15} | {'Oper Status':<15}")
            #         print("-" * 70)
            #
            #         for iface in switch_data["interfaces"]:
            #             name = iface.get("name", "N/A")
            #             index = iface.get("index", "N/A")
            #             admin_status = iface.get("admin_status", "N/A")
            #             oper_status = iface.get("oper_status", "N/A")
            #             print(f"{name:<30} | {index:<8} | {admin_status:<15} | {oper_status:<15}")
            #         print("\n")
            #     else:
            #         logging.info(f"No interface data found for a switch after critical data processing.")


        else:
            logging.warning("No switch configurations to process for critical data cycle.")
        logging.info(f"Agent sleeping for {CRITICAL_POLLING_INTERVAL_SECONDS} seconds before next critical data cycle...")
        await asyncio.sleep(CRITICAL_POLLING_INTERVAL_SECONDS)

async def _poll_sensor_data_loop():
    """Polls full hardware sensor data (CPU, memory, temperature) at the defined interval."""
    logging.info("Starting sensor data polling loop.")
    while True:
        logging.info(f"Entering sensor data polling cycle. Next cycle in {SENSOR_POLLING_INTERVAL_SECONDS} seconds.")
        switch_configs = await _get_all_switch_configs()
        if switch_configs:
            sensor_tasks = [process_switch_config(config, "sensors") for config in switch_configs]
            await asyncio.gather(*sensor_tasks) # No need to capture results for printing here
            logging.info(f"Completed {len(sensor_tasks)} tasks for sensor data cycle.")
        else:
            logging.warning("No switch configurations to process for sensor data cycle.")
        logging.info(f"Agent sleeping for {SENSOR_POLLING_INTERVAL_SECONDS} seconds before next sensor data cycle...")
        await asyncio.sleep(SENSOR_POLLING_INTERVAL_SECONDS)


async def main_agent():
    logging.info("Switch Config Agent starting main polling loops.")
    await initialize_http_session() # Initialize HTTP session for WS notifications
    # Run both polling loops concurrently
    await asyncio.gather(
        _poll_critical_data_loop(),
        _poll_sensor_data_loop()
    )


if __name__ == "__main__":
    if sys.platform.startswith('win'):
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    try:
        logging.debug("asyncio.run(main_agent()) called.")
        asyncio.run(main_agent())
    except KeyboardInterrupt:
        logging.info("Switch Config Agent stopped by user (KeyboardInterrupt).")
        sys.exit(0)
    except Exception as e:
        logging.critical(f"Switch Config Agent terminated due to an unhandled error: {e}", exc_info=True)
        sys.exit(1)
    finally:
        asyncio.run(close_http_session()) # Ensure session is closed on exit
