import asyncio
import aiohttp
import json
import logging
import os
import redis
from datetime import datetime
from elasticsearch import AsyncElasticsearch
from elasticsearch.helpers import async_bulk # For bulk indexing
import uuid # For generating unique IDs for ES documents
from urllib.parse import quote as url_quote # Import url_quote from urllib.parse

# --- Logging Configuration ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- Configuration ---
REDIS_HOST = os.environ.get('REDIS_HOST', '192.168.56.30')
REDIS_PORT = int(os.environ.get('REDIS_PORT', 6379))

ES_HOST = os.environ.get('ES_HOST', '192.168.56.30')
ES_PORT = int(os.environ.get('ES_PORT', 9200))
ES_INDEX_NAME = "monitor_switch_status" # Elasticsearch index name

PROMETHEUS_URL = os.environ.get('PROMETHEUS_URL', 'http://192.168.56.30:9090')
WEBSOCKET_NOTIFIER_URL = os.environ.get('WEBSOCKET_NOTIFIER_URL', 'http://127.0.0.1:8001/notify')

POLLING_INTERVAL_SECONDS = 60 # Poll Prometheus every 60 seconds

# --- Alarm Thresholds ---
CPU_THRESHOLD_PERCENT = 40.0
TEMP_THRESHOLD_CELSIUS = 50.0 # Default temperature threshold in Celsius

# Initialize Redis client (can be used for state management if needed later)
try:
    r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=0, decode_responses=False)
    r.ping()
    logging.info("Successfully connected to Redis.")
except redis.exceptions.ConnectionError as e:
    logging.critical(f"FATAL: Failed to connect to Redis at {REDIS_HOST}:{REDIS_PORT}. Error: {e}")
    # Exit if Redis is critical for operation, otherwise log and continue
except Exception as e:
    logging.critical(f"FATAL: Unexpected error during Redis connection test: {e}", exc_info=True)

# Initialize Elasticsearch client
es = AsyncElasticsearch(f"http://{ES_HOST}:{ES_PORT}")

# Async HTTP session for Prometheus and WebSocket notifier communication
http_session = None

async def get_http_session():
    """Ensures a single aiohttp ClientSession is used."""
    global http_session
    if http_session is None or http_session.closed:
        # Explicitly set ssl=False for the connector if Prometheus/Notifier are HTTP
        http_session = aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=False))
    return http_session

async def send_websocket_notification(message_data):
    """Sends a JSON message to the WebSocket notifier's HTTP endpoint."""
    session = await get_http_session()
    try:
        async with session.post(WEBSOCKET_NOTIFIER_URL, json=message_data) as response:
            if response.status != 200:
                logging.error(f"Failed to send WebSocket notification: {response.status} - {await response.text()}")
            else:
                logging.info(f"WebSocket notification sent successfully for frontend_block_id: {message_data.get('frontend_block_id')}")
    except aiohttp.ClientConnectionError as e:
        logging.error(f"WebSocket notifier connection error: {e}. Is the notifier running at {WEBSOCKET_NOTIFIER_URL}?")
    except Exception as e:
        logging.error(f"Error sending WebSocket notification: {e}", exc_info=True)

async def query_prometheus(query):
    """Performs an asynchronous query against the Prometheus API."""
    session = await get_http_session()
    encoded_query = url_quote(query) # Changed to url_quote from urllib.parse
    url = f"{PROMETHEUS_URL}/api/v1/query?query={encoded_query}"
    try:
        async with session.get(url) as response:
            if response.status != 200:
                error_body = await response.text()
                raise Exception(f"Prometheus HTTP error: {response.status} - {error_body}")
            return await response.json()
    except aiohttp.ClientConnectionError as e:
        raise Exception(f"Prometheus connection error: {e}. Is Prometheus running at {PROMETHEUS_URL}?")
    except Exception as e:
        logging.error(f"Error querying Prometheus for query \"{query}\": {e}", exc_info=True)
        raise

def get_prometheus_metric_data(prometheus_result, label_key=None, default_value='N/A'):
    """
    Extracts metric data from Prometheus query result.
    If label_key is provided, it returns a dictionary of {label_value: value}.
    Otherwise, it returns a single value (for queries expected to return one result).
    """
    if not prometheus_result or prometheus_result.get('status') != 'success' or not prometheus_result['data']['result']:
        return default_value

    if label_key:
        data = {}
        for item in prometheus_result['data']['result']:
            if label_key in item['metric']:
                value = float(item['value'][1])
                data[item['metric'][label_key]] = value
        return data
    else:
        # For single-value metrics like CPU, Uptime, Memory
        value = float(prometheus_result['data']['result'][0]['value'][1])
        return value

