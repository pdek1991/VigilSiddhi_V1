VigilSiddhi Dashboard
VigilSiddhi is a comprehensive monitoring and dashboard application designed to provide real-time status and alarm monitoring for various IT infrastructure components, including Cisco D9800 IRD devices, Windows hosts, and HPE iLO servers. It leverages Elasticsearch for data storage, a Flask backend for API exposure, and a modern web frontend for visualization.

##Features
Cisco D9800 IRD Monitoring:
Fetches RF data (C/N Margin, Signal Level, Input Rate) and channel names from configured IRD devices.
Generates and logs alarms based on IRD status and critical metrics.
Stores IRD trend data and alarms in Elasticsearch for historical analysis and real-time display.
Windows Host Monitoring:
Monitors Windows hosts by checking the status of specified services and processes via SSH and PowerShell commands.
Collects system information, disk usage, and network statistics.
Generates issues (alarms) based on predefined thresholds or service/process states.
Stores Windows host trend data and alarms in Elasticsearch.
HPE iLO Monitoring:
Fetches active Integrated Lights-Out (iLO) Management Log (IML) alarms.
Provides collective status and alarms for groups of iLO devices (e.g., "iLO M", "iLO P", "iLO B").
Includes a robust retry mechanism for network requests.
Centralized Configuration Management:
Loads initial configurations for channels, global groups, and Windows hosts from JSON/YAML files into dedicated Elasticsearch indices.
Allows dynamic retrieval and management of these configurations via API endpoints.
Real-time Web Dashboard:
A responsive Flask-based web interface for visualizing the health and status of all monitored devices.
Displays overall status, active alarms, and detailed information for IRD, Windows, and iLO components.
Includes a full-screen alarm console for focused alarm monitoring.
Robust Logging and Error Handling:
Comprehensive logging across all components for debugging and operational insights.
Handles connection errors and provides informative warning/error messages.
Data Flow
The application follows a client-server architecture with Elasticsearch acting as the central data store.

##Configuration Loading:

The ElasticManager (from elastic_client.py) is responsible for connecting to Elasticsearch and loading initial configurations from channel_ilo_config.json, global_ilo_config.json, and windows_config.yaml (implied by elastic_client.py and windows_monitor.py config loading) into respective Elasticsearch indices (channel_config, global_config, windows_config).
ird_monitor.py also loads its configurations from the ird_config Elasticsearch index.
Monitoring Data Collection:

IRD Monitoring: The D9800IRD class (from ird_monitor.py) periodically fetches status data, RF parameters, and channel names from configured Cisco D9800 IRD devices using HTTP/HTTPS requests. ird_graph.py provides functions for specific IRD data fetching (RF data, channel name) and pushing to ES.
Windows Monitoring: The WindowsMonitor class (from windows_monitor.py) connects to Windows hosts via SSH to execute PowerShell commands, gathering service, process, disk, and network information.
iLO Monitoring: The ILOProxy class (from ilo_proxy.py) interacts with HPE iLO devices to retrieve active IML alarms and compute collective group statuses.
Data Processing and Storage:

Monitors (IRD, Windows) process the collected raw data.
Alarms and status issues are generated based on predefined logic and thresholds.
All collected monitoring data (trend data) and generated alarms are pushed to Elasticsearch into ird_trend, windows_trend, active_alarms, and historical_alarms indices.
API Exposure (Backend):

The main.py Flask application acts as the backend server.
It initializes instances of ElasticManager, ILOProxy, D9800IRD, and WindowsMonitor.
It exposes RESTful API endpoints that the frontend can query to retrieve real-time status data, alarms, and configuration information.
Frontend Visualization (Dashboard):

The index.html and alarm_console_fullscreen.html files represent the web frontend.
These HTML pages use JavaScript to make asynchronous calls to the Flask API endpoints.
The retrieved data is then rendered dynamically on the dashboard, providing an overview of system health and detailed alarm information.
Endpoints
The Flask application exposes the following API endpoints:

##Dashboard & HTML Endpoints:

/ (GET): Serves the main dashboard HTML page (index.html).
/alarm_console_fullscreen (GET): Serves the full-screen alarm console HTML page (alarm_console_fullscreen.html).
Monitoring Status Endpoints:

/api/v1/get_windows_status (GET): Fetches the overall status and details for all configured Windows hosts.
/api/v1/get_ilo_collective_status (GET): Fetches the collective status and alarms for configured iLO device groups.
/api/v1/get_ird_status (GET): Fetches the overall status and alarms for all configured Cisco D9800 IRD devices.
/api/v1/get_active_alarms_dashboard (GET): Fetches active alarms relevant for display on the dashboard.
Configuration Management Endpoints:

