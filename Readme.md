```markdown
# VigilSiddhi_V1

VigilSiddhi is an extensible, multi-agent, real-time monitoring and alarm management system for broadcast and IT infrastructure. It integrates SNMP, Windows, iLO, IRD, Switch, and custom health checks, providing unified dashboards, alarm consoles, and history analytics. The backend is built on Python (Flask), Elasticsearch, MySQL, and (optionally) Redis Streams for real-time event processing.

---

## Table of Contents

- [Overview](#overview)
- [Key Features](#key-features)
- [How It Works](#how-it-works)
- [Architecture](#architecture)
- [Pros & Benefits](#pros--benefits)
- [API Endpoints](#api-endpoints)
- [Elasticsearch Indexes](#elasticsearch-indexes)
- [MySQL Tables & Schema](#mysql-tables--schema)
- [Redis Streams Usage](#redis-streams-usage)
- [Future Improvements](#future-improvements)
- [Contributing](#contributing)
- [License](#license)

---

## Overview

**VigilSiddhi** is designed for 24x7 operational monitoring of devices such as IRDs, switches, servers (Windows/Linux), HP iLOs, and networked appliances. It provides:

- Real-time health dashboards
- Configurable alarm rules and deduplication
- Alarm history and forensic analytics
- Seamless integration with existing NMS and IT/OT environments

---

## Key Features

- **Multi-Agent Monitoring:** Supports SNMP polling, SNMP traps, Windows/Linux process checks, website/API health, iLO monitoring, and more.
- **Real-Time Alarm Console:** Fast, operator-friendly alarm board with instant raise/clear tracking.
- **Flexible Data Storage:** Uses Elasticsearch for state/config/history, MySQL for structured config, and Redis Streams for event flow.
- **Config-Driven:** Easily add or change monitored devices/channels via JSON/YAML or database.
- **Historical Analytics:** Search, filter, and export alarm/event history for audits or root-cause analysis.
- **Custom UIs:** Frontend dashboard, fullscreen alarm console, IRD/switch views.
- **REST API:** For integration with external systems and custom dashboards.
- **Extensible:** Add new agent types or protocols with minimal code changes.

---

## How It Works

1. **Configuration:**  
   Device/channel/global block configs are loaded from JSON/YAML and stored in MySQL and Elasticsearch.
2. **Agent Operation:**  
   Multiple agents (pollers, trap receivers, Windows, IRD, etc.) run in parallel, collecting health/status data.
3. **Event Processing:**  
   Agents push events to a central processor (via Redis Streams or internal queue), which deduplicates, updates alarm states, and persists to Elasticsearch/MySQL.
4. **Frontend/UI:**  
   Flask web app serves dashboards, alarm consoles, and provides REST APIs for all state/history/config data.
5. **Data Storage:**  
   - **Elasticsearch:** Hot state (active alarms), configs, and full event/alarm history.
   - **MySQL:** Structured configs, device/channel metadata.
   - **Redis Streams:** High-speed alarm/event flow between agents and processor (optional, for scale).
6. **Operator Actions:**  
   Operators use the dashboard/alarm console for real-time visibility and troubleshooting.

---

## Architecture

```plaintext
+-------------------+       +---------------------+       +----------------------+
|   Poller Agents   |-----> |                     |-----> |    Event Processor   |
| SNMP, Windows,    |      |   Redis Streams      |      | (Dedup, State,       |
| IRD, iLO, etc.    |      |  (optional, fast)   |      |  Alarm Logic)        |
+-------------------+       +---------------------+       +----------------------+
         |                           |                                |
         v                           |                                v
+-------------------+        +---------------------+         +------------------+
|  MySQL (Config)   |        | Elasticsearch      |<-------> |  Flask Frontend  |
+-------------------+        | (State, History)   |          |  (Dashboards,    |
                             |                    |          |  Alarm Console)  |
                             +--------------------+          +------------------+
