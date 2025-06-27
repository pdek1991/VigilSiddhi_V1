from flask import Flask, render_template, jsonify, request
from flask_cors import CORS
import sys
import os
import logging
from datetime import datetime

# Configure logging for more visibility
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# Adjust path to import from the backend directory
# Assuming project_root is the directory containing both 'frontend' and 'backend'
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.append(os.path.join(project_root, 'backend'))
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))) # Add project root itself for other potential imports

# Import your backend classes
from elastic_client import ElasticManager
from backend.mysql_client import MySQLManager
# from ilo_proxy import ILOProxy # Uncomment if needed
# from ird_monitor import D9800IRD # Uncomment if needed
# from ird_overview_monitor import D9800IRDOverview # Uncomment if needed
# from windows_monitor import WindowsMonitor # Uncomment if needed

# Define the absolute path to the Media folder for static files
media_folder_path = os.path.abspath(os.path.join(project_root, 'Media'))
# Define the absolute path to the templates folder
templates_folder_path = os.path.abspath(os.path.join(os.path.dirname(__file__))) # Templates are in the same directory as main.py

# --- DEBUGGING PATHS ---
logging.info(f"DEBUG: Resolved project_root: {project_root}")
logging.info(f"DEBUG: Resolved media_folder_path: {media_folder_path}")
logging.info(f"DEBUG: Resolved templates_folder_path: {templates_folder_path}")

app = Flask(
    __name__,
    static_folder=media_folder_path, # Serve static files (like logo.png) from 'Media'
    static_url_path='/media', # URL path to access static files (e.g., /media/logo.png)
    template_folder=templates_folder_path # Serve HTML templates from 'frontend/templates'
)
CORS(app) # Enable CORS for all routes

# --- Elasticsearch Manager Initialization ---
ES_HOST = os.environ.get('ES_HOST', '192.168.56.30')
ES_PORT = int(os.environ.get('ES_PORT', 9200))
es_manager = None
try:
    # Correctly pass the hosts as a list of URLs to ElasticManager
    es_manager = ElasticManager(hosts=[f"http://{ES_HOST}:{ES_PORT}"])
    logging.info(f"Elasticsearch Manager initialized for host {ES_HOST}:{ES_PORT}.")
except Exception as e:
    logging.error(f"Failed to initialize Elasticsearch Manager: {e}. Elasticsearch-dependent features will not function.")
    # The application might still run, but ES-dependent features will fail.
finally:
    if es_manager and es_manager.es and es_manager.es.ping(): # Check es_manager.es as it could be None if init failed earlier
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
        # Depending on criticality, you might sys.exit(1) here or let it continue with warnings.
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
    return render_template('alarm_console_fullscreen.html')

@app.route('/ird_overview')
def ird_overview():
    """Serves the IRD overview dashboard page."""
    return render_template('ird_overview.html')


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
        # Example: Fetch a document that summarizes KMX status, e.g., by a known ID
        # Or you might aggregate KMX alarms for a real-time overall status
        kmx_status_doc = es_manager.get_config("KMX_BLOCK_STATUS") # Assuming a fixed doc ID
        overall_status = kmx_status_doc.get("overall_status", "unknown") if kmx_status_doc else "unknown"
        return jsonify({"overall_status": overall_status}), 200
    except Exception as e:
        logging.exception(f"Error fetching KMX block status: {e}")
        return jsonify({"overall_status": "unknown", "message": f"Error: {str(e)}"}), 500


@app.route('/api/v1/get_all_active_alarms', methods=['GET'])
def get_all_active_alarms():
    """
    Fetches all active alarms from the 'active_alarms' Elasticsearch index.
    This index is expected to be kept up-to-date by the consumers.
    """
    if not es_manager:
        logging.error("Elasticsearch manager not available to fetch all active alarms.")
        return jsonify({"status": "error", "message": "Elasticsearch not configured"}), 503
    
    try:
        # This now fetches from the 'active_alarms' index, which is kept up-to-date
        all_alarms = es_manager.fetch_all_alarms() # This method should query 'active_alarms'
        return jsonify({"alarms": all_alarms}), 200
    except Exception as e:
        logging.exception(f"Error fetching all active alarms: {e}")
        return jsonify({"status": "error", "message": f"Error fetching active alarms: {str(e)}"}), 500


@app.route('/api/v1/get_alarm_history', methods=['GET', 'POST']) # Allow POST for filters
def get_alarm_history():
    """
    Fetches alarm history from the 'monitor_historical_alarms' Elasticsearch index
    based on provided filters (time range, device_name, channel_name, group_name, agent_type).
    """
    if not es_manager:
        logging.error("Elasticsearch manager not available to fetch alarm history.")
        return jsonify({"status": "error", "message": "Elasticsearch not configured"}), 503

    # Get filter parameters from request arguments (GET) or JSON body (POST)
    if request.method == 'POST':
        data = request.get_json(silent=True) or {}
    else:
        data = request.args

    # Extract all possible filter parameters
    start_time_str = data.get('start_time')
    end_time_str = data.get('end_time')
    device_name = data.get('device_name')
    channel_name = data.get('channel_name')
    group_name = data.get('group_name')
    agent_type = data.get('agent_type')
    severity = data.get('severity') # New filter
    message = data.get('message') # New filter
    block_id = data.get('block_id') # New filter for specific dashboard blocks


    query_params = {}
    if start_time_str:
        query_params['start_time'] = start_time_str
    if end_time_str:
        query_params['end_time'] = end_time_str
    if device_name:
        query_params['device_name'] = device_name
    if channel_name:
        query_params['channel_name'] = channel_name
    if group_name:
        query_params['group_name'] = group_name
    if agent_type:
        query_params['agent_type'] = agent_type
    if severity:
        query_params['severity'] = severity
    if message:
        query_params['message'] = message
    if block_id:
        # Use 'frontend_block_id' if available, otherwise fallback to 'block_id'
        # The Elasticsearch document might have it stored as 'block_id' or 'frontend_block_id'
        query_params['block_id'] = block_id 


    try:
        # Call ElasticsearchManager to fetch historical alarms with filters
        # Assuming ElasticManager has a method like 'fetch_historical_alarms'
        historical_alarms = es_manager.fetch_historical_alarms(query_params)
        
        # The frontend expects a 'hits' structure for the alarm console fullscreen
        # Reformat the response to match the expected 'hits' structure
        formatted_alarms = {
            "hits": [
                {"_source": alarm} for alarm in historical_alarms
            ],
            "total": {"value": len(historical_alarms), "relation": "eq"}
        }
        return jsonify(formatted_alarms), 200
    except Exception as e:
        logging.exception(f"Error fetching alarm history: {e}")
        return jsonify({"status": "error", "message": f"Error fetching alarm history: {str(e)}"}), 500

if __name__ == '__main__':
    # Ensure 'static' and 'templates' folders exist if they are expected by Flask
    # In this setup, we assume 'main.py' is in the 'frontend' directory,
    # and templates are also in 'frontend' and static is in 'Media'
    
    # You might need to adjust static_folder and template_folder in Flask app initialization
    # if your directory structure is different.
    # For example, if 'static' and 'templates' are direct subdirectories of 'main.py's parent:
    # app = Flask(__name__, static_folder='../static', template_folder='../templates')
    
    app.run(debug=True, host='0.0.0.0', port=5000)