/api/v1/configs/all (GET): Retrieves all configurations (channel, global, windows).
/api/v1/configs/channel (GET): Retrieves all channel configurations.
/api/v1/configs/global (GET): Retrieves all global group configurations.
/api/v1/configs/windows (GET): Retrieves all Windows host configurations.
/api/v1/configs/channel/<int:channel_id> (GET): Retrieves a specific channel configuration by channel_id.
/api/v1/configs/global/<string:group_id> (GET): Retrieves a specific global group configuration by group_id.
/api/v1/configs/windows/<string:host_name> (GET): Retrieves a specific Windows host configuration by host_name.
/api/v1/configs/channel (POST): Adds or updates a channel configuration.
/api/v1/configs/global (POST): Adds or updates a global group configuration.
/api/v1/configs/windows (POST): Adds or updates a Windows host configuration.
/api/v1/configs/channel/<int:channel_id> (DELETE): Deletes a channel configuration by channel_id.
/api/v1/configs/global/<string:group_id> (DELETE): Deletes a global group configuration by group_id.
/api/v1/configs/windows/<string:host_name> (DELETE): Deletes a Windows host configuration by host_name.
Elasticsearch Index Formats
This section details the mappings for the Elasticsearch indices used by the application. The base Elasticsearch URL used in the examples is http://192.168.56.30:9200.

##ird_trend Index
Purpose: Stores time-series trend data for Cisco D9800 IRD devices.
Mapping:
```
{
  "mappings": {
    "properties": {
      "@timestamp": { "type": "date" },
      "system_id": { "type": "integer" },
      "channel_name": { "type": "text" },
      "cn_margin": { "type": "float" },
      "signal_level": { "type": "float" },
      "input_rate": { "type": "long" }
    }
  }
}
```
channel_config Index
Purpose: Stores configurations for various channels, each potentially containing multiple devices.
Mapping:
JSON
```
{
  "mappings": {
    "properties": {
      "channel_id": { "type": "integer" },
      "devices": {
        "type": "nested",
        "properties": {
          "id": { "type": "keyword" },
          "ip": { "type": "ip" },
          "username": { "type": "keyword" },
          "password": { "type": "keyword" }
        }
      }
    }
  }
}
```

global_config Index
Purpose: Stores global group configurations, possibly for grouping different types of devices or services.
Mapping:
JSON
```
{
  "mappings": {
    "properties": {
      "id": { "type": "keyword" },
      "name": { "type": "text" },
      "type": { "type": "keyword" },
      "additional_ips": {
        "type": "nested",
        "properties": {
          "ip": { "type": "ip" },
          "username": { "type": "keyword" },
          "password": { "type": "keyword" }
        }
      }
    }
  }
}
```
##windows_config Index
Purpose: Stores configurations for Windows hosts, including their IP addresses, credentials, and lists of services/processes to monitor.
Mapping:
JSON
```
{
  "mappings": {
    "properties": {
      "name": { "type": "keyword" },
      "ip": { "type": "ip" },
      "username": { "type": "keyword" },
      "password": { "type": "keyword" },
      "services": { "type": "keyword" },
      "processes": { "type": "keyword" }
    }
  }
}
```
ird_config Index
Purpose: Stores configurations specific to IRD devices, used by ird_monitor.py to fetch devices to monitor.
Mapping:
JSON
```
{
  "mappings": {
    "properties": {
      "system_id": { "type": "keyword" },
      "ip_address": { "type": "ip" },
      "username": { "type": "keyword" },
      "password": { "type": "keyword" },
      "channel_name": { "type": "text" }
    }
  }
}
```
##active_alarms Index
Purpose: Stores currently active alarms for immediate display on the dashboard.
Mapping:
JSON
```
{
  "mappings": {
    "properties": {
      "@timestamp": { "type": "date" },
      "alarm_id": { "type": "keyword" },
      "source": { "type": "keyword" },
      "server_ip": { "type": "ip" },
      "message": { "type": "text" },
      "severity": { "type": "keyword" },
      "channel_name": { "type": "keyword" },
      "device_name": { "type": "keyword" },
      "group_id": { "type": "keyword" }
    }
  }
}
```
historical_alarms Index
Purpose: Stores a complete history of all generated alarms.
Mapping:
JSON
```
{
  "mappings": {
    "properties": {
      "@timestamp": { "type": "date" },
      "alarm_id": { "type": "keyword" },
      "source": { "type": "keyword" },
      "server_ip": { "type": "ip" },
      "message": { "type": "text" },
      "severity": { "type": "keyword" },
      "channel_name": { "type": "keyword" },
      "device_name": { "type": "keyword" },
      "group_id": { "type": "keyword" }
    }
  }
}
```
Create Index with cURL
You can create the necessary Elasticsearch indices using the following curl commands. Replace http://192.168.56.30:9200 with your Elasticsearch host and port if different.

