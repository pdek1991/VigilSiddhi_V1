from flask import Flask, render_template, jsonify, request
from flask_cors import CORS
import sys
import os
import logging
from datetime import datetime
import json

# Configure logging for more visibility
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# Adjust path to import from the backend directory
current_file_dir = os.path.dirname(__file__)
project_root = os.path.abspath(os.path.join(current_file_dir, '..'))

sys.path.append(os.path.join(project_root, 'backend'))
sys.path.append(project_root)

# Import your backend classes
from elastic_client import ElasticManager
from backend.mysql_client import MySQLManager


app = Flask(__name__)
CORS(app)

logging.info(f"DEBUG: Flask app instance created. Name: {__name__}")
logging.info(f"DEBUG: Flask template_folder (default): {app.template_folder}")
logging.info(f"DEBUG: Flask static_folder (default): {app.static_folder}")
logging.info(f"DEBUG: Flask static_url_path (default): {app.static_url_path}")


# --- Elasticsearch Manager Initialization ---
ES_HOST = os.environ.get('ES_HOST', '192.168.56.30')
ES_PORT = int(os.environ.get('ES_PORT', 9200))
es_manager = None
try:
    es_manager = ElasticManager(hosts=[f"http://{ES_HOST}:{ES_PORT}"])
    logging.info(f"Elasticsearch Manager initialized for host {ES_HOST}:{ES_PORT}.")
except Exception as e:
    logging.error(f"Failed to initialize Elasticsearch Manager: {e}. Elasticsearch-dependent features will not function.")
finally:
    if es_manager and es_manager.es and es_manager.es.ping():
        logging.info("Elasticsearch connection confirmed: OK.")
    else:
        logging.warning("Elasticsearch connection failed or ElasticManager not initialized. Check ES server status and configuration.")

# --- MySQL Manager Initialization ---
MYSQL_HOST = os.environ.get('MYSQL_HOST', '192.168.56.30')
MYSQL_DATABASE = os.environ.get('MYSQL_DATABASE', 'vigil_siddhi')
MYSQL_USER = os.environ.get('MYSQL_USER', 'vigilsiddhi')
MYSQL_PASSWORD = os.environ.get('MYSQL_PASSWORD', 'vigilsiddhi')

mysql_manager = None
try:
    mysql_manager = MySQLManager(
        host=MYSQL_HOST,
        database=MYSQL_DATABASE,
        user=MYSQL_USER,
        password=MYSQL_PASSWORD
    )
    if not mysql_manager.connection or not mysql_manager.connection.is_connected():
        logging.critical(f"FATAL: MySQLManager initialized but connection is not active.")
    logging.info("MySQLManager initialized.")
except Exception as e:
    logging.error(f"Failed to initialize MySQLManager: {e}. MySQL-dependent features will not function.")


# --- Routes for serving HTML pages ---
@app.route('/')
def index():
    """Serves the main dashboard page."""
    return render_template('index.html')

@app.route('/alarm_console_fullscreen')
def alarm_console_fullscreen():
    """Serves the full-screen alarm console page."""
    block_id = request.args.get('block_id')
    return render_template('alarm_console_fullscreen.html', block_id=block_id)

@app.route('/ird_overview')
def ird_overview():
    """Serves the IRD overview dashboard page."""
    return render_template('ird_overview.html')

@app.route('/switch_overview')
def switch_overview_page():
    """Serves the new Switch Overview dashboard page."""
    return render_template('switch_overview.html')


# --- API Endpoints ---

@app.route('/api/v1/get_all_device_ips', methods=['GET'])
def get_all_device_ips():
    """
    Fetches all unique device IPs from MySQL.
    This is primarily for populating dropdowns on the frontend.
    """
    if not mysql_manager:
        logging.error("MySQL manager not available to fetch device IPs.")
        return jsonify({"status": "error", "message": "MySQL not configured"}), 503
    
    try:
        device_ips = mysql_manager.get_all_device_ips()
        return jsonify({"ips": [ip.get('ip_address') for ip in device_ips]}), 200
    except Exception as e:
        logging.exception(f"Error fetching all device IPs: {e}")
        return jsonify({"status": "error", "message": f"Error fetching device IPs: {str(e)}"}), 500

@app.route('/api/v1/get_channel_configs', methods=['GET'])
def get_channel_configs():
    """
    Fetches channel configurations from MySQL.
    """
    if not mysql_manager:
        logging.error("MySQL manager not available to fetch channel configs.")
        return jsonify({"status": "error", "message": "MySQL not configured"}), 503

    try:
        channels = mysql_manager.get_channel_configs()
        return jsonify({"channels": channels}), 200
    except Exception as e:
        logging.exception(f"Error fetching channel configurations: {e}")
        return jsonify({"status": "error", "message": f"Error fetching channel configurations: {str(e)}"}), 500

