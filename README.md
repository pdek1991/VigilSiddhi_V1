# VigilSiddhi_V1

**VigilSiddhi** is an extensible, multi-agent, real-time monitoring and alarm management system for broadcast, network, and IT infrastructure. It supports modern architectures with SNMP, Windows, iLO, IRD, Switch, and custom health checks, providing unified dashboards, alarm consoles, and history analytics. The backend is powered by Python (Flask), Elasticsearch, MySQL, Redis Streams, WebSockets, and push notifications.

---

## Table of Contents

- [Overview](#overview)
- [Key Features](#key-features)
- [How It Works](#how-it-works)
- [Architecture](#architecture)
- [NMS, IT, and OT Environments](#nms-it-and-ot-environments)
- [Pros & Benefits](#pros--benefits)
- [API Endpoints](#api-endpoints)
- [Elasticsearch Indexes & Schema](#elasticsearch-indexes--schema)
- [MySQL Tables & Roles](#mysql-tables--roles)
- [Redis Streams & Groups](#redis-streams--groups)
- [Real-Time & Notifications](#real-time--notifications)
- [Future Improvements](#future-improvements)
- [Contributing](#contributing)
- [License](#license)

---

## Overview

VigilSiddhi is designed for 24x7 operational monitoring of devices such as IRDs, switches, servers (Windows/Linux), HP iLOs, and networked appliances. It provides:

- Real-time health dashboards
- Configurable alarm rules and deduplication
- Alarm history and forensic analytics
- Seamless integration with NMS, IT, and OT environments

---

## Key Features

- **Multi-Agent Monitoring:** SNMP polling, SNMP traps, Windows/Linux process checks, website/API health, iLO monitoring, IRD, switch monitoring, and more.
- **Real-Time Alarm Console:** Operator-friendly alarm board with live updates.
- **WebSocket Live Updates:** Instant push of alarm/status to dashboards via WebSocket.
- **Event-Based Notifications:** Telegram and Email alerts for each event (raise/clear).
- **Flexible Data Storage:** Elasticsearch for state/config/history, MySQL for structured config, Redis Streams for event flow.
- **Config-Driven:** Add/change monitored devices/channels via JSON/YAML or DB.
- **Historical Analytics:** Search, filter, and export alarm/event history.
- **REST API:** Integration with external systems and custom dashboards.
- **Extensible:** Add new agent types/protocols with minimal code changes.

---

## How It Works

1. **Configuration:**  
   Device/channel/global block configs are loaded from JSON/YAML and stored in MySQL and Elasticsearch.
2. **Agent Operation:**  
   Multiple agents (pollers, trap receivers, Windows, IRD, etc.) run in parallel, collecting health/status data.
3. **Event Processing:**  
   Agents push events to Redis Streams, which are consumed by processor groups for deduplication, state updates, and delivery to Elasticsearch/MySQL.
4. **Frontend/UI:**  
   Flask serves dashboards, alarm consoles, and REST APIs.
5. **Live Updates & Alerts:**  
   - Consumers notify WebSocket broadcaster and send Telegram/Email notifications per event.
6. **Operator Actions:**  
   Operators use the dashboard/alarm console for real-time, actionable visibility.

---

## Architecture

```plaintext
+-------------------+      +---------------------+       +----------------------+
|   Poller Agents   |----->| Redis Streams       |-----> |    Event Processor   |
| SNMP, iLO, IRD,   |      | (agent-specific,    |      | (Dedup, State,       |
| Switch, Windows   |      | grouped, persistent)|      |  Alarm Logic,        |
+-------------------+      +---------------------+      |  WebSocket/Notif.)   |
         |                           |                      |   |        |
         v                           |                      v   v        v
+-------------------+        +---------------------+   +---------------------+
|  MySQL (Config)   |        | Elasticsearch      |   | WebSocket Notifier,  |
+-------------------+        | (State, History)   |   | Telegram, Email     |
                             |                    |   +---------------------+
                             +--------------------+
```

---

## NMS, IT, and OT Environments

- **NMS (Network Management System):**  
  A platform for monitoring, managing, and controlling network devices (switches, routers, servers, etc.). NMS helps with fault management, performance monitoring, and configuration.
- **IT (Information Technology):**  
  All traditional computing, networking, and storage systems—servers, desktops, network equipment, and associated software.
- **OT (Operational Technology):**  
  Systems and equipment that monitor or control physical devices, processes, and events in industries (e.g., broadcast, manufacturing, utilities). Includes industrial control systems, SCADA, PLCs, and other real-world device management.

**VigilSiddhi** unifies monitoring and alarm management across all these environments.

---

## Pros & Benefits

- **Unified Monitoring:** All device types, protocols, and alarms in a single UI.
- **High Reliability:** Agent/process decoupling via Redis Streams.
- **Real-Time Operations:** Sub-second alarm updates with WebSockets.
- **Scalable & Extensible:** Add new devices/protocols/features easily.
- **Audit-Ready:** Full alarm/event history in Elasticsearch.
- **Customizable:** Configurable mapping and notification rules.
- **Proactive Response:** Telegram and email alerts for critical events.

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
| `/api/v1/get_all_device_ips`                | GET    | List of all device IPs (MySQL) |
| `/api/v1/get_channel_configs`               | GET    | Channel configs (MySQL)        |
| `/api/v1/get_pgm_routing_configurations`    | GET    | PGM routing configs (MySQL)    |
| `/api/v1/get_kmx_block_status`              | GET    | KMX block overall status (ES)  |
| `/api/v1/get_all_active_alarms`             | GET    | All active alarms (ES)         |
| `/api/v1/get_alarm_history`                 | GET/POST | Filtered alarm/event history (ES) |
| `/api/v1/get_switch_overview_data`          | GET    | Switch config (MySQL) + stats (ES) |

### WebSocket

| Endpoint              | Description                          |
|-----------------------|--------------------------------------|
| `/ws/alarms` (example)| Real-time alarm/status updates for dashboards and alarm console |

---

## Elasticsearch Indexes & Schema

### 1. `monitor_historical_alarms`
**Purpose:** Complete alarm/event history  
**Schema:**
- `alarm_id` (keyword)
- `timestamp` (date)
- `message` (text)
- `device_name` (keyword)
- `block_id` (keyword)
- `severity` (keyword)
- `type` (keyword)
- `device_ip` (ip)
- `group_name` (keyword)

### 2. `ird_trend_data`
**Purpose:** IRD trend/statistics  
**Schema:**  
- `channel_name` (keyword)
- `system_id` (integer)
- `C_N_margin` (float)
- `signal_strength` (float)
- `timestamp` (date)
- `ip_address` (ip)

### 3. `ird_configurations`
**Purpose:** IRD configuration and health  
**Schema:**  
- `ird_ip` (keyword)
- `channel_name` (keyword)
- `system_id` (integer)
- `freq` (float)
- `SR` (float)
- `Pol` (keyword)
- `C_N` (float)
- `signal_strength` (float)
- `moip_status` (keyword)
- `output_bitrate` (float)
- `input_bitrate` (float)
- `last_updated` (date)

### 4. `switch_overview`
**Purpose:** Switch + interface health  
**Schema:**  
- `switch_ip` (keyword)
- `hostname` (keyword)
- `interfaces` (nested: name, alias, admin_status, oper_status, vlan, input/output_octets)
- `timestamp` (date)

---

## MySQL Tables & Roles

- `device_ips`: All registered device IPs
- `channel_configs`: Channel mapping, block/channel naming, etc.
- `pgm_routing_configs`: PGM router configuration
- `switch_configs`: Switch IP, hostname, model, etc.

**Roles:**  
- Source of truth for device/channel metadata.
- Used for API dropdowns, config reloads, and agent assignment.

---

## Redis Streams & Groups

### Stream Names

A selection of agent-specific streams (see `REDIS_STREAM_config.md` and code):

- `vs:agent:cisco_sw_status`
- `vs:agent:enc_ilo_status`, `vs:agent:enc_iloM_status`, `vs:agent:enc_iloP_status`, `vs:agent:enc_iloB_status`
- `vs:agent:iloM_status`, `vs:agent:iloP_status`, `vs:agent:iloB_status`
- `vs:agent:playoutmv_status`
- `vs:agent:windows_status`
- `vs:agent:ird_config_status`, `vs:agent:ird_trend_status`
- `vs:agent:zixi_status`
- `vs:agent:nexus_sw_status`
- `vs:agent:gv_da_status`
- `vs:agent:pgm_router_status`
- `vs:agent:kmx_status`

#### Consumer Groups

- `<agent_stream>:group:<role>` (e.g., `vs:agent:kmx_status:group:kmx_processor`)
- `websocket_broadcaster` and `es_ingester` for notifying frontends and persisting to ES

#### Example

```python
# Example publishing to a Redis stream
r.xadd("vs:agent:kmx_status", {"data": json.dumps(payload).encode('utf-8')})

# Example consumer for ES and WebSocket
r.xgroup_create("vs:agent:kmx_status", "es_ingester", id='$', mkstream=True)
r.xgroup_create("vs:agent:kmx_status", "websocket_broadcaster", id='$', mkstream=True)
```

---

## Real-Time & Notifications

- **WebSocket:**  
  All status and alarm updates are pushed to `/ws/alarms` endpoint for UIs.
- **Telegram & Email:**  
  Each event can trigger Telegram messages (to groups/users) and emails (to operators/teams).
  Notification handlers are called on each event, with rules configurable per agent, block, or severity.

---

## Future Improvements

- **Role-Based Access Control:**  
  User authentication and role management.
- **Agent Auto-Registration:**  
  Dynamic agent/service onboarding.
- **Containerization:**  
  Docker/K8s deployment.
- **Self-Healing & Auto-Remediation:**  
  Automated actions on alarms.
- **Integrations:**  
  Additional notification channels (SMS, Slack, Teams), advanced rules.
- **Mobile UI:**  
  Responsive dashboard for mobile.
- **Config Editor & Audit Logs:**  
  In-app config management and operator audit trails.
- **PWA Integration for Live Push Notifications:**  
  Implement Progressive Web App (PWA) capabilities so users can install the dashboard on any device and receive real-time push notifications even when the app is closed. This includes:
  - Service Workers for background sync and push
  - Integration with browser/device notification APIs
  - Installation prompt for mobile/desktop
  - Offline support and improved mobile UX

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

**For more, see the full codebase:** [VigilSiddhi_V1 on GitHub](https://github.com/pdek1991/VigilSiddhi_V1)
