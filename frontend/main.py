from flask import Flask, render_template, jsonify, request
from flask_cors import CORS
import sys
import os
import logging
from datetime import datetime

# Configure logging for more visibility
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# Adjust path to import from the backend directory
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.append(os.path.join(project_root, 'backend'))
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

# Import your backend classes
from backend.elastic_client import ElasticManager
# from backend.ilo_proxy import ILOProxy # Commenting out as direct proxy calls are being replaced by agents
# from backend.ird_monitor import D9800IRD # Commenting out
# from backend.ird_overview_monitor import D9800IRDOverview # Commenting out
# from backend.windows_monitor import WindowsMonitor # Commenting out

# NEW: Import MySQLManager
from backend.mysql_client import MySQLManager

# Define the absolute path to the Media folder for static files
media_folder_path = os.path.abspath(os.path.join(project_root, 'Media'))
# Define the absolute path to the templates folder
templates_folder_path = os.path.abspath(os.path.join(project_root, 'frontend', 'templates'))

# --- DEBUGGING PATHS ---
logging.info(f"DEBUG: Resolved project_root: {project_root}")
logging.info(f"DEBUG: Resolved media_folder_path: {media_folder_path}")
logging.info(f"DEBUG: Resolved templates_folder_path: {templates_folder_path}")
# --- END DEBUGGING PATHS ---

# --- Initialization ---
app = Flask(__name__,
            template_folder=templates_folder_path,
            static_folder=media_folder_path,
            static_url_path='/media')
CORS(app)

# Define paths to initial configuration files (if still used, otherwise remove)
config_dir = os.path.abspath(os.path.join(project_root, 'initial_configs'))

# NOTE: These config files are for initial loading into ES.
# For dynamic config management from frontend, you'd need dedicated APIs to MySQL.
config_file_paths = {
    'channel': os.path.join(config_dir, 'channel_ilo_config.json'),
    'global': os.path.join(config_dir, 'global_ilo_config.json'),
    'windows': os.path.join(config_dir, 'windows_config.yaml'),
    'ird': os.path.join(config_dir, 'ird_config.json')
}

# --- DEBUGGING CONFIG FILE PATHS ---
logging.info(f"DEBUG: Config file paths: {config_file_paths}")
for key, path in config_file_paths.items():
    logging.info(f"DEBUG: Checking config file '{key}': Does it exist? {os.path.exists(path)}")
    if not os.path.exists(path):
        logging.error(f"ERROR: Config file '{path}' does NOT exist. Please check your initial_configs folder.")
# --- END DEBUGGING CONFIG FILE PATHS ---


# Initialize Elasticsearch connection and MySQLManager
es_manager = None
es_client = None
mysql_manager = None # NEW: Initialize MySQLManager

try:
    es_manager = ElasticManager(hosts=["http://192.168.56.30:9200"])
    es_client = es_manager.get_client()

    # NEW: Initialize MySQLManager
    MYSQL_HOST = os.environ.get('MYSQL_HOST', '192.168.56.30')
    MYSQL_DATABASE = os.environ.get('MYSQL_DATABASE', 'vigil_siddhi')
    MYSQL_USER = os.environ.get('MYSQL_USER', 'vigilsiddhi')
    MYSQL_PASSWORD = os.environ.get('MYSQL_PASSWORD', 'vigilsiddhi')
    mysql_manager = MySQLManager(
        host=MYSQL_HOST,
        database=MYSQL_DATABASE,
        user=MYSQL_USER,
        password=MYSQL_PASSWORD
    )
    if not mysql_manager.connection or not mysql_manager.connection.is_connected():
        logging.critical(f"FATAL: MySQLManager initialized but connection is not active.")
        # Optionally exit here if DB is critical for startup
    logging.info("MySQLManager initialized for backend.")

    logging.info("Attempting to load initial configurations into Elasticsearch...")
    # NOTE: Initial config loading from files might become less relevant if frontend manages configs directly
    es_manager.load_initial_configs(config_file_paths)
    logging.info("Initial configurations loaded successfully.")
    
except Exception as e:
    logging.error(f"FATAL: Could not initialize backend services. {e}")
    # Services remain None, handled gracefully by endpoints