@app.route('/api/v1/get_pgm_routing_configurations', methods=['GET'])
def get_pgm_routing_configurations():
    """
    Fetches PGM routing configurations from MySQL.
    """
    if not mysql_manager:
        logging.error("MySQL manager not available to fetch PGM routing configurations.")
        return jsonify({"status": "error", "message": "MySQL not configured"}), 503
    
    try:
        pgm_configs = mysql_manager.get_pgm_routing_configs()
        return jsonify({"pgm_configs": pgm_configs}), 200
    except Exception as e:
        logging.exception(f"Error fetching PGM routing configurations: {e}")
        return jsonify({"status": "error", "message": f"Error fetching PGM routing configurations: {str(e)}"}), 500


@app.route('/api/v1/get_kmx_block_status', methods=['GET'])
def get_kmx_block_status():
    """
    Fetches the overall status for KMX blocks from Elasticsearch.
    This assumes a summary document exists in ES.
    """
    if not es_manager:
        logging.error("Elasticsearch manager not available to fetch KMX block status.")
        return jsonify({"overall_status": "unknown"}), 503

    try:
        kmx_status_doc = es_manager.get_config("KMX_BLOCK_STATUS")
        overall_status = kmx_status_doc.get("overall_status", "unknown") if kmx_status_doc else "unknown"
        return jsonify({"overall_status": overall_status}), 200
    except Exception as e:
        logging.exception(f"Error fetching KMX block status: {e}")
        return jsonify({"overall_status": "unknown", "message": f"Error: {str(e)}"}), 500


@app.route('/api/v1/get_all_active_alarms', methods=['GET'])
def get_all_active_alarms():
    """
    Fetches all active alarms from the 'active_alarms' Elasticsearch index.
    """
    if not es_manager:
        logging.error("Elasticsearch manager not available to fetch all active alarms.")
        return jsonify({"status": "error", "message": "Elasticsearch not configured"}), 503
    
    try:
        all_alarms = es_manager.fetch_all_alarms()
        return jsonify({"alarms": all_alarms}), 200
    except Exception as e:
        logging.exception(f"Error fetching all active alarms: {e}")
        return jsonify({"status": "error", "message": f"Error fetching active alarms: {str(e)}"}), 500


@app.route('/api/v1/get_alarm_history', methods=['GET', 'POST'])
def get_alarm_history():
    """
    Fetches alarm history from the 'monitor_historical_alarms' Elasticsearch index
    based on provided filters (time range, device_name, channel_name, group_name, agent_type).
    """
    if not es_manager:
        logging.error("Elasticsearch manager not available to fetch alarm history.")
        return jsonify({"status": "error", "message": "Elasticsearch not configured"}), 503

    if request.method == 'POST':
        data = request.get_json(silent=True) or {}
    else:
        data = request.args

    from_timestamp_str = data.get('from_timestamp')
    to_timestamp_str = data.get('to_timestamp')
    device_name = data.get('device_name')
    channel_name = data.get('channel_name')
    group_name = data.get('group_name')
    agent_type = data.get('agent_type')
    severity = data.get('severity')
    message = data.get('message')
    block_id = data.get('block_id')

    es_query_body = {
        "query": {
            "bool": {
                "must": []
            }
        },
        "sort": [{"timestamp": {"order": "desc"}}],
        "size": 1000
    }
    
    if from_timestamp_str or to_timestamp_str:
        range_query = {"range": {"timestamp": {}}}
        if from_timestamp_str:
            range_query["range"]["timestamp"]["gte"] = from_timestamp_str
        if to_timestamp_str:
            range_query["range"]["timestamp"]["lte"] = to_timestamp_str
        es_query_body["query"]["bool"]["must"].append(range_query)
    
    if device_name:
        es_query_body["query"]["bool"]["must"].append({"match": {"device_name": device_name}})
    if channel_name:
        es_query_body["query"]["bool"]["must"].append({"match": {"channel_name": channel_name}})
    if group_name:
        es_query_body["query"]["bool"]["must"].append({"match": {"group_name": group_name}})
    if agent_type:
        es_query_body["query"]["bool"]["must"].append({"match": {"type": agent_type}})
    if severity:
        es_query_body["query"]["bool"]["must"].append({"match": {"severity": severity}})
    if message:
        es_query_body["query"]["bool"]["must"].append({"match": {"message": message}})

    if block_id:
        es_query_body["query"]["bool"]["must"].append({
            "bool": {
                "should": [
                    {"term": {"frontend_block_id.keyword": block_id.lower()}},
                    {"term": {"group_id.keyword": block_id.lower()}}
                ],
                "minimum_should_match": 1
            }
        })
        logging.info(f"Applying block_id filter: {block_id} to ES query.")
    
    logging.info(f"Executing ES historical alarm query: {json.dumps(es_query_body)}")

    try:
        response = es_manager.es.search(index="monitor_historical_alarms", body=es_query_body)
        
        historical_alarms_hits = response.get('hits', {}).get('hits', [])
        total_hits_value = response.get('hits', {}).get('total', {}).get('value', 0)

        formatted_alarms = {
            "hits": [
                {"_source": hit.get('_source')} for hit in historical_alarms_hits
            ],
            "total": {"value": total_hits_value, "relation": "eq"}
        }
        return jsonify(formatted_alarms), 200
    except Exception as e:
        logging.exception(f"Error fetching alarm history: {e}")
        return jsonify({"status": "error", "message": f"Error fetching alarm history: {str(e)}"}), 500

