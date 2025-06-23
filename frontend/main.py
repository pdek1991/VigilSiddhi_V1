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
from backend.ilo_proxy import ILOProxy
from backend.ird_monitor import D9800IRD
from backend.ird_overview_monitor import D9800IRDOverview
from backend.windows_monitor import WindowsMonitor

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

# Define paths to initial configuration files
config_dir = os.path.abspath(os.path.join(project_root, 'initial_configs'))

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


# Initialize Elasticsearch connection and backend classes
es_manager = None
es_client = None
ilo_service = None
ird_service = None
ird_overview_service = None
windows_service = None

try:
    es_manager = ElasticManager(hosts=["http://192.168.56.30:9200"])
    es_client = es_manager.get_client()

    logging.info("Attempting to load initial configurations into Elasticsearch...")
    es_manager.load_initial_configs(config_file_paths)
    logging.info("Initial configurations loaded successfully.")
    
    ilo_service = ILOProxy(es_client)
    ird_service = D9800IRD(es_client)
    ird_overview_service = D9800IRDOverview(es_client)
    windows_service = WindowsMonitor(es_client)
    
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

@app.route('/api/v1/get_ilo_status/<string:channel_id_str>/<string:device_id>', methods=['GET'])
def get_ilo_status(channel_id_str, device_id):
    if not ilo_service:
        logging.error("iLO service not available during get_ilo_status API call.")
        return jsonify({"status": "error", "message": "iLO service not available"}), 503
    
    response_data, status_code = ilo_service.get_status_for_single_device(channel_id_str, device_id)
    
    # Update active alarms for this specific iLO device
    alarms = response_data.get("alarms", [])
    # Add common fields to alarms if missing and assign a type
    for alarm in alarms:
        alarm['source'] = 'ilo'
        alarm['type'] = f'ilo_device_channel_{channel_id_str}_id_{device_id}' # More specific type
        if 'channel_name' not in alarm and 'device_name' not in alarm:
            alarm['channel_name'] = f"Channel {channel_id_str}"
            alarm['device_name'] = device_id
        if '@timestamp' not in alarm:
            alarm['@timestamp'] = datetime.now().isoformat()
    
    # Clear and re-index active alarms for this specific iLO device/channel
    if es_manager:
        es_manager.update_active_alarms(f'ilo_device_channel_{channel_id_str}_id_{device_id}', alarms)
        
    return jsonify(response_data), status_code

@app.route('/api/v1/get_collective_ilo_status/<string:block_type>', methods=['GET'])
def get_collective_ilo_status(block_type):
    if not ilo_service:
        logging.error("iLO service not available during get_collective_ilo_status API call.")
        return jsonify({"status": "error", "message": "iLO service not available"}), 503
    
    response_data, status_code = ilo_service.get_status_for_collective(block_type)
    
    # Update active alarms for this collective iLO type
    alarms = response_data.get("alarms", [])
    for alarm in alarms:
        alarm['source'] = 'ilo_collective'
        alarm['type'] = f'ilo_collective_{block_type}' # Assign specific type
        if 'group_id' not in alarm:
            alarm['group_id'] = block_type
        if '@timestamp' not in alarm:
            alarm['@timestamp'] = datetime.now().isoformat()

    if es_manager:
        es_manager.update_active_alarms(f'ilo_collective_{block_type}', alarms)
        
    return jsonify(response_data), status_code

@app.route('/api/v1/get_windows_status', methods=['GET'])
def get_windows_status():
    if not windows_service:
        logging.error("Windows monitoring service not available during get_windows_status API call.")
        return jsonify({"status": "error", "message": "Windows monitoring service not available"}), 503
    
    results = windows_service.get_all_statuses()
    overall_status = "ok"
    
    # Process alarms for active_alarms index
    alarms = results.get('alarms', [])
    for alarm in alarms:
        alarm['source'] = 'windows'
        alarm['type'] = 'windows_monitor' # Assign specific type
        if '@timestamp' not in alarm:
            alarm['@timestamp'] = datetime.now().isoformat()
    
    if es_manager:
        es_manager.update_active_alarms('windows_monitor', alarms) # Update active alarms for Windows
    
    if any(s['overall_status'] != 'OK' for s in results.get('status_data', [])):
        overall_status = "alarm"
    
    return jsonify({
        "overall_status": overall_status,
        "alarms": alarms, # Return the alarms here too for frontend debugging if needed
        "details": results.get('status_data', [])
    })
    