Bash
```
curl -X PUT "http://192.168.56.30:9200/ird_trend" -H "Content-Type: application/json" -d "{\"mappings\":{\"properties\":{\"@timestamp\":{\"type\":\"date\"},\"system_id\":{\"type\":\"integer\"},\"channel_name\":{\"type\":\"text\"},\"cn_margin\":{\"type\":\"float\"},\"signal_level\":{\"type\":\"float\"},\"input_rate\":{\"type\":\"long\"}}}}"
```
```
curl -X PUT "http://192.168.56.30:9200/channel_config" -H "Content-Type: application/json" -d "{\"mappings\":{\"properties\":{\"channel_id\":{\"type\":\"integer\"},\"devices\":{\"type\":\"nested\",\"properties\":{\"id\":{\"type\":\"keyword\"},\"ip\":{\"type\":\"ip\"},\"username\":{\"type\":\"keyword\"},\"password\":{\"type\":\"keyword\"}}}}}}"
```
```
curl -X PUT "http://192.168.56.30:9200/global_config" -H "Content-Type: application/json" -d "{\"mappings\":{\"properties\":{\"id\":{\"type\":\"keyword\"},\"name\":{\"type\":\"text\"},\"type\":{\"type\":\"keyword\"},\"additional_ips\":{\"type\":\"nested\",\"properties\":{\"ip\":{\"type\":\"ip\"},\"username\":{\"type\":\"keyword\"},\"password\":{\"type\":\"keyword\"}}}}}}"
```
```
curl -X PUT "http://192.168.56.30:9200/windows_config" -H "Content-Type: application/json" -d "{\"mappings\":{\"properties\":{\"name\":{\"type\":\"keyword\"},\"ip\":{\"type\":\"ip\"},\"username\":{\"type\":\"keyword\"},\"password\":{\"type\":\"keyword\"},\"services\":{\"type\":\"keyword\"},\"processes\":{\"type\":\"keyword\"}}}}"
```
```
curl -X PUT "http://192.168.56.30:9200/ird_config" -H "Content-Type: application/json" -d "{\"mappings\":{\"properties\":{\"system_id\":{\"type\":\"keyword\"},\"ip_address\":{\"type\":\"ip\"},\"username\":{\"type\":\"keyword\"},\"password\":{\"type\":\"keyword\"},\"channel_name\":{\"type\":\"text\"}}}}"
```
```
curl -X PUT "http://192.168.56.30:9200/active_alarms" -H "Content-Type: application/json" -d "{\"mappings\":{\"properties\":{\"@timestamp\":{\"type\":\"date\"},\"alarm_id\":{\"type\":\"keyword\"},\"source\":{\"type\":\"keyword\"},\"server_ip\":{\"type\":\"ip\"},\"message\":{\"type\":\"text\"},\"severity\":{\"type\":\"keyword\"},\"channel_name\":{\"type\":\"keyword\"},\"device_name\":{\"type\":\"keyword\"},\"group_id\":{\"type\":\"keyword\"}}}}"
```
```
curl -X PUT "http://192.168.56.30:9200/historical_alarms" -H "Content-Type: application/json" -d "{\"mappings\":{\"properties\":{\"@timestamp\":{\"type\":\"date\"},\"alarm_id\":{\"type\":\"keyword\"},\"source\":{\"type\":\"keyword\"},\"server_ip\":{\"type\":\"ip\"},\"message\":{\"type\":\"text\"},\"severity\":{\"type\":\"keyword\"},\"channel_name\":{\"type\":\"keyword\"},\"device_name\":{\"type\":\"keyword\"},\"group_id\":{\"type\":\"keyword\"}}}}"
```
Get Index Schema
To retrieve the mapping (schema) for a specific index, use the following curl command.

Bash
```curl -X GET "http://192.168.56.30:9200/historical_alarms?pretty"```
Get Index Document
To retrieve documents from an index (e.g., to see active alarms), use the following curl command. This will fetch the top 10 documents by default.

Bash
```curl -X GET "http://192.168.56.30:9200/active_alarms/_search?pretty"```