async def process_switch_data(switch_config):
    """
    Fetches all relevant data for a single switch from Prometheus,
    formats it, and sends it to Elasticsearch.
    """
    hostname = switch_config.get('hostname')
    switch_ip = switch_config.get('switch_ip') # This is agent_host in Prometheus
    model = switch_config.get('model', 'Unknown') # Example for model, if you have it elsewhere

    if not hostname or not switch_ip:
        logging.error(f"Skipping switch due to missing hostname or IP: {switch_config}")
        await send_websocket_notification({
            "frontend_block_id": "G.CISCOSW",
            "message": f"Skipped processing switch due to missing hostname or IP: {switch_config}",
            "severity": "ERROR",
            "status": "ERROR",
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "type": "BACKEND_ERROR"
        })
        return

    logging.info(f"Processing data for switch: {hostname} ({switch_ip})")

    # --- Prometheus Queries ---
    queries = {
        "uptime": f'cisco_snmp_uptime{{hostname="{hostname}"}} / (60*60*24)',
        "cpu_utilization": f'cisco_snmp_cpu_5min{{hostname="{hostname}"}}',
        "mem_used": f'cisco_snmp_mem_used{{hostname="{hostname}"}}',
        "mem_free": f'cisco_snmp_mem_free{{hostname="{hostname}"}}',
        "admin_status": f'interfaces_ifAdminStatus{{agent_host="{switch_ip}",job="telegraf_snmp"}}',
        "oper_status": f'interfaces_ifOperStatus{{agent_host="{switch_ip}",job="telegraf_snmp"}}',
        "if_speed": f'interfaces_ifSpeed{{agent_host="{switch_ip}",job="telegraf_snmp"}}',
        "if_in_discards": f'interfaces_ifInDiscards{{agent_host="{switch_ip}",job="telegraf_snmp"}}',
        "if_in_errors": f'interfaces_ifInErrors{{agent_host="{switch_ip}",job="telegraf_snmp"}}',
        "if_in_nucast_pkts": f'interfaces_ifInNUcastPkts{{agent_host="{switch_ip}",job="telegraf_snmp"}}',
        "if_in_octets": f'interfaces_ifInOctets{{agent_host="{switch_ip}",job="telegraf_snmp"}}',
        "if_in_ucast_pkts": f'interfaces_ifInUcastPkts{{agent_host="{switch_ip}",job="telegraf_snmp"}}',
        "if_in_unknown_protos": f'interfaces_ifInUnknownProtos{{agent_host="{switch_ip}",job="telegraf_snmp"}}',
        "if_last_change": f'interfaces_ifLastChange{{agent_host="{switch_ip}",job="telegraf_snmp"}}',
        "if_mtu": f'interfaces_ifMtu{{agent_host="{switch_ip}",job="telegraf_snmp"}}',
        "if_out_discards": f'interfaces_ifOutDiscards{{agent_host="{switch_ip}",job="telegraf_snmp"}}',
        "if_out_errors": f'interfaces_ifOutErrors{{agent_host="{switch_ip}",job="telegraf_snmp"}}',
        "if_out_nucast_pkts": f'interfaces_ifOutNUcastPkts{{agent_host="{switch_ip}",job="telegraf_snmp"}}',
        "if_out_octets": f'interfaces_ifOutOctets{{agent_host="{switch_ip}",job="telegraf_snmp"}}',
        "if_out_qlen": f'interfaces_ifOutQLen{{agent_host="{switch_ip}",job="telegraf_snmp"}}',
        "if_out_ucast_pkts": f'interfaces_ifOutUcastPkts{{agent_host="{switch_ip}",job="telegraf_snmp"}}',
        "if_type": f'interfaces_ifType{{agent_host="{switch_ip}",job="telegraf_snmp"}}',
        "if_descr": f'interfaces_ifDescr{{agent_host="{switch_ip}",job="telegraf_snmp"}}',
        "if_alias": f'interfaces_ifAlias{{agent_host="{switch_ip}",job="telegraf_snmp"}}',
        "temperature_sensors": f'cisco_temperature_table_temp_sensor_value{{agent_host="{switch_ip}",job="telegraf_snmp"}}',
        # Advanced query to get problem interfaces with their descriptive labels
        "problem_interface_details": f'label_replace((interfaces_ifAdminStatus{{agent_host="{switch_ip}",job="telegraf_snmp"}} == 1 and interfaces_ifOperStatus{{agent_host="{switch_ip}",job="telegraf_snmp"}} == 2) * on (ifIndex, agent_host) group_left(ifType, ifDescr, ifAlias) interfaces_ifType{{job="telegraf_snmp"}}, "status", "AdminUp_OperDown", "", "")'
    }

    results = {}
    for key, query in queries.items():
        try:
            results[key] = await query_prometheus(query)
        except Exception as e:
            logging.error(f"Failed to query Prometheus for {key} for {hostname}: {e}")
            results[key] = None # Mark as None if query fails

    # --- Extract and Format Data ---
    uptime = get_prometheus_metric_data(results.get("uptime"), default_value=None)
    cpu_utilization = get_prometheus_metric_data(results.get("cpu_utilization"), default_value=None)
    mem_used = get_prometheus_metric_data(results.get("mem_used"), default_value=None)
    mem_free = get_prometheus_metric_data(results.get("mem_free"), default_value=None)

    memory_utilization = None
    if mem_used is not None and mem_free is not None and (mem_used + mem_free) > 0:
        memory_utilization = (mem_used / (mem_used + mem_free)) * 100

    # Process interfaces
    interfaces_data = {}
    
    # Group interface metrics by ifIndex
    all_interface_metrics = [
        results.get("admin_status"), results.get("oper_status"), results.get("if_speed"),
        results.get("if_in_discards"), results.get("if_in_errors"), results.get("if_in_nucast_pkts"),
        results.get("if_in_octets"), results.get("if_in_ucast_pkts"), results.get("if_in_unknown_protos"),
        results.get("if_last_change"), results.get("if_mtu"), 
        results.get("if_out_discards"), results.get("if_out_errors"), results.get("if_out_nucast_pkts"),
        results.get("if_out_octets"), results.get("if_out_qlen"), results.get("if_out_ucast_pkts"),
        results.get("if_type"), results.get("if_descr"), results.get("if_alias")
    ]

    for prom_result in all_interface_metrics:
        if prom_result and prom_result.get('status') == 'success' and prom_result['data']['result']:
            for item in prom_result['data']['result']:
                ifIndex = item['metric'].get('ifIndex')
                if not ifIndex: continue

                if ifIndex not in interfaces_data:
                    interfaces_data[ifIndex] = {
                        "ifIndex": int(ifIndex),
                        "name": item['metric'].get('ifDescr') or item['metric'].get('ifAlias') or 'N/A', # Default name
                        "ifDescr": item['metric'].get('ifDescr', 'N/A'),
                        "ifAlias": item['metric'].get('ifAlias', 'N/A'),
                        "ifType": int(item['metric'].get('ifType', 0)), # ifType is numerical
                        "ifMtu": None, "ifSpeed": None, "ifAdminStatus": None, "ifOperStatus": None,
                        "ifLastChange": None, "ifInOctets": None, "ifInUcastPkts": None,
                        "ifInDiscards": None, "ifInErrors": None, "ifInUnknownProtos": None,
                        "ifInNUcastPkts": None,
                        "ifOutOctets": None, "ifOutUcastPkts": None, "ifOutDiscards": None, "ifOutErrors": None,
                        "ifOutNUcastPkts": None,
                        "ifOutQLen": None
                    }
                
                metric_name = item['metric'].get('__name__')
                # Try converting to float, but keep original if not a valid number
                value = float(item['value'][1]) if item['value'][1].replace('.', '', 1).isdigit() else item['value'][1]

                # Map Prometheus metric names to schema fields
                if metric_name == 'interfaces_ifAdminStatus': interfaces_data[ifIndex]['ifAdminStatus'] = int(value)
                elif metric_name == 'interfaces_ifOperStatus': interfaces_data[ifIndex]['ifOperStatus'] = int(value)
                elif metric_name == 'interfaces_ifSpeed': interfaces_data[ifIndex]['ifSpeed'] = int(value)
                elif metric_name == 'interfaces_ifInDiscards': interfaces_data[ifIndex]['ifInDiscards'] = int(value)
                elif metric_name == 'interfaces_ifInErrors': interfaces_data[ifIndex]['ifInErrors'] = int(value)
                elif metric_name == 'interfaces_ifInNUcastPkts': interfaces_data[ifIndex]['ifInNUcastPkts'] = int(value)
                elif metric_name == 'interfaces_ifInOctets': interfaces_data[ifIndex]['ifInOctets'] = int(value)
                elif metric_name == 'interfaces_ifInUcastPkts': interfaces_data[ifIndex]['ifInUcastPkts'] = int(value)
                elif metric_name == 'interfaces_ifInUnknownProtos': interfaces_data[ifIndex]['ifInUnknownProtos'] = int(value)
                elif metric_name == 'interfaces_ifLastChange': interfaces_data[ifIndex]['ifLastChange'] = datetime.fromtimestamp(value).isoformat() + "Z"
                elif metric_name == 'interfaces_ifMtu': interfaces_data[ifIndex]['ifMtu'] = int(value)
                elif metric_name == 'interfaces_ifOutDiscards': interfaces_data[ifIndex]['ifOutDiscards'] = int(value)
                elif metric_name == 'interfaces_ifOutErrors': interfaces_data[ifIndex]['ifOutErrors'] = int(value)
                elif metric_name == 'interfaces_ifOutNUcastPkts': interfaces_data[ifIndex]['ifOutNUcastPkts'] = int(value)
                elif metric_name == 'interfaces_ifOutOctets': interfaces_data[ifIndex]['ifOutOctets'] = int(value)
                elif metric_name == 'interfaces_ifOutQLen': interfaces_data[ifIndex]['ifOutQLen'] = int(value)
                elif metric_name == 'interfaces_ifOutUcastPkts': interfaces_data[ifIndex]['ifOutUcastPkts'] = int(value)
                # ifDescr and ifAlias are handled during initial ifIndex creation, if they exist as labels

    final_interfaces_details = list(interfaces_data.values())

    # Determine overall_interface_status and problem_interfaces using the advanced query result
    overall_interface_status = "OK"
    problem_interfaces_list = []
    
    problem_if_result = results.get("problem_interface_details")
    if problem_if_result and problem_if_result.get('status') == 'success' and problem_if_result['data']['result']:
        overall_interface_status = "Problem"
        for item in problem_if_result['data']['result']:
            metric = item['metric']
            # Prioritize alias, then description, then type, then index
            problem_name = metric.get('ifAlias') or metric.get('ifDescr') or metric.get('ifType') or f"Index: {metric.get('ifIndex', 'N/A')}"
            problem_interfaces_list.append(problem_name)

            # --- ALARM: Interface Admin Up / Oper Down ---
            await send_websocket_notification({
                "frontend_block_id": "G.CISCOSW",
                "message": f"Interface PROBLEM: {problem_name} on {hostname} ({switch_ip}) is Admin Up but Oper Down.",
                "severity": "CRITICAL",
                "status": "INTERFACE_DOWN",
                "timestamp": datetime.utcnow().isoformat() + "Z",
                "type": "ALARM",
                "details": {
                    "hostname": hostname,
                    "ip": switch_ip,
                    "interface_name": problem_name,
                    "ifIndex": metric.get('ifIndex', 'N/A'),
                    "admin_status": "Up",
                    "oper_status": "Down"
                }
            })


    # Process temperature sensors
    temperature_sensors_data = []
    temp_result = results.get("temperature_sensors")
    if temp_result and temp_result.get('status') == 'success' and temp_result['data']['result']:
        for item in temp_result['data']['result']:
            sensor_index = int(item['metric'].get('index'))
            sensor_description = item['metric'].get('temp_sensor_descr', 'N/A')
            sensor_value = float(item['value'][1])

            temperature_sensors_data.append({
                "index": sensor_index,
                "description": sensor_description,
                "value": sensor_value
            })

            # --- ALARM: High Temperature ---
            if sensor_value > TEMP_THRESHOLD_CELSIUS:
                await send_websocket_notification({
                    "frontend_block_id": "G.CISCOSW",
                    "message": f"TEMPERATURE ALERT: {sensor_description} on {hostname} ({switch_ip}) is {sensor_value:.2f}°C (threshold: {TEMP_THRESHOLD_CELSIUS}°C).",
                    "severity": "WARNING",
                    "status": "HIGH_TEMPERATURE",
                    "timestamp": datetime.utcnow().isoformat() + "Z",
                    "type": "ALARM",
                    "details": {
                        "hostname": hostname,
                        "ip": switch_ip,
                        "sensor_description": sensor_description,
                        "temperature": sensor_value
                    }
                })

    # --- ALARM: High CPU Utilization ---
    if cpu_utilization is not None and cpu_utilization > CPU_THRESHOLD_PERCENT:
        await send_websocket_notification({
            "frontend_block_id": "G.CISCOSW",
            "message": f"CPU UTILIZATION ALERT: {hostname} ({switch_ip}) CPU is at {cpu_utilization:.2f}% (threshold: {CPU_THRESHOLD_PERCENT}%).",
            "severity": "WARNING",
            "status": "HIGH_CPU",
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "type": "ALARM",
            "details": {
                "hostname": hostname,
                "ip": switch_ip,
                "cpu_utilization": cpu_utilization
            }
        })


    # --- Construct Elasticsearch Document ---
    es_document = {
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "agent_type": "cisco_snmp_agent",
        "switch_ip": switch_ip,
        "hostname": hostname,
        "model": model, # Placeholder, populate if you have this data
        "cpu_utilization": cpu_utilization,
        "memory_utilization": memory_utilization,
        "uptime": uptime,
        "overall_interface_status": overall_interface_status,
        "problem_interfaces": problem_interfaces_list, # List of names of problem interfaces
        "interfaces_details": final_interfaces_details,
        "temperature_sensors": temperature_sensors_data # New field for temperature
    }

    # --- Send to Elasticsearch ---
    try:
        await es.index(index=ES_INDEX_NAME, document=es_document, id=str(uuid.uuid4()))
        logging.info(f"Successfully indexed data for switch {hostname} to Elasticsearch.")
    except Exception as e:
        logging.error(f"Failed to index data for switch {hostname} to Elasticsearch: {e}", exc_info=True)
        await send_websocket_notification({
            "frontend_block_id": "G.CISCOSW",
            "message": f"Backend: Failed to index switch data for {hostname} ({switch_ip}) to Elasticsearch: {e}",
            "severity": "ERROR",
            "status": "ERROR",
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "type": "BACKEND_ERROR"
        })