@app.route('/api/v1/get_ird_status', methods=['GET'])
def get_ird_status():
    if not ird_service:
        logging.error("IRD monitoring service (D9800IRD) not available during get_ird_status API call.")
        return jsonify({"status": "error", "message": "IRD monitoring service not available"}), 503
    
    results = ird_service.get_all_channel_ird_statuses()
    
    # Process alarms from IRD channel monitoring
    all_ird_channel_alarms = []
    for ird_data in results.get('irds', []):
        # Only add alarms if the overall status indicates an issue OR if there are explicit faults
        if ird_data.get('overall_status') in ['Alarm', 'Warning', 'Error'] and ird_data.get('errors'):
            for error_msg in ird_data['errors']:
                alarm = {
                    'source': 'ird_channel',
                    'type': 'ird_channel', # Assign specific type
                    'server_ip': ird_data.get('ip_address'),
                    'message': error_msg,
                    'severity': ird_data.get('overall_status').upper(), # Use overall status severity
                    'device_name': f"D9800 IRD {ird_data.get('ip_address')}",
                    '@timestamp': datetime.now().isoformat()
                }
                all_ird_channel_alarms.append(alarm)
        
        # Include specific faults as alarms regardless of overall status summary
        if ird_data.get('fault_status'):
            for fault in ird_data['fault_status']:
                alarm = {
                    'source': 'ird_channel',
                    'type': 'ird_channel_fault', # More specific type for faults
                    'server_ip': fault.get('server_ip') or ird_data.get('ip_address'), # Use fault's IP if available, else IRD's
                    'message': f"Fault: {fault.get('name')} - {fault.get('details')}",
                    'severity': fault.get('severity', 'UNKNOWN').upper(),
                    'device_name': f"D9800 IRD {ird_data.get('ip_address')}",
                    '@timestamp': fault.get('setsince') or datetime.now().isoformat()
                }
                all_ird_channel_alarms.append(alarm)

    if es_manager:
        es_manager.update_active_alarms('ird_channel', all_ird_channel_alarms) # Update active alarms for IRD channels
    
    return jsonify(results)

@app.route('/api/v1/ird_overview_status', methods=['GET'])
def ird_overview_status():
    if not ird_overview_service:
        logging.error("IRD Overview service not available during ird_overview_status API call.")
        return jsonify({"status": "error", "message": "IRD Overview service not available"}), 503
    
    results = ird_overview_service.get_all_irds_status()
    
    # Process alarms from IRD overview monitoring
    all_ird_overview_alarms = []
    for ird_data in results: # results is a list of IRD status dicts
        # Only add alarms if the overall status indicates an issue OR if there are explicit faults
        if ird_data.get('overall_status') in ['Alarm', 'Warning', 'Error'] and ird_data.get('errors'):
            for error_msg in ird_data['errors']:
                alarm = {
                    'source': 'ird_overview',
                    'type': 'ird_overview', # Assign specific type
                    'server_ip': ird_data.get('ip_address'),
                    'message': error_msg,
                    'severity': ird_data.get('overall_status').upper(), # Use overall status severity
                    'device_name': f"D9800 IRD {ird_data.get('ip_address')}",
                    '@timestamp': datetime.now().isoformat()
                }
                all_ird_overview_alarms.append(alarm)
        
        # Include specific faults as alarms regardless of overall status summary
        if ird_data.get('fault_status'):
            for fault in ird_data['fault_status']:
                alarm = {
                    'source': 'ird_overview',
                    'type': 'ird_overview_fault', # More specific type for faults
                    'server_ip': fault.get('server_ip') or ird_data.get('ip_address'), # Use fault's IP if available, else IRD's
                    'message': f"Fault: {fault.get('name')} - {fault.get('details')}",
                    'severity': fault.get('severity', 'UNKNOWN').upper(),
                    'device_name': f"D9800 IRD {ird_data.get('ip_address')}",
                    '@timestamp': fault.get('setsince') or datetime.now().isoformat()
                }
                all_ird_overview_alarms.append(alarm)

    if es_manager:
        es_manager.update_active_alarms('ird_overview', all_ird_overview_alarms) # Update active alarms for IRD overview
        
    return jsonify(results)

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

        return jsonify({"status": "success", "message": "KMX data received and processed"}), 200

    except Exception as e:
        logging.exception(f"Error processing KMX data from Java service: {e}")
        return jsonify({"status": "error", "message": f"Error processing KMX data: {str(e)}"}), 500


@app.route('/api/v1/get_kmx_block_status', methods=['GET'])
def get_kmx_block_status():
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
    if not es_manager:
        logging.error("Elasticsearch manager not available to fetch all active alarms.")
        return jsonify({"status": "error", "message": "Elasticsearch not configured"}), 503
    
    try:
        # This now fetches from the 'active_alarms' index, which is kept up-to-date
        all_alarms = es_manager.fetch_all_alarms()
        return jsonify({"alarms": all_alarms}), 200
    except Exception as e:
        logging.exception(f"Error fetching all active alarms from Elasticsearch: {e}")
        return jsonify({"status": "error", "message": f"Error fetching alarms: {str(e)}"}), 500


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)

