import mysql.connector
from mysql.connector import Error
import logging
from datetime import datetime

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

class MySQLManager:
    """
    Manages connections and operations with the MySQL database for configuration data.
    """
    def __init__(self, host, database, user, password):
        self.host = host
        self.database = database
        self.user = user
        self.password = password
        self.connection = None
        self._connect()

    def _connect(self):
        """Establishes a connection to the MySQL database."""
        try:
            self.connection = mysql.connector.connect(
                host=self.host,
                database=self.database,
                user=self.user,
                password=self.password
            )
            if self.connection.is_connected():
                db_info = self.connection.get_server_info()
                logging.info(f"Successfully connected to MySQL Database... Server version: {db_info}")
        except Error as e:
            logging.error(f"Error while connecting to MySQL: {e}")
            self.connection = None # Ensure connection is None on failure

    def reconnect(self):
        """Forces a re-establishment of the MySQL database connection."""
        self.close() # Close any existing connection first
        self._connect() # Then establish a new one
        if self.connection and self.connection.is_connected():
            logging.info("MySQL connection successfully re-established.")
        else:
            logging.error("Failed to re-establish MySQL connection.")

    def _get_cursor(self):
        """Returns a cursor, attempting to reconnect if the connection is lost."""
        if not self.connection or not self.connection.is_connected():
            logging.warning("MySQL connection lost. Attempting to reconnect...")
            self._connect()
        if self.connection and self.connection.is_connected():
            return self.connection.cursor(dictionary=True) # Return dictionaries for easier access
        else:
            logging.error("Failed to establish MySQL connection. Cannot get cursor.")
            return None

    def close(self):
        """Closes the MySQL database connection."""
        if self.connection and self.connection.is_connected():
            self.connection.close()
            self.connection = None # Set to None after closing
            logging.info("MySQL connection closed.")

    def _execute_query(self, query, params=None, fetch_one=False, fetch_all=False, commit=False):
        """
        Executes a SQL query.
        :param query: The SQL query string.
        :param params: A tuple or dictionary of parameters for the query.
        :param fetch_one: True to fetch one row, False otherwise.
        :param fetch_all: True to fetch all rows, False otherwise.
        :param commit: True to commit the transaction, False otherwise.
        :return: Result of fetch operation, or True/False for success/failure of DML.
        """
        cursor = self._get_cursor()
        if not cursor:
            return None

        try:
            cursor.execute(query, params or ())
            if commit:
                self.connection.commit()
                logging.debug("Query committed.")
                return True
            if fetch_one:
                return cursor.fetchone()
            if fetch_all:
                return cursor.fetchall()
            return True # For DML operations that don't fetch
        except Error as e:
            logging.error(f"Error executing query: {e}. Query: {query}, Params: {params}")
            if commit:
                self.connection.rollback()
                logging.warning("Transaction rolled back due to error.")
            return False
        finally:
            if cursor:
                cursor.close()

    # --- CRUD operations for global_configs ---
    def add_global_config(self, config_id, global_block_name, ip_address=None, username=None, password=None, device_name=None):
        query = "INSERT INTO global_configs (id, global_block_name, ip_address, username, password, device_name) VALUES (%s, %s, %s, %s, %s, %s)"
        params = (config_id, global_block_name, ip_address, username, password, device_name)
        return self._execute_query(query, params, commit=True)

    def get_global_configs(self):
        query = "SELECT id, global_block_name, ip_address, username, password, device_name FROM global_configs"
        return self._execute_query(query, fetch_all=True)

    def get_global_config_by_id(self, config_id):
        query = "SELECT id, global_block_name, ip_address, username, password, device_name FROM global_configs WHERE id = %s"
        return self._execute_query(query, (config_id,), fetch_one=True)

    def update_global_config(self, config_id, global_block_name, ip_address=None, username=None, password=None, device_name=None):
        query = "UPDATE global_configs SET global_block_name = %s, ip_address = %s, username = %s, password = %s, device_name = %s WHERE id = %s"
        params = (global_block_name, ip_address, username, password, device_name, config_id)
        return self._execute_query(query, params, commit=True)

    def delete_global_config(self, config_id):
        # NOTE: With device_ips no longer having owner_type/owner_id, cascade deletion
        # from global_configs to specific device_ips is not automatic here.
        # Application logic needs to handle deleting associated device_ips if they map
        # semantically to this global config ID (e.g., frontend_block_id starting with G.ID).
        query = "DELETE FROM global_configs WHERE id = %s"
        return self._execute_query(query, (config_id,), commit=True)

    # --- CRUD operations for channel_configs ---
    def add_channel_config(self, channel_id, channel_name, pgm=None):
        query = "INSERT INTO channel_configs (id, channel_name, pgm) VALUES (%s, %s, %s)"
        params = (channel_id, channel_name, pgm)
        return self._execute_query(query, params, commit=True)

    def get_channel_configs(self):
        query = "SELECT id, channel_name, pgm FROM channel_configs"
        return self._execute_query(query, fetch_all=True)

    def get_channel_config_by_id(self, channel_id):
        query = "SELECT id, channel_name, pgm FROM channel_configs WHERE id = %s"
        return self._execute_query(query, (channel_id,), fetch_one=True)

    def update_channel_config(self, channel_id, channel_name, pgm=None):
        query = "UPDATE channel_configs SET channel_name = %s, pgm = %s WHERE id = %s"
        params = (channel_name, pgm, channel_id)
        return self._execute_query(query, params, commit=True)

    def delete_channel_config(self, channel_id):
        # NOTE: With device_ips no longer having owner_type/owner_id, cascade deletion
        # from channel_configs to specific device_ips is not automatic here.
        # Application logic needs to handle deleting associated device_ips if they map
        # semantically to this channel ID (e.g., frontend_block_id starting with C.CHANNEL_ID).
        query = "DELETE FROM channel_configs WHERE id = %s"
        return self._execute_query(query, (channel_id,), commit=True)

    # --- CRUD operations for device_ips (modified based on DESCRIBE output) ---
    def add_device_ip(self, ip_address, username, password, frontend_block_id, device_name, group_name):
        query = """
            INSERT INTO device_ips (ip_address, username, password, frontend_block_id, device_name, Group_name)
            VALUES (%s, %s, %s, %s, %s, %s)
        """
        params = (ip_address, username, password, frontend_block_id, device_name, group_name)
        return self._execute_query(query, params, commit=True)

    def get_all_device_ips(self):
        # Updated to select the new Group_name column
        query = "SELECT ip_id, ip_address, username, password, frontend_block_id, device_name, Group_name FROM device_ips"
        return self._execute_query(query, fetch_all=True)
    
    def get_device_ip_by_frontend_block_id(self, frontend_block_id):
        # Updated to select the new Group_name column
        query = "SELECT ip_id, ip_address, username, password, frontend_block_id, device_name, Group_name FROM device_ips WHERE frontend_block_id = %s"
        return self._execute_query(query, (frontend_block_id,), fetch_one=True)

    def update_device_ip(self, ip_id, ip_address, username, password, frontend_block_id, device_name, group_name):
        # Updated to include Group_name in the update
        query = """
            UPDATE device_ips SET ip_address = %s, username = %s, password = %s, frontend_block_id = %s, device_name = %s, Group_name = %s
            WHERE ip_id = %s
        """
        params = (ip_address, username, password, frontend_block_id, device_name, group_name, ip_id)
        return self._execute_query(query, params, commit=True)

    def delete_device_ip(self, ip_id):
        query = "DELETE FROM device_ips WHERE ip_id = %s"
        return self._execute_query(query, (ip_id,), commit=True)

    # --- CRUD operations for ird_configs (modified based on DESCRIBE output) ---
    def add_ird_config(self, ird_ip, description, username, password, system_id, channel_name=None):
        query = "INSERT INTO ird_configs (ird_ip, description, username, password, system_id, channel_name) VALUES (%s, %s, %s, %s, %s, %s)"
        params = (ird_ip, description, username, password, system_id, channel_name)
        return self._execute_query(query, params, commit=True)

    def get_ird_configs(self):
        query = "SELECT id, ird_ip, description, username, password, system_id, channel_name FROM ird_configs"
        return self._execute_query(query, fetch_all=True)
    
    def get_ird_config_by_id(self, config_id):
        query = "SELECT id, ird_ip, description, username, password, system_id, channel_name FROM ird_configs WHERE id = %s"
        return self._execute_query(query, (config_id,), fetch_one=True)

    def get_ird_config_by_ip(self, ird_ip):
        query = "SELECT id, ird_ip, description, username, password, system_id, channel_name FROM ird_configs WHERE ird_ip = %s"
        return self._execute_query(query, (ird_ip,), fetch_one=True)

    def update_ird_config(self, config_id, ird_ip, description, username, password, system_id, channel_name=None):
        query = "UPDATE ird_configs SET ird_ip = %s, description = %s, username = %s, password = %s, system_id = %s, channel_name = %s WHERE id = %s"
        params = (ird_ip, description, username, password, system_id, channel_name, config_id)
        return self._execute_query(query, params, commit=True)

    def delete_ird_config(self, config_id):
        query = "DELETE FROM ird_configs WHERE id = %s"
        return self._execute_query(query, (config_id,), commit=True)

    # --- CRUD operations for windows_configs ---
    def add_windows_config(self, ip_address, device_name, username, password):
        query = "INSERT INTO windows_configs (ip_address, device_name, username, password) VALUES (%s, %s, %s, %s)"
        params = (ip_address, device_name, username, password)
        return self._execute_query(query, params, commit=True)

    def get_windows_configs(self):
        query = "SELECT id, ip_address, device_name, username, password FROM windows_configs"
        return self._execute_query(query, fetch_all=True)
    
    def get_windows_config_by_id(self, config_id):
        query = "SELECT id, ip_address, device_name, username, password FROM windows_configs WHERE id = %s"
        return self._execute_query(query, (config_id,), fetch_one=True)
    
    def get_windows_config_by_ip(self, ip_address):
        query = "SELECT id, ip_address, device_name, username, password FROM windows_configs WHERE ip_address = %s"
        return self._execute_query(query, (ip_address,), fetch_one=True)

    def update_windows_config(self, config_id, ip_address, device_name, username, password):
        query = "UPDATE windows_configs SET ip_address = %s, device_name = %s, username = %s, password = %s WHERE id = %s"
        params = (ip_address, device_name, username, password, config_id)
        return self._execute_query(query, params, commit=True)

    def delete_windows_config(self, config_id):
        # Cascade delete is handled by MySQL for services and processes if foreign keys are set up correctly.
        query = "DELETE FROM windows_configs WHERE id = %s"
        return self._execute_query(query, (config_id,), commit=True)

    # --- CRUD operations for windows_services ---
    def add_windows_service(self, windows_config_id, service_name):
        query = "INSERT INTO windows_services (windows_config_id, service_name) VALUES (%s, %s)"
        params = (windows_config_id, service_name)
        return self._execute_query(query, params, commit=True)

    def get_windows_services(self, windows_config_id):
        query = "SELECT service_id, windows_config_id, service_name FROM windows_services WHERE windows_config_id = %s"
        return self._execute_query(query, (windows_config_id,), fetch_all=True)

    # NEW: Method to get all Windows services, needed for config.html
    def get_all_windows_services(self):
        query = "SELECT service_id, windows_config_id, service_name FROM windows_services"
        return self._execute_query(query, fetch_all=True)

    def delete_windows_service(self, service_id):
        query = "DELETE FROM windows_services WHERE service_id = %s"
        return self._execute_query(query, (service_id,), commit=True)
    
    def delete_windows_services_by_windows_config_id(self, windows_config_id):
        query = "DELETE FROM windows_services WHERE windows_config_id = %s"
        return self._execute_query(query, (windows_config_id,), commit=True)

    # --- CRUD operations for windows_processes ---
    def add_windows_process(self, windows_config_id, process_name):
        query = "INSERT INTO windows_processes (windows_config_id, process_name) VALUES (%s, %s)"
        params = (windows_config_id, process_name)
        return self._execute_query(query, params, commit=True)

    def get_windows_processes(self, windows_config_id):
        query = "SELECT process_id, windows_config_id, process_name FROM windows_processes WHERE windows_config_id = %s"
        return self._execute_query(query, (windows_config_id,), fetch_all=True)
    
    # NEW: Method to get all Windows processes, needed for config.html
    def get_all_windows_processes(self):
        query = "SELECT process_id, windows_config_id, process_name FROM windows_processes"
        return self._execute_query(query, fetch_all=True)


    def delete_windows_process(self, process_id):
        query = "DELETE FROM windows_processes WHERE process_id = %s"
        return self._execute_query(query, (process_id,), commit=True)

    def delete_windows_processes_by_windows_config_id(self, windows_config_id):
        query = "DELETE FROM windows_processes WHERE windows_config_id = %s"
        return self._execute_query(query, (windows_config_id,), commit=True)

    # --- CRUD operations for kmx_configs (modified based on DESCRIBE output) ---
    def add_kmx_config(self, kmx_ip, community, base_oid, device_name=None):
        query = "INSERT INTO kmx_configs (kmx_ip, community, base_oid, device_name) VALUES (%s, %s, %s, %s)"
        params = (kmx_ip, community, base_oid, device_name)
        return self._execute_query(query, params, commit=True)

    def get_kmx_configs(self):
        query = "SELECT id, kmx_ip, community, base_oid, device_name FROM kmx_configs"
        return self._execute_query(query, fetch_all=True)
    
    def get_kmx_config_by_id(self, config_id):
        query = "SELECT id, kmx_ip, community, base_oid, device_name FROM kmx_configs WHERE id = %s"
        return self._execute_query(query, (config_id,), fetch_one=True)

    def update_kmx_config(self, config_id, kmx_ip, community, base_oid, device_name=None):
        query = "UPDATE kmx_configs SET kmx_ip = %s, community = %s, base_oid = %s, device_name = %s WHERE id = %s"
        params = (kmx_ip, community, base_oid, device_name, config_id)
        return self._execute_query(query, params, commit=True)

    def delete_kmx_config(self, config_id):
        query = "DELETE FROM kmx_configs WHERE id = %s"
        return self._execute_query(query, (config_id,), commit=True)

    # --- CRUD operations for pgm_routing ---
    def add_pgm_routing_config(self, pgm_dest, router_source, frontend_block_id, domain, channel_name=None):
        query = "INSERT INTO pgm_routing (pgm_dest, router_source, frontend_block_id, domain, channel_name) VALUES (%s, %s, %s, %s, %s)"
        params = (pgm_dest, router_source, frontend_block_id, domain, channel_name)
        return self._execute_query(query, params, commit=True)

    def get_pgm_routing_configs(self):
        # Includes 'id' as it's typically a primary key and useful for updates/deletes
        query = "SELECT id, pgm_dest, router_source, frontend_block_id, domain, channel_name FROM pgm_routing"
        return self._execute_query(query, fetch_all=True)
    
    def get_pgm_routing_config_by_id(self, config_id):
        query = "SELECT id, pgm_dest, router_source, frontend_block_id, domain, channel_name FROM pgm_routing WHERE id = %s"
        return self._execute_query(query, (config_id,), fetch_one=True)
    
    def get_pgm_routing_config_by_pgm_dest(self, pgm_dest): # Added for direct pgm_dest lookup
        query = "SELECT id, pgm_dest, router_source, frontend_block_id, domain, channel_name FROM pgm_routing WHERE pgm_dest = %s"
        return self._execute_query(query, (pgm_dest,), fetch_one=True)

    def update_pgm_routing_config(self, config_id, pgm_dest, router_source, frontend_block_id, domain, channel_name=None):
        query = "UPDATE pgm_routing SET pgm_dest = %s, router_source = %s, frontend_block_id = %s, domain = %s, channel_name = %s WHERE id = %s"
        params = (pgm_dest, router_source, frontend_block_id, domain, channel_name, config_id)
        return self._execute_query(query, params, commit=True)

    def delete_pgm_routing_config(self, config_id):
        query = "DELETE FROM pgm_routing WHERE id = %s"
        return self._execute_query(query, (config_id,), commit=True)

    # --- CRUD operations for playoutmv_configs ---
    def add_playoutmv_config(self, ip_address, name, api_endpoint, username, password):
        query = "INSERT INTO playoutmv_configs (ip_address, name, api_endpoint, username, password) VALUES (%s, %s, %s, %s, %s)"
        params = (ip_address, name, api_endpoint, username, password)
        return self._execute_query(query, params, commit=True)

    def get_playoutmv_configs(self):
        query = "SELECT id, ip_address, name, api_endpoint, username, password FROM playoutmv_configs"
        return self._execute_query(query, fetch_all=True)
    
    def get_playoutmv_config_by_id(self, config_id):
        query = "SELECT id, ip_address, name, api_endpoint, username, password FROM playoutmv_configs WHERE id = %s"
        return self._execute_query(query, (config_id,), fetch_one=True)
    
    def get_playoutmv_config_by_ip(self, ip_address):
        query = "SELECT id, ip_address, name, api_endpoint, username, password FROM playoutmv_configs WHERE ip_address = %s"
        return self._execute_query(query, (ip_address,), fetch_one=True)


    def update_playoutmv_config(self, config_id, ip_address, name, api_endpoint, username, password):
        query = "UPDATE playoutmv_configs SET ip_address = %s, name = %s, api_endpoint = %s, username = %s, password = %s WHERE id = %s"
        params = (ip_address, name, api_endpoint, username, password, config_id)
        return self._execute_query(query, params, commit=True)

    def delete_playoutmv_config(self, config_id):
        query = "DELETE FROM playoutmv_configs WHERE id = %s"
        return self._execute_query(query, (config_id,), commit=True)

    # --- CRUD operations for switch_configs ---
    def add_switch_config(self, switch_ip, hostname, domain, community, model, frontend_block_id):
        query = """
            INSERT INTO switch_configs (switch_ip, hostname, domain, community, model, frontend_block_id)
            VALUES (%s, %s, %s, %s, %s, %s)
        """
        params = (switch_ip, hostname, domain, community, model, frontend_block_id)
        return self._execute_query(query, params, commit=True)

    def get_switch_configs(self):
        query = "SELECT switch_ip, hostname, domain, community, model, frontend_block_id FROM switch_configs" # Removed LIMIT 5
        return self._execute_query(query, fetch_all=True)

    def get_switch_config_by_ip(self, switch_ip):
        query = "SELECT switch_ip, hostname, domain, community, model, frontend_block_id FROM switch_configs WHERE switch_ip = %s"
        return self._execute_query(query, (switch_ip,), fetch_one=True)

    def update_switch_config(self, switch_ip, hostname, domain, community, model, frontend_block_id):
        query = """
            UPDATE switch_configs SET hostname = %s, domain = %s, community = %s, model = %s, frontend_block_id = %s
            WHERE switch_ip = %s
        """
        params = (hostname, domain, community, model, frontend_block_id, switch_ip)
        return self._execute_query(query, params, commit=True)

    def delete_switch_config(self, switch_ip):
        query = "DELETE FROM switch_configs WHERE switch_ip = %s"
        return self._execute_query(query, (switch_ip,), commit=True)

