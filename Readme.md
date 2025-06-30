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