async def discover_switches():
    """
    Discovers active switches from Prometheus based on a common metric.
    Returns a list of dictionaries: [{ 'hostname': '...', 'switch_ip': '...' }]
    """
    logging.info("Discovering switches from Prometheus...")
    query = 'cisco_snmp_cpu_5min{job="telegraf_snmp"}' # Assuming this metric exists for all switches
    
    try:
        result = await query_prometheus(query)
        if result and result.get('status') == 'success' and result['data']['result']:
            unique_switches = {}
            for item in result['data']['result']:
                hostname = item['metric'].get('hostname')
                agent_host = item['metric'].get('agent_host') # This is the IP
                if hostname and agent_host:
                    unique_switches[hostname] = {'hostname': hostname, 'switch_ip': agent_host}
            logging.info(f"Discovered {len(unique_switches)} switches.")
            return list(unique_switches.values())
        else:
            logging.warning("No switch data found in Prometheus for discovery query.")
            await send_websocket_notification({
                "frontend_block_id": "G.CISCOSW",
                "message": "Backend: No switch data found in Prometheus for discovery.",
                "severity": "WARNING",
                "status": "NO_DATA",
                "timestamp": datetime.utcnow().isoformat() + "Z",
                "type": "BACKEND_WARNING"
            })
            return []
    except Exception as e:
        logging.error(f"Failed to discover switches from Prometheus: {e}", exc_info=True)
        await send_websocket_notification({
            "frontend_block_id": "G.CISCOSW",
            "message": f"Backend: Failed to discover switches from Prometheus: {e}",
            "severity": "CRITICAL",
            "status": "ERROR",
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "type": "BACKEND_ERROR"
        })
        return []

