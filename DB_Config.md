## DB Creation
CREATE DATABASE IF NOT EXISTS vigil_siddhi;
MYSQL_ROOT_PASSWORD: pdek
MYSQL_DATABASE: vigil_siddhi
MYSQL_USER: vigilsiddhi
MYSQL_PASSWORD: vigilsiddhi

-- Use the database
```USE vigil_siddhi;```

-- Table: global_configs
-- Stores configurations for global monitoring blocks.
-- `id` is the primary key and the frontend block_id (e.g., "G.ILOM").
-- `global_block_name` is the display name.
-- `ip_address`, `username`, `password` are for the primary device associated with this global block.
-- If a global block encompasses multiple devices, the *additional* devices will be in `device_ips` linked to this `id`. ```
CREATE TABLE IF NOT EXISTS global_configs (
    id VARCHAR(50) PRIMARY KEY, -- Unique identifier, also acts as frontend block_id (e.g., "G.ILOM")
    global_block_name VARCHAR(100) NOT NULL, -- Display name (e.g., "iLO M")
    ip_address VARCHAR(15), -- IP address of the primary device for this global block
    username VARCHAR(100), -- Username for the primary device
    password VARCHAR(100), -- Password for the primary device (consider encryption in production)
    last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);  ```

-- Table: channel_configs
-- Stores configurations for individual logical channels (e.g., Channel 1, Channel 50).
-- This table defines the *channel*, not its individual data points.
-- The 10 data points (iLO, Router, GV Frames) for each channel will be stored in `device_ips`
-- and linked to this channel_id via `owner_type='CHANNEL'`. ```
CREATE TABLE IF NOT EXISTS channel_configs (
    id INT PRIMARY KEY, -- Unique identifier for each channel (e.g., 1, 50)
    channel_name VARCHAR(100) NOT NULL, -- Display name for the channel (e.g., "Channel 1")
    last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);   ```

-- Table: device_ips
-- Stores IP addresses and credentials for *individual devices* linked to GLOBAL groups or CHANNEL data points.
-- `frontend_block_id` is crucial for linking to specific frontend elements.
-- For global groups like 'iLO M', this table will hold the IPs of additional servers under that group
-- (beyond the primary one stored in global_configs itself).
-- For channel blocks, it will hold IPs for C.101, C.102, etc. (the 10 data points). ```
CREATE TABLE IF NOT EXISTS device_ips (
    ip_id INT AUTO_INCREMENT PRIMARY KEY,
    ip_address VARCHAR(15) NOT NULL,
    username VARCHAR(100),
    password VARCHAR(100), -- In production, consider encrypting this field
    frontend_block_id VARCHAR(50) NOT NULL UNIQUE, -- The specific frontend element ID (e.g., "C.101", "G.ILOM_SERVER_X")
    device_name VARCHAR(100), -- Name of the specific device (e.g., "VS_M Server 1", "iLO Device 1", "Router Port 1", "GV_Frame_X")
    last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    
);  ```

-- Table: ird_configs
-- Stores configurations for individual IRD devices. ```
CREATE TABLE IF NOT EXISTS ird_configs (
    id INT AUTO_INCREMENT PRIMARY KEY, -- Unique ID for the IRD config entry
    ird_ip VARCHAR(15) NOT NULL UNIQUE, -- IP address of the IRD
    username VARCHAR(100),
    password VARCHAR(100), -- In production, consider encrypting this field
    system_id INT, -- Integer value representing the system ID
    channel_name VARCHAR(255), -- Associated channel name, if applicable
    last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);  ```

-- Table: windows_configs
-- Stores configurations for Windows hosts.
-- Note: services and processes are stored in separate tables for normalization. ```
CREATE TABLE IF NOT EXISTS windows_configs (
    id INT AUTO_INCREMENT PRIMARY KEY, -- Unique ID for the Windows config entry
    ip_address VARCHAR(15) NOT NULL UNIQUE, -- IP address of the Windows host
    device_name VARCHAR(100) NOT NULL, -- Display name for the host (e.g., "Laptop")
    username VARCHAR(100),
    password VARCHAR(100), -- In production, consider encrypting this field
    last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);   ````

-- Table: windows_services
-- Stores services to monitor on Windows hosts, linked to windows_configs by ip_address.```
CREATE TABLE IF NOT EXISTS windows_services ( 
    service_id INT AUTO_INCREMENT PRIMARY KEY,
    windows_config_id INT NOT NULL, -- FK to windows_configs.id
    service_name VARCHAR(100) NOT NULL,
    last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (windows_config_id) REFERENCES windows_configs(id) ON DELETE CASCADE,
    UNIQUE KEY unique_service_per_host (windows_config_id, service_name)
);  ```

-- Table: windows_processes
-- Stores processes to monitor on Windows hosts, linked to windows_configs by ip_address. ```
CREATE TABLE IF NOT EXISTS windows_processes (
    process_id INT AUTO_INCREMENT PRIMARY KEY,
    windows_config_id INT NOT NULL, -- FK to windows_configs.id
    process_name VARCHAR(100) NOT NULL,
    last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (windows_config_id) REFERENCES windows_configs(id) ON DELETE CASCADE,
    UNIQUE KEY unique_process_per_host (windows_config_id, process_name)
);  ```

-- Table: kmx_configs
-- Stores configuration for the KMX SNMP polling. ```
CREATE TABLE IF NOT EXISTS kmx_configs (
    id INT AUTO_INCREMENT PRIMARY KEY, -- Unique ID for the KMX config entry
    kmx_ip VARCHAR(15) NOT NULL UNIQUE,
    community VARCHAR(100) NOT NULL,
    base_oid VARCHAR(255) NOT NULL,
    last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);  ```

-- Table: pgm_routing
-- Stores configurations for router monitoring via SNMP. ```
CREATE TABLE IF NOT EXISTS pgm_routing (
    id INT AUTO_INCREMENT PRIMARY KEY, -- Unique ID for the routing entry
    pgm_dest INT NOT NULL, -- Program Destination (integer)
    router_source INT NOT NULL, -- Router Source (integer)
    frontend_block_id VARCHAR(50) NOT NULL UNIQUE, -- The specific frontend element ID (e.g., "C.104")
    domain ENUM('Main', 'Backup') NOT NULL, -- Domain of the routing
    last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);   ```


###########################################################################################