```

---

## Pros & Benefits

- **Unified Monitoring:** All device types, protocols, and alarms in a single UI.
- **High Reliability:** Agents/processors can be restarted independently; Redis Streams prevent data loss.
- **Real-Time Operations:** Sub-second alarm raise/clear, fast UI updates.
- **Scalable & Extensible:** Add new devices, protocols, or features with minimal changes.
- **Audit-Ready:** Complete alarm/event history for compliance and troubleshooting.
- **Customizable:** Configurable block/channel mapping and alarm rules.

---

## API Endpoints

### HTML/UI Pages

| Endpoint                         | Method | Description                         |
|-----------------------------------|--------|-------------------------------------|
| `/`                              | GET    | Main dashboard                      |
| `/alarm_console_fullscreen`       | GET    | Full-screen alarm console           |
| `/ird_overview`                  | GET    | IRD device overview                 |
| `/switch_overview`               | GET    | Switch overview                     |

### REST APIs

| Endpoint                                   | Method | Data Served |
|---------------------------------------------|--------|-------------|
| `/api/v1/get_all_device_ips`                | GET    | List of all device IPs (from MySQL) |
| `/api/v1/get_channel_configs`               | GET    | Channel configs (from MySQL) |
| `/api/v1/get_pgm_routing_configurations`    | GET    | PGM routing configs (from MySQL) |
| `/api/v1/get_kmx_block_status`              | GET    | KMX block overall status (from ES) |
| `/api/v1/get_all_active_alarms`             | GET    | List of all active alarms (from ES) |
| `/api/v1/get_alarm_history`                 | GET/POST | Filtered alarm/event history (from ES) |
| `/api/v1/get_switch_overview_data`          | GET    | Combines switch config (MySQL) and latest stats (ES) |

#### Data Returned:
- All endpoints return JSON objects with clear field names. Error conditions return HTTP error codes and details.

---

## Elasticsearch Indexes

### 1. `active_alarms`
- **Purpose:** Stores all currently active (uncleared) alarms.
- **Schema Example:**
  ```json
  {
    "timestamp": "2024-06-29T12:34:56Z",
    "device_name": "IRD-01",
    "channel_name": "C.101",
    "group_name": "Demux",
    "type": "snmp",
    "severity": "critical",
    "message": "No Signal",
    "frontend_block_id": "c.101",
    "group_id": "demux"
  }
  ```

### 2. `monitor_historical_alarms`
- **Purpose:** Stores every alarm/event ever raised or cleared for audit/history.
- **Schema Example:** Same as above, with additional fields for clear time, operator actions, etc.

### 3. `config`
- **Purpose:** Stores device/channel/global block configurations.
- **Schema Example:**
  ```json
  {
    "block_id": "C.101",
    "device_ip": "10.100.1.22",
    "name": "IRD-01",
    "type": "ird",
    "monitor_params": { ... }
  }
  ```

### 4. `historical_alarms` and others
- **Purpose:** Device-specific or custom historical data.
- **Schema:** Contextual to the device (ex: switch stats, interface list, etc.)

---

## MySQL Tables & Schema

### 1. `device_ips`
| Column      | Type        | Description  |
|-------------|------------|--------------|
| id          | INT         | PK           |
| ip_address  | VARCHAR     | Device IP    |
| ...         | ...         | ...          |

### 2. `channel_configs`
| Column      | Type        | Description  |
|-------------|------------|--------------|
| id          | INT         | PK           |
| channel     | VARCHAR     | Channel name |
| ...         | ...         | ...          |

### 3. `pgm_routing_configs`
| Column      | Type        | Description  |
|-------------|------------|--------------|
| id          | INT         | PK           |
| source      | VARCHAR     | Source info  |
| ...         | ...         | ...          |

### 4. `switch_configs`
| Column      | Type        | Description  |
|-------------|------------|--------------|
| id          | INT         | PK           |
| switch_ip   | VARCHAR     | Switch IP    |
| hostname    | VARCHAR     | Switch name  |
| model       | VARCHAR     | Model        |

**Other Tables:**  
Additional tables as needed for device metadata, historical configs, or agent assignments.

---

## Redis Streams Usage

- **Purpose:**  
  Used for fast, reliable passing of events from agents to the central event processor. Ensures events are processed in order and can be replayed if needed.
- **Stream Name:**  
  Typically `alarm_events` or `monitor_events`
- **Message Schema:**
  ```json
  {
    "timestamp": "2024-06-29T12:34:56Z",
    "agent_id": "snmp_poller_1",
    "event_type": "alarm",
    "device_name": "IRD-01",
    "severity": "critical",
    "message": "No Signal"
  }
  ```
- **Advantages:**  
  High throughput, durability, and decoupling between agents and processor.

---

## Future Improvements

- **WebSocket Support:**  
  Push real-time alarm updates to the frontend for instant operator visibility.
- **Role-Based Access Control:**  
  User authentication and role management for multi-operator environments.
- **Agent Auto-Registration:**  
  Dynamic discovery and registration of new agents/services.
- **Kubernetes/Dockerization:**  
  Containerized deployment for high-availability and ease of scaling.
- **Self-Healing & Auto-Remediation:**  
  Trigger automated corrective actions on alarm (e.g., restart service).
- **Integrations:**  
  SNMP trap forwarding, email/SMS notifications, integration with other NMS/BMS/EMS.
- **Advanced Analytics:**  
  Root-cause analysis, anomaly detection, and predictive maintenance.
- **Mobile UI:**  
  Responsive mobile dashboard for on-the-go monitoring.
- **Config Editor:**  
  In-app editor for device/channel/global config, with audit trails.
- **Granular Audit Logs:**  
  Operator actions, alarm acknowledgments, and clearances tracked in detail.

---

## Contributing

1. Fork this repo and create your feature branch (`git checkout -b feature/new-agent`).
2. Commit your changes with clear messages.
3. Push to the branch and open a pull request.

---

## License

MIT License. See `LICENSE` file for details.

---

## Contact

For queries, enhancements, or support, please open an issue or contact the maintainer: [pdek1991](https://github.com/pdek1991)

---
```
*This README gives a complete, professional overview for developers, operators, and contributors. Sections and schemas can be extended as your repo evolves.*