@app.route('/api/v1/get_switch_overview_data', methods=['GET'])
def get_switch_overview_data():
    """
    Fetches switch configurations from MySQL and combines with
    latest overview data (CPU, Memory, Interfaces) from Elasticsearch.
    """
    if not mysql_manager or not es_manager:
        logging.error("MySQL or Elasticsearch manager not available to fetch switch overview data.")
        return jsonify({"status": "error", "message": "Backend services not configured"}), 503

    combined_switch_data = []

    try:
        # 1. Fetch switch configurations from MySQL
        # Assuming mysql_manager has a method to execute raw queries or get switch configs.
        # This is a placeholder for a proper method in MySQLManager.
        cursor = mysql_manager.connection.cursor(dictionary=True)
        cursor.execute("SELECT switch_ip, hostname, model FROM switch_configs")
        switch_configs = cursor.fetchall()
        cursor.close()
        logging.info(f"Fetched {len(switch_configs)} switch configurations from MySQL.")

        # 2. For each switch, fetch latest data from Elasticsearch
        for config in switch_configs:
            switch_ip = config.get('switch_ip')
            hostname = config.get('hostname')
            model = config.get('model')

            if not switch_ip or not hostname:
                logging.warning(f"Skipping switch config due to missing IP or hostname: {config}")
                continue

            es_query_body = {
                "query": {
                    "bool": {
                        "must": [
                            {"term": {"switch_ip.keyword": switch_ip}},
                            {"term": {"hostname.keyword": hostname}}
                        ]
                    }
                },
                "sort": [{"timestamp": {"order": "desc"}}],
                "size": 1 # Get only the latest document
            }

            es_data = {}
            try:
                # Assuming ElasticManager's es client can be used directly for search
                es_response = es_manager.es.search(index="historical_alarms", body=es_query_body)
                if es_response and es_response['hits']['hits']:
                    latest_doc = es_response['hits']['hits'][0]['_source']
                    es_data = {
                        "cpu_utilization": latest_doc.get("cpu_utilization"),
                        "memory_utilization": latest_doc.get("memory_utilization"),
                        "interfaces": latest_doc.get("interfaces", [])
                    }
                    logging.info(f"Fetched ES data for {switch_ip} - {hostname}.")
                else:
                    logging.info(f"No ES data found for {switch_ip} - {hostname} in switch_overview.")
            except Exception as es_e:
                logging.error(f"Error fetching ES data for {switch_ip} - {hostname}: {es_e}")
                es_data = {} # Ensure empty data if ES fetch fails

            # 3. Combine MySQL and Elasticsearch data
            combined_switch = {
                "switch_ip": switch_ip,
                "hostname": hostname,
                "model": model,
                "cpu_utilization": es_data.get("cpu_utilization", "N/A"),
                "memory_utilization": es_data.get("memory_utilization", "N/A"),
                "interfaces": es_data.get("interfaces", [])
            }

            # Determine overall interface status and collect problematic interfaces
            interface_problem = False
            problem_interfaces = []
            for iface in combined_switch["interfaces"]:
                if iface.get("admin_status") == "up" and iface.get("oper_status") == "down":
                    interface_problem = True
                    problem_interfaces.append(iface.get("name", "Unknown Interface"))
            
            combined_switch["overall_interface_status"] = "Problem" if interface_problem else "OK"
            combined_switch["problem_interfaces"] = problem_interfaces

            combined_switch_data.append(combined_switch)

        return jsonify(combined_switch_data), 200

    except Exception as e:
        logging.exception(f"Error in get_switch_overview_data: {e}")
        return jsonify({"status": "error", "message": f"Error fetching switch overview data: {str(e)}"}), 500

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)