# --- HTML Serving Endpoints ---
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/alarm_console_fullscreen')
def alarm_console_fullscreen():
    try:
        return render_template('alarm_console_fullscreen.html')
    except Exception as e:
        logging.error(f"ERROR: Failed to render alarm_console_fullscreen.html: {e}")
        return "<h1>Error: Could not load Alarm Console</h1><p>Please check server logs for details.</p>", 500

@app.route('/ird_overview')
def ird_overview_page():
    try:
        return render_template('ird_overview.html')
    except Exception as e:
        logging.error(f"ERROR: Failed to render ird_overview.html: {e}")
        return "<h1>Error: Could not load IRD Overview</h1><p>Please check server logs for details.</p>", 500

# --- API Endpoints ---
# All /api/v1/* endpoints are now expected to either:
# 1. Fetch data directly from Elasticsearch (e.g., active alarms, KMX block status)
# 2. Fetch configurations from MySQL (e.g., channel names, device_ips for frontend config)
# 3. Act as a proxy to external agents/consumers (e.g., the KMX Java service)

# NEW: Endpoint to get all channel configurations for frontend rendering
@app.route('/api/v1/get_channel_configs', methods=['GET'])
def get_channel_configs():
    if not mysql_manager:
        logging.error("MySQLManager not available to fetch channel configurations.")
        return jsonify({"status": "error", "message": "Database not configured"}), 503
    try:
        channels = mysql_manager.get_channel_configs()
        return jsonify({"channels": channels}), 200
    except Exception as e:
        logging.exception(f"Error fetching channel configurations: {e}")
        return jsonify({"status": "error", "message": f"Error: {str(e)}"}), 500

# NOTE: The following APIs (`get_ilo_status`, `get_collective_ilo_status`, `get_windows_status`, `get_ird_status`, `update_kmx_data`)
# are kept for compatibility and to show how a Flask backend might aggregate/proxy data,
# but the primary live updates to the frontend will now come via WebSocket.
# These APIs would primarily be used for initial state loading or on-demand full refreshes.

@app.route('/api/v1/get_ilo_status/<string:channel_id_str>/<string:device_id>', methods=['GET'])
def get_ilo_status(channel_id_str, device_id):
    # This endpoint should now fetch the latest status from Elasticsearch's active_alarms
    # or the last known good status for a specific frontend_block_id.
    # For now, this is a placeholder/mock. The real status comes via WebSocket.
    # This might be used by the frontend to get an initial state or in case WS fails.
    try:
        frontend_block_id = f"C.{channel_id_str.lstrip('C.')}{device_id.replace('VS_', '').replace('CP_', '').replace('DA_', '')}" # Logic to construct ID from frontend
        # Example: Query Elasticsearch for alarms related to this specific block_id
        alarms_for_block = es_manager.fetch_alarms_by_block_id(frontend_block_id) if es_manager else []
        
        # Determine status/severity from alarms, or default to OK
        status = "OK"
        severity = "INFO"
        if alarms_for_block:
            # Find the highest severity alarm for this block
            highest_severity = "INFO"
            severity_order = {"INFO": 0, "WARNING": 1, "P-WARNING": 2, "ALARM": 3, "ERROR": 4, "CRITICAL": 5}
            
            for alarm in alarms_for_block:
                current_severity_rank = severity_order.get(alarm.get("severity", "INFO").upper(), 0)
                highest_severity_rank = severity_order.get(highest_severity.upper(), 0)
                if current_severity_rank > highest_severity_rank:
                    highest_severity = alarm["severity"]

            status = "ALARM" if highest_severity_rank >= severity_order["ALARM"] else highest_severity
            severity = highest_severity
        
        return jsonify({"status": status, "severity": severity, "alarms": alarms_for_block}), 200
    except Exception as e:
        logging.exception(f"Error fetching iLO status for {channel_id_str}-{device_id}: {e}")
        return jsonify({"status": "unknown", "severity": "UNKNOWN", "message": f"Error: {str(e)}"}), 500


@app.route('/api/v1/get_collective_ilo_status/<string:block_type>', methods=['GET'])
def get_collective_ilo_status(block_type):
    # This will fetch the status for collective blocks (e.g., G.iloM, G.COMPRESSION M)
    # The actual update will happen via WebSocket for real-time changes.
    try:
        # Example: Fetch a specific document representing the overall status of this collective type
        # Or aggregate statuses from related alarms
        status_doc = es_manager.get_config(block_type) if es_manager else {}
        overall_status = status_doc.get("overall_status", "unknown")
        
        # Optionally, fetch alarms for this collective type if stored with a matching 'type' or 'group_id'
        alarms_for_group = es_manager.fetch_alarms_by_group_id(block_type) if es_manager else []

        return jsonify({"status": overall_status, "alarms": alarms_for_group}), 200
    except Exception as e:
        logging.exception(f"Error fetching collective iLO status for {block_type}: {e}")
        return jsonify({"status": "unknown", "message": f"Error: {str(e)}"}), 500

