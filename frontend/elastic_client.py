# elastic_client.py
import logging
from elasticsearch import Elasticsearch, NotFoundError
import json
import os
import time
import hashlib # For generating stable IDs
from datetime import datetime # Import datetime for timestamping

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

class ElasticManager:
    def __init__(self, hosts, timeout=10, max_retries=5, retry_interval=2):
        self.hosts = hosts
        self.timeout = timeout # This timeout will still be available for other methods if needed
        self.max_retries = max_retries
        self.retry_interval = retry_interval
        self.es = self._connect_with_retry()
        self.config_index = "monitor_configs"
        # Renamed to match the usage in consumers for consistency
        self.active_alarms_index = "active_alarms" 
        self.historical_alarms_index = "monitor_historical_alarms" # Corrected to match user's desired index name

        if self.es:
            self._create_index_if_not_exists(self.config_index)
            self._create_index_if_not_exists(self.active_alarms_index)
            self._create_index_if_not_exists(self.historical_alarms_index)

    def _connect_with_retry(self):
        for i in range(self.max_retries):
            try:
                # Removed 'timeout' keyword argument from Elasticsearch constructor
                # Timeout for individual requests can be passed to methods like es.search()
                es_client = Elasticsearch(self.hosts) 
                if es_client.ping():
                    logging.info("Successfully connected to Elasticsearch.")
                    return es_client
                else:
                    logging.warning(f"Elasticsearch ping failed. Retrying in {self.retry_interval} seconds...")
                    time.sleep(self.retry_interval)
            except Exception as e:
                logging.error(f"Error connecting to Elasticsearch (attempt {i+1}/{self.max_retries}): {e}")
                time.sleep(self.retry_interval)
        logging.error("Failed to connect to Elasticsearch after multiple retries.")
        return None

    def _create_index_if_not_exists(self, index_name):
        if not self.es:
            return
        if not self.es.indices.exists(index=index_name):
            try:
                # Basic mapping for alarms to ensure @timestamp is a date
                # and relevant fields are keyword for filtering/aggregation
                if index_name in [self.active_alarms_index, self.historical_alarms_index]:
                    mappings = {
                        "properties": {
                            "timestamp": {"type": "date"}, # Changed from @timestamp to timestamp
                            "alarm_id": {"type": "keyword"},
                            "source": {"type": "keyword"}, # e.g., 'ilo', 'windows', 'ird', 'kmx'
                            "device_ip": {"type": "ip"}, # Changed from server_ip to device_ip as per new schema
                            "message": {"type": "text"},
                            "severity": {"type": "keyword"},
                            "channel_name": {"type": "keyword"},
                            "device_name": {"type": "keyword"},
                            "group_name": {"type": "keyword"}, # Changed from group_id to group_name
                            "type": {"type": "keyword"}, # e.g., 'ilo_health', 'kmx_config', 'pgm_routing', etc. (agent_type)
                            "block_id": {"type": "keyword"} # Added block_id as per new schema
                        }
                    }
                    self.es.indices.create(index=index_name, body={"mappings": mappings})
                    logging.info(f"Index '{index_name}' created with mapping successfully.")
                else:
                    self.es.indices.create(index=index_name)
                    logging.info(f"Index '{index_name}' created successfully.")
            except Exception as e:
                logging.error(f"Error creating index '{index_name}': {e}")
        else:
            logging.info(f"Index '{index_name}' already exists.")

    def get_client(self):
        return self.es

    def get_config(self, config_id, index=None):
        if not self.es:
            logging.error("Elasticsearch client not available. Cannot get config.")
            return None
        target_index = index if index else self.config_index
        try:
            response = self.es.get(index=target_index, id=config_id)
            return response['_source']
        except NotFoundError:
            logging.warning(f"Configuration with ID '{config_id}' not found in index '{target_index}'.")
            return None
        except Exception as e:
            logging.error(f"Error fetching configuration '{config_id}' from index '{target_index}': {e}")
            return None

    def store_config(self, config_id, config_data, index=None):
        if not self.es:
            logging.error("Elasticsearch client not available. Cannot store config.")
            return False
        target_index = index if index else self.config_index
        try:
            response = self.es.index(index=target_index, id=config_id, document=config_data)
            logging.info(f"Configuration '{config_id}' stored/updated in index '{target_index}': {response['result']}")
            return True
        except Exception as e:
            logging.error(f"Error storing configuration '{config_id}' in index '{target_index}': {e}")
            return False

    def load_initial_configs(self, config_file_paths):
        if not self.es:
            logging.error("Elasticsearch client not available. Cannot load initial configurations.")
            return

        for config_type, file_path in config_file_paths.items():
            if not os.path.exists(file_path):
                logging.warning(f"Initial config file not found: {file_path}. Skipping.")
                continue

            try:
                with open(file_path, 'r') as f:
                    configs = json.load(f)
                    
                if not isinstance(configs, list):
                    configs = [configs] # Ensure it's always a list for iteration

                for config_item in configs:
                    target_index = ""
                    doc_id = None
                    if config_type == 'channel':
                        target_index = "channel_config"
                        doc_id = f"channel_{config_item.get('channel_id')}"
                    elif config_type == 'global':
                        target_index = "global_config"
                        doc_id = config_item.get('id')
                    elif config_type == 'windows':
                        target_index = "windows_config"
                        doc_id = config_item.get('name')
                    elif config_type == 'ird':
                        target_index = "ird_config"
                        doc_id = config_item.get('system_id') or config_item.get('ip_address')
                    else:
                        logging.warning(f"Unknown config type: {config_type}. Skipping item.")
                        continue
                    
                    if doc_id:
                        self.store_config(doc_id, config_item, index=target_index)
                    else:
                        logging.warning(f"Could not determine document ID for config item in {file_path}: {config_item}. Skipping.")

            except json.JSONDecodeError as e:
                logging.error(f"Error decoding JSON from {file_path}: {e}")
            except Exception as e:
                logging.error(f"Error loading initial config from {file_path}: {e}")

    def index_alarm(self, alarm_data):
        """
        Indexes an alarm into the historical_alarms index.
        This method is now primarily for historical logging. For active alarms, use update_active_alarms.
        """
        if not self.es:
            logging.error(f"Elasticsearch client not available. Cannot index alarm to historical.")
            return False
        try:
            # Ensure timestamp exists for historical indexing
            if "timestamp" not in alarm_data: # Changed from @timestamp to timestamp
                alarm_data["timestamp"] = datetime.now().isoformat()

            response = self.es.index(index=self.historical_alarms_index, document=alarm_data)
            logging.debug(f"Alarm indexed successfully to historical: {response['result']} with ID: {response['_id']}")
            return True
        except Exception as e:
            logging.error(f"Error indexing alarm to historical: {e}")
            logging.debug(f"Alarm data: {alarm_data}")
            return False

    def update_active_alarms(self, source_type, current_active_alarms):
        """
        Replaces all active alarms for a specific source_type in the active_alarms index
        and also logs them to the historical_alarms index.
        `source_type` could be 'windows_monitor', 'ilo_device', 'ird_channel', 'ird_overview', 'snmp_kmx', etc.
        `current_active_alarms` is a list of alarm dictionaries.
        """
        if not self.es:
            logging.error("Elasticsearch client not available. Cannot update active alarms.")
            return False

        try:
            # 1. Clear existing active alarms for this source type
            # Note: A dedicated 'active_alarms' index usually holds only the *current* state.
            # If an alarm clears, it should be removed from this index.
            # Your consumer should handle updating/clearing active alarms.
            # This method should primarily be for initial population or full sync.
            # For real-time updates, consumers directly push to ES active_alarms.
            # The original structure of `delete_by_query` is problematic for a single active_alarms index
            # that multiple sources write to. Better to handle this in the consumer with specific IDs.
            # For now, commenting out aggressive delete_by_query here, as consumers manage active_alarms.
            # query = {"match": {"type.keyword": source_type}} # Use .keyword for exact match
            # self.delete_by_query(self.active_alarms_index, query)

            # 2. Add current active alarms for this source type to active_alarms and historical_alarms
            for alarm_data in current_active_alarms:
                # Ensure timestamp exists
                if "timestamp" not in alarm_data:
                    alarm_data["timestamp"] = datetime.now().isoformat()
                
                # Assign the source type if not already present
                if "type" not in alarm_data:
                    alarm_data["type"] = source_type # 'type' field stores agent_type

                # Generate a stable ID for active alarms to allow easy replacement/update
                # This ID should be unique per alarm (combination of fields)
                # Ensure fields exist before trying to use them in ID generation
                stable_alarm_id_parts = [
                    alarm_data.get('type', 'generic_type'),
                    alarm_data.get('device_ip', 'unknown_ip'),
                    alarm_data.get('block_id', 'unknown_block'), # Changed to block_id
                    # Use a hash of the message to keep the ID from becoming too long
                    hashlib.sha256(alarm_data.get('message', '').encode()).hexdigest()[:16] # Truncate hash
                ]
                stable_alarm_id = "_".join(str(p) for p in stable_alarm_id_parts).replace(" ", "_").replace(".", "-").replace(":", "-").lower()
                
                # Index into active alarms (with stable ID for replacement/upsert)
                # This needs to be an 'upsert' or direct index if consumer manages it
                self.es.index(index=self.active_alarms_index, id=stable_alarm_id, document=alarm_data)
                
                # Also log to historical alarms (let ES auto-generate ID for historical)
                self.es.index(index=self.historical_alarms_index, document=alarm_data)

            logging.info(f"Updated active alarms for source_type '{source_type}'. Indexed {len(current_active_alarms)} alarms.")
            return True
        except Exception as e:
            logging.error(f"Error updating active alarms for source_type '{source_type}': {e}")
            return False


    def fetch_all_alarms(self, size=1000):
        """
        Fetches all documents from the 'active_alarms' Elasticsearch index.
        This should correspond to the *current* state of active alarms.
        """
        if not self.es:
            logging.error("Elasticsearch client not available. Cannot fetch active alarms.")
            return []
        try:
            query_body = {
                "query": {
                    "match_all": {}
                },
                "sort": [
                    {"timestamp": {"order": "desc"}} # Sort by 'timestamp' field
                ],
                "size": size
            }
            response = self.es.search(index=self.active_alarms_index, body=query_body)
            alarms = [hit['_source'] for hit in response['hits']['hits']]
            return alarms
        except Exception as e:
            logging.error(f"Error fetching all alarms from {self.active_alarms_index}: {e}", exc_info=True)
            return []

    def fetch_historical_alarms(self, filters, size=1000):
        """
        Fetches alarm history from 'monitor_historical_alarms' based on filters.
        Filters can include: start_time, end_time, device_name, channel_name, group_name, agent_type.
        """
        if not self.es:
            logging.error("Elasticsearch client not available. Cannot fetch historical alarms.")
            return []

        query_body = {
            "query": {
                "bool": {
                    "must": []
                }
            },
            "sort": [{"timestamp": {"order": "desc"}}],
            "size": size  # ✅ Size is now only passed inside body
        }

        # Time range filter (using 'timestamp' field)
        if 'start_time' in filters or 'end_time' in filters:
            range_query = {"range": {"timestamp": {}}}
            if 'start_time' in filters:
                range_query["range"]["timestamp"]["gte"] = filters['start_time']
            if 'end_time' in filters:
                range_query["range"]["timestamp"]["lte"] = filters['end_time']
            query_body["query"]["bool"]["must"].append(range_query)

        # Keyword filters (term queries for exact matches on .keyword fields)
        if 'device_name' in filters:
            query_body["query"]["bool"]["must"].append({"term": {"device_name.keyword": filters['device_name']}})
        if 'channel_name' in filters:
            query_body["query"]["bool"]["must"].append({"term": {"channel_name.keyword": filters['channel_name']}})
        if 'group_name' in filters:
            query_body["query"]["bool"]["must"].append({"term": {"group_name.keyword": filters['group_name']}})
        if 'agent_type' in filters:
            query_body["query"]["bool"]["must"].append({"term": {"type.keyword": filters['agent_type']}})

        logging.info(f"Executing ES historical alarm query: {json.dumps(query_body)}")

        try:
            response = self.es.search(index=self.historical_alarms_index, body=query_body)  # ✅ FIXED: Removed size=
            alarms = [hit['_source'] for hit in response['hits']['hits']]
            return alarms
        except Exception as e:
            logging.error(f"Error fetching historical alarms from ES with filters {filters}: {e}", exc_info=True)
            return []

    def clear_index(self, index_name):
        """Deletes all documents from a specified index."""
        if not self.es:
            logging.error(f"Elasticsearch client not available. Cannot clear index '{index_name}'.")
            return False
        try:
            # Removed the delete_by_query call to prevent accidental data deletion.
            logging.info(f"Clear index operation for '{index_name}' is disabled.")
            return False # Indicate that the operation was not performed
        except Exception as e:
            logging.error(f"Error clearing index '{index_name}': {e}")
            return False

    def delete_by_query(self, index_name, query_body):
        """Deletes documents matching a specific query from an index."""
        if not self.es:
            logging.error(f"Elasticsearch client not available. Cannot delete by query from '{index_name}'.")
            return False
        try:
            response = self.es.delete_by_query(index=index_name, body={"query": query_body})
            logging.info(f"Deleted {response['deleted']} documents from '{index_name}' matching query.")
            return True
        except Exception as e:
            logging.error(f"Error deleting by query from '{index_name}': {e}")
            return False