async def main_agent():
    """Main asynchronous loop for the backend agent."""
    logging.info("Starting Cisco Switch Data Agent.")
    while True:
        start_time = datetime.now()
        logging.info("Starting new data collection cycle.")
        
        switches = await discover_switches()
        if switches:
            tasks = [process_switch_data(s) for s in switches]
            await asyncio.gather(*tasks)
        else:
            logging.warning("No switches to process in this cycle.")

        end_time = datetime.now()
        duration = (end_time - start_time).total_seconds()
        
        sleep_time = POLLING_INTERVAL_SECONDS - duration
        if sleep_time > 0:
            logging.info(f"Data collection cycle completed in {duration:.2f} seconds. Sleeping for {sleep_time:.2f} seconds.")
            await asyncio.sleep(sleep_time)
        else:
            logging.warning(f"Data collection cycle took {duration:.2f} seconds, exceeding polling interval. Not sleeping.")

if __name__ == "__main__":
    try:
        asyncio.run(main_agent())
    except KeyboardInterrupt:
        logging.info("Cisco Switch Data Agent stopped by user (KeyboardInterrupt).")
    except Exception as e:
        logging.critical(f"Cisco Switch Data Agent terminated due to an unhandled error: {e}", exc_info=True)
    finally:
        # Ensure aiohttp session and Elasticsearch client are closed on exit
        if http_session and not http_session.closed:
            asyncio.run(http_session.close())
        if es:
            asyncio.run(es.close())
