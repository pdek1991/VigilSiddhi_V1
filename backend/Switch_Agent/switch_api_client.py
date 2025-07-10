import logging
import asyncio
import os
import redis
import json
import aiohttp # For making HTTP requests to Prometheus

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

class SwitchAPIClient:
    """
    A client for interacting with network switches by fetching metrics from Prometheus.
    """
    
    PROMETHEUS_URL = os.environ.get('PROMETHEUS_URL', 'http://192.168.56.30:9090')

    # Redis configuration for caching (optional, but good for performance)
    REDIS_HOST = os.environ.get('REDIS_HOST', '192.168.56.30')
    REDIS_PORT = int(os.environ.get('REDIS_PORT', 6379))
    CACHE_TTL_SECONDS = 30 # Cache results for 30 seconds, aligning with agent poll interval

    def __init__(self):
        self.http_session = None # aiohttp client session for Prometheus queries
        logging.info(f"SwitchAPIClient initialized. Prometheus URL: {self.PROMETHEUS_URL}")

        try:
            self.redis_client = redis.Redis(host=self.REDIS_HOST, port=self.REDIS_PORT, db=0, decode_responses=True)
            self.redis_client.ping()
            logging.info(f"SwitchAPIClient successfully connected to Redis at {self.REDIS_HOST}:{self.REDIS_PORT}.")
        except redis.exceptions.ConnectionError as e:
            logging.error(f"SwitchAPIClient: Failed to connect to Redis for caching at {self.REDIS_HOST}:{self.REDIS_PORT}. Error: {e}")
            self.redis_client = None
        except Exception as e:
            logging.error(f"SwitchAPIClient: Unexpected error during Redis connection test: {e}", exc_info=True)
            self.redis_client = None

    async def _get_http_session(self):
        """Ensures a single aiohttp client session is used."""
        if self.http_session is None or self.http_session.closed:
            self.http_session = aiohttp.ClientSession()
        return self.http_session

    async def _close_http_session(self):
        """Closes the aiohttp client session."""
        if self.http_session and not self.http_session.closed:
            await self.http_session.close()
            self.http_session = None

    async def _query_prometheus(self, query):
        """
        Helper method to perform an asynchronous query against the Prometheus API.
        """
        encoded_query = query # No need to encode here, aiohttp handles it for params
        url = f"{self.PROMETHEUS_URL}/api/v1/query"
        session = await self._get_http_session()
        
        try:
            async with session.get(url, params={'query': encoded_query}, timeout=10) as response:
                if response.status != 200:
                    error_text = await response.text()
                    logging.error(f"Prometheus HTTP error for query '{query}': {response.status} - {error_text}")
                    return None
                data = await response.json()
                if data and data['status'] == 'success':
                    return data['data']['result']
                else:
                    logging.warning(f"Prometheus query '{query}' returned non-success status or no data: {data}")
                    return None
        except aiohttp.ClientError as e:
            logging.error(f"Network/Client error querying Prometheus for '{query}': {e}", exc_info=True)
            return None
        except asyncio.TimeoutError:
            logging.error(f"Prometheus query '{query}' timed out after 10 seconds.")
            return None
        except Exception as e:
            logging.error(f"Unexpected error during Prometheus query '{query}': {e}", exc_info=True)
            return None

    async def get_switch_metrics(self, hostname, switch_ip):
        """
        Fetches CPU, Memory utilization, and Uptime from Prometheus for a given switch.
        Uses hostname for metrics that are labeled with it, and switch_ip (agent_host)
        for metrics that might only have the agent_host label.
        """
        cache_key = f"switch_metrics:{hostname}"
        if self.redis_client:
            cached_data = self.redis_client.get(cache_key)
            if cached_data:
                logging.info(f"Returning cached switch metrics for {hostname}.")
                return json.loads(cached_data)

        metrics = {
            "cpu_utilization": "N/A",
            "memory_utilization": "N/A",
            "uptime": "N/A"
        }

        # Prometheus queries
        # Note: Assuming 'job="telegraf_snmp"' as per previous frontend code
        cpu_query = f'cisco_snmp_cpu_5min{{hostname="{hostname}", job="telegraf_snmp"}}'
        mem_used_query = f'cisco_snmp_mem_used{{hostname="{hostname}", job="telegraf_snmp"}}'
        mem_total_query = f'cisco_snmp_mem_total{{hostname="{hostname}", job="telegraf_snmp"}}' # Assuming a total memory metric
        uptime_query = f'cisco_snmp_uptime{{hostname="{hostname}", job="telegraf_snmp"}} / (60*60*24)' # Uptime in days

        tasks = [
            self._query_prometheus(cpu_query),
            self._query_prometheus(mem_used_query),
            self._query_prometheus(mem_total_query),
            self._query_prometheus(uptime_query)
        ]

        cpu_result, mem_used_result, mem_total_result, uptime_result = await asyncio.gather(*tasks, return_exceptions=True)

        # Process CPU
        if isinstance(cpu_result, list) and cpu_result:
            try:
                metrics["cpu_utilization"] = float(cpu_result[0]['value'][1])
            except (ValueError, KeyError, IndexError):
                logging.warning(f"Could not parse CPU for {hostname}: {cpu_result}")

        # Process Memory
        if (isinstance(mem_used_result, list) and mem_used_result and
            isinstance(mem_total_result, list) and mem_total_result):
            try:
                used = float(mem_used_result[0]['value'][1])
                total = float(mem_total_result[0]['value'][1])
                if total > 0:
                    metrics["memory_utilization"] = (used / total) * 100
            except (ValueError, KeyError, IndexError):
                logging.warning(f"Could not parse Memory for {hostname}: Used={mem_used_result}, Total={mem_total_result}")

        # Process Uptime
        if isinstance(uptime_result, list) and uptime_result:
            try:
                metrics["uptime"] = float(uptime_result[0]['value'][1])
            except (ValueError, KeyError, IndexError):
                logging.warning(f"Could not parse Uptime for {hostname}: {uptime_result}")

        if self.redis_client:
            self.redis_client.setex(cache_key, self.CACHE_TTL_SECONDS, json.dumps(metrics))
            logging.info(f"Cached switch metrics for {hostname}.")

        return metrics

    async def get_interface_details(self, switch_ip):
        """
        Fetches detailed interface information for a specific switch from Prometheus.
        This will involve querying multiple metrics and consolidating them by ifIndex.
        """
        cache_key = f"switch_interfaces:{switch_ip}"
        if self.redis_client:
            cached_data = self.redis_client.get(cache_key)
            if cached_data:
                logging.info(f"Returning cached interface details for {switch_ip}.")
                return json.loads(cached_data)

        interfaces = {} # Key: ifIndex, Value: interface_data_dict

        # List of Prometheus metrics to fetch for interfaces, using agent_host (switch_ip)
        # Note: These metric names are based on the original frontend's Prometheus queries.
        metrics_to_query = {
            "ifAdminStatus": f'interfaces_ifAdminStatus{{agent_host="{switch_ip}", job="telegraf_snmp"}}',
            "ifInDiscards": f'interfaces_ifInDiscards{{agent_host="{switch_ip}", job="telegraf_snmp"}}',
            "ifInErrors": f'interfaces_ifInErrors{{agent_host="{switch_ip}", job="telegraf_snmp"}}',
            "ifInOctets": f'interfaces_ifInOctets{{agent_host="{switch_ip}", job="telegraf_snmp"}}',
            "ifInUcastPkts": f'interfaces_ifInUcastPkts{{agent_host="{switch_ip}", job="telegraf_snmp"}}',
            "ifInUnknownProtos": f'interfaces_ifInUnknownProtos{{agent_host="{switch_ip}", job="telegraf_snmp"}}',
            "ifLastChange": f'interfaces_ifLastChange{{agent_host="{switch_ip}", job="telegraf_snmp"}}',
            "ifMtu": f'interfaces_ifMtu{{agent_host="{switch_ip}", job="telegraf_snmp"}}',
            "ifOperStatus": f'interfaces_ifOperStatus{{agent_host="{switch_ip}", job="telegraf_snmp"}}',
            "ifOutDiscards": f'interfaces_ifOutDiscards{{agent_host="{switch_ip}", job="telegraf_snmp"}}',
            "ifOutErrors": f'interfaces_ifOutErrors{{agent_host="{switch_ip}", job="telegraf_snmp"}}',
            "ifOutOctets": f'interfaces_ifOutOctets{{agent_host="{switch_ip}", job="telegraf_snmp"}}',
            "ifOutUcastPkts": f'interfaces_ifOutUcastPkts{{agent_host="{switch_ip}", job="telegraf_snmp"}}',
            "ifSpeed": f'interfaces_ifSpeed{{agent_host="{switch_ip}", job="telegraf_snmp"}}',
            "ifType": f'interfaces_ifType{{agent_host="{switch_ip}", job="telegraf_snmp"}}',
            "ifDescr": f'interfaces_ifDescr{{agent_host="{switch_ip}", job="telegraf_snmp"}}',
            "ifAlias": f'interfaces_ifAlias{{agent_host="{switch_ip}", job="telegraf_snmp"}}'
        }

        tasks = [self._query_prometheus(query) for query in metrics_to_query.values()]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        metric_names = list(metrics_to_query.keys())
        for i, result in enumerate(results):
            metric_name = metric_names[i]
            if isinstance(result, list):
                for item in result:
                    try:
                        ifIndex = int(item['metric']['ifIndex'])
                        if ifIndex not in interfaces:
                            interfaces[ifIndex] = {"ifIndex": ifIndex, "name": f"Interface {ifIndex}"}

                        value = item['value'][1]
                        if metric_name in ["ifAdminStatus", "ifOperStatus", "ifType", "ifMtu", "ifSpeed",
                                           "ifInOctets", "ifInUcastPkts", "ifInDiscards", "ifInErrors", "ifInUnknownProtos",
                                           "ifOutOctets", "ifOutUcastPkts", "ifOutDiscards", "ifOutErrors"]:
                            interfaces[ifIndex][metric_name] = float(value)
                        elif metric_name == "ifLastChange":
                            # Prometheus timestamp is seconds since epoch, convert to ISO 8601
                            interfaces[ifIndex][metric_name] = datetime.fromtimestamp(float(value)).isoformat() + "Z"
                        else: # ifDescr, ifAlias
                            interfaces[ifIndex][metric_name] = value
                    except (ValueError, KeyError, IndexError) as e:
                        logging.warning(f"Error parsing Prometheus data for {metric_name} on ifIndex {item.get('metric', {}).get('ifIndex', 'N/A')}: {e}. Item: {item}")
            elif isinstance(result, Exception):
                logging.error(f"Error fetching {metric_name} from Prometheus for {switch_ip}: {result}")

        interfaces_list = list(interfaces.values())
        
        if self.redis_client:
            self.redis_client.setex(cache_key, self.CACHE_TTL_SECONDS, json.dumps(interfaces_list))
            logging.info(f"Cached interface details for {switch_ip}.")

        return interfaces_list

# Global instance of the client
switch_api_client = SwitchAPIClient()