@app.route('/api/v1/get_windows_status', methods=['GET'])
def get_windows_status():
    # This should now get the latest status from Elasticsearch, where the Windows agent publishes.
    try:
        # Assuming WindowsMonitor agent pushes an aggregated status to ES.
        # This is a placeholder; actual implementation depends on WindowsMonitor's ES indexing.
        windows_overall_status_doc = es_manager.get_config("WINDOWS_OVERALL_STATUS") if es_manager else {}
        overall_status = windows_overall_status_doc.get("overall_status", "unknown")
        alarms = es_manager.fetch_alarms_by_type("windows_monitor") if es_manager else [] # Assuming type
        
        return jsonify({"overall_status": overall_status, "alarms": alarms}), 200
    except Exception as e:
        logging.exception(f"Error fetching Windows status: {e}")
        return jsonify({"overall_status": "unknown", "message": f"Error: {str(e)}"}), 500
    
@app.route('/api/v1/get_ird_status', methods=['GET'])
def get_ird_status():
    # This endpoint serves the data for the IRD Overview page.
    # It should fetch all IRD configurations and their latest statuses/faults from Elasticsearch.
    try:
        if not es_manager:
            return jsonify({"status": "error", "message": "Elasticsearch not configured"}), 503
        
        # Fetch all IRD configurations (assuming 'ird_configurations' index holds configs)
        # The agent should be responsible for updating status/faults in this document or a related one.
        # For simplicity, let's assume `es_manager.get_all_ird_configs_with_status()` exists
        # and fetches the latest status/faults aggregated by the IRD agent/consumer.
        
        # Placeholder for actual data retrieval based on IRD agent's ES output
        # If the IRD agent publishes to a specific index (e.g., 'ird_live_status'), we query that.
        # For now, this mimics fetching detailed status for IRD overview.
        # The IRD agent/consumer will need to update these documents in ES.
        
        # This will fetch data intended for the IRD Overview page, which shows detailed IRDs.
        # This needs to query an ES index where the IRD consumer stores detailed IRD statuses.
        # Assuming the IRD consumer indexes per-IRD documents with all their latest telemetry.
        all_ird_statuses = es_manager.fetch_all_ird_live_statuses() if es_manager else [] # Example ES method
        
        return jsonify({"irds": all_ird_statuses}), 200
    except Exception as e:
        logging.exception(f"Error fetching IRD status for overview: {e}")
        return jsonify({"status": "error", "message": f"Error: {str(e)}"}), 500


@app.route('/api/v1/ird_overview_status', methods=['GET'])
def ird_overview_status():
    # This endpoint is explicitly for the summary in the main dashboard's IRD block.
    # It should aggregate the highest severity across all IRDs.
    try:
        if not es_manager:
            return jsonify({"status": "error", "message": "Elasticsearch not configured"}), 503
        
        # Fetch all current IRD alarms/statuses and determine overall status
        all_ird_alarms = es_manager.fetch_alarms_by_type("ird_overview") # Or a more generic query for all IRD related alarms
        
        overall_status = "OK"
        overall_severity_rank = 0
        severity_order = {"INFO": 0, "WARNING": 1, "MINOR": 2, "MAJOR": 3, "CRITICAL": 4, "ERROR": 5}

        for alarm in all_ird_alarms:
            alarm_severity = alarm.get("severity", "INFO").upper()
            if alarm_severity in severity_order:
                if severity_order[alarm_severity] > overall_severity_rank:
                    overall_severity_rank = severity_order[alarm_severity]
                    overall_status = "ALARM" if overall_severity_rank >= severity_order["MINOR"] else alarm_severity # Map to dashboard status

        return jsonify({"overall_status": overall_status, "alarms": all_ird_alarms}), 200

    except Exception as e:
        logging.exception(f"Error fetching IRD overview status: {e}")
        return jsonify({"status": "unknown", "message": f"Error: {str(e)}"}), 500


@app.route('/api/v1/update_kmx_data', methods=['POST'])
def update_kmx_data():
    if not es_manager:
        logging.error("Elasticsearch manager not available to receive KMX data from Java service.")
        return jsonify({"status": "error", "message": "Elasticsearch not configured"}), 503

    try:
        data_from_java = request.get_json()
        if not data_from_java:
            return jsonify({"status": "error", "message": "No JSON data received"}), 400

        overall_status = data_from_java.get("overall_status", "unknown")
        alarms = data_from_java.get("alarms", [])

        logging.info(f"Received KMX data from Java service. Overall Status: {overall_status}, Alarms: {len(alarms)}")

        # Add 'source' and 'type' to KMX alarms
        for alarm in alarms:
            alarm['source'] = 'kmx'
            alarm['type'] = alarm.get('type', 'snmp_kmx') # Default to snmp_kmx if not set by Java
            if '@timestamp' not in alarm:
                alarm['@timestamp'] = datetime.now().isoformat()

        # Update active alarms for KMX
        if es_manager:
            es_manager.update_active_alarms('snmp_kmx', alarms)

        # Store the overall status of KMX for the KMX block
        kmx_status_doc = {
            "id": "KMX_BLOCK_STATUS",
            "type": "block_status",
            "overall_status": overall_status,
            "timestamp": datetime.now().isoformat()
        }
        es_manager.store_config("KMX_BLOCK_STATUS", kmx_status_doc, "block_status")

        # After processing, potentially notify via websocket_notifier if it's integrated
        # (This Flask app acts as the proxy for the Java service)
        # Construct message similar to other agents' output for consistency with frontend WS listener
        ws_message = {
            "agent_type": "kmx",
            "device_ip": "172.19.185.230", # KMX fixed IP
            "device_name": "KMX",
            "group_name": "KMX", # Corresponds to G.KMX
            "frontend_block_id": "G.KMX", # Corresponds to G.KMX
            "status": overall_status,
            "severity": "UNKNOWN", # Default, let frontend infer from block status
            "message": f"KMX overall status: {overall_status}",
            "timestamp": datetime.now().isoformat() + "Z",
            "details": {"alarms": alarms} # Include alarms for alarm console
        }
        # Assuming you have a way to send this to the websocket_notifier from Flask
        # This would require either:
        # 1. Flask making an aiohttp call to the websocket_notifier's HTTP endpoint (recommended)
        # 2. Re-implementing a Redis publisher here (less ideal if notifier is already listening to Redis)
        
        # Example (if websocket_notifier's HTTP endpoint is directly callable by Flask):
        # import requests
        # try:
        #    requests.post(os.environ.get('WEBSOCKET_NOTIFIER_URL', 'http://127.0.0.1:8001/notify'), json=ws_message, timeout=1)
        # except requests.exceptions.RequestException as e:
        #    logging.warning(f"Failed to send KMX update to WebSocket notifier: {e}")

        return jsonify({"status": "success", "message": "KMX data received and processed"}), 200

    except Exception as e:
        logging.exception(f"Error processing KMX data from Java service: {e}")
        return jsonify({"status": "error", "message": f"Error processing KMX data: {str(e)}"}), 500


@app.route('/api/v1/get_kmx_block_status', methods=['GET'])
def get_kmx_block_status():
    # This endpoint gets the KMX block's overall status for display in the dashboard.
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
    # This endpoint fetches all active alarms from Elasticsearch to populate the console.
    if not es_manager:
        logging.error("Elasticsearch manager not available to fetch all active alarms.")
        return jsonify({"status": "error", "message": "Elasticsearch not configured"}), 503
    
    try:
        # This fetches from the 'active_alarms' index, which is kept up-to-date by consumers
        all_alarms = es_manager.fetch_all_alarms()
        return jsonify({"alarms": all_alarms}), 200
    except Exception as e:
        logging.exception(f"Error fetching all active alarms from Elasticsearch: {e}")
        return jsonify({"status": "error", "message": f"Error fetching alarms: {str(e)}"}), 500


if __name__ == '__main__':
    # Flask app will run on 5000.
    # WebSocket notifier needs to run on 8000/8001 separately.
    app.run(host='0.0.0.0', port=5000, debug=True, use_reloader=False) # use_reloader=False to prevent multiple calls on startup
