import asyncio
import aiohttp
import logging
import base64
from urllib.parse import urljoin
from datetime import datetime

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

class ILOAPIClient:
    """
    A client for interacting with HPE iLO devices via the Redfish API.
    Fetches health status for various hardware components.
    """
    def __init__(self):
        # Using a shared aiohttp.ClientSession for better performance with multiple requests.
        # It's crucial to manage its lifecycle; it will be created and closed by the agent.
        self.session = None
        logging.info("ILOAPIClient initialized.")

    async def _get_session(self):
        """Ensures an aiohttp client session is available."""
        if self.session is None or self.session.closed:
            # Setting connector=aiohttp.TCPConnector(ssl=False) to disable SSL verification
            # as per the user's `curl -k` example. This is generally INSECURE for production.
            self.session = aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=False))
            logging.debug("New aiohttp client session created with SSL verification disabled.")
        return self.session

    async def close_session(self):
        """Closes the aiohttp client session."""
        if self.session and not self.session.closed:
            await self.session.close()
            self.session = None
            logging.info("aiohttp client session closed.")

    async def _make_request(self, ip_address, username, password, path):
        """
        Makes an authenticated GET request to the iLO Redfish API.
        :param ip_address: IP address of the iLO device.
        :param username: Username for iLO authentication.
        :param password: Password for iLO authentication.
        :param path: The Redfish API endpoint path (e.g., "/redfish/v1/Systems/1").
        :return: JSON response or None on error.
        """
        url = f"https://{ip_address}{path}"
        auth_str = f"{username}:{password}"
        encoded_auth = base64.b64encode(auth_str.encode()).decode('ascii')
        headers = {
            'Authorization': f'Basic {encoded_auth}',
            'Content-Type': 'application/json'
        }
        
        session = await self._get_session()
        try:
            logging.debug(f"Making request to {url}")
            async with session.get(url, headers=headers, timeout=15) as response:
                response.raise_for_status()  # Raise an exception for HTTP errors (4xx or 5xx)
                return await response.json()
        except aiohttp.client_exceptions.ClientConnectorError as e:
            logging.error(f"Connection error to {ip_address}: {e}")
            return {"error": "Connection Error", "details": str(e)}
        except aiohttp.client_exceptions.ContentTypeError:
            logging.error(f"Invalid Content-Type or empty response from {url}. Expected JSON.")
            return {"error": "Invalid Response", "details": "Not a valid JSON response."}
        except aiohttp.client_exceptions.ClientResponseError as e:
            logging.error(f"HTTP error for {url}: Status {e.status}, Message: {e.message}")
            return {"error": "HTTP Error", "status": e.status, "details": e.message}
        except asyncio.TimeoutError:
            logging.error(f"Request to {url} timed out.")
            return {"error": "Timeout", "details": "Request timed out."}
        except Exception as e:
            logging.error(f"An unexpected error occurred for {url}: {e}", exc_info=True)
            return {"error": "Unexpected Error", "details": str(e)}

    def _extract_health_status(self, data, path_segments, default_health="UNKNOWN"):
        """
        Extracts health status from a nested dictionary given a list of path segments.
        Example: path_segments = ["MemorySummary", "Status", "HealthRollup"]
        """
        current_data = data
        for segment in path_segments:
            if isinstance(current_data, dict) and segment in current_data:
                current_data = current_data[segment]
            else:
                return default_health  # Path not found
        return current_data if isinstance(current_data, str) else default_health

    async def get_chassis_health(self, ip_address, username, password):
        """Fetches health status from /redfish/v1/Chassis/1."""
        path = "/redfish/v1/Chassis/1"
        data = await self._make_request(ip_address, username, password, path)
        if data and not data.get("error"):
            chassis_health = {
                "SmartStorageBattery": self._extract_health_status(data, ["SmartStorageBattery", "Status", "Health"]),
                "Fans": self._extract_health_status(data, ["Thermal", "Fans", "Status", "Health"]),
                "Temperatures": self._extract_health_status(data, ["Thermal", "Temperatures", "Status", "Health"]),
                "PowerSupplies": self._extract_health_status(data, ["Power", "PowerSupplies", "Status", "Health"]),
                "overall_chassis_health": self._extract_health_status(data, ["Status", "Health"])
            }
            logging.debug(f"Chassis health for {ip_address}: {chassis_health}")
            return chassis_health
        return {"error": data.get("error", "Failed to retrieve chassis health"), "details": data.get("details", "No data")}

    async def get_system_health(self, ip_address, username, password):
        """Fetches health status from /redfish/v1/Systems/1."""
        path = "/redfish/v1/Systems/1"
        data = await self._make_request(ip_address, username, password, path)
        if data and not data.get("error"):
            system_health = {
                "HostName": data.get("HostName", "N/A"),
                "MemorySummary_HealthRollup": self._extract_health_status(data, ["MemorySummary", "Status", "HealthRollup"]),
                "ProcessorSummary_HealthRollup": self._extract_health_status(data, ["ProcessorSummary", "Status", "HealthRollup"]),
                "AggregateServerHealth": self._extract_health_status(data, ["AggregateHealthStatus", "AggregateServerHealth"]),
                "BiosOrHardwareHealth": self._extract_health_status(data, ["AggregateHealthStatus", "BiosOrHardwareHealth", "Status", "Health"]),
                "FanHealth": self._extract_health_status(data, ["AggregateHealthStatus", "Fans", "Status", "Health"]),
                "MemoryHealth": self._extract_health_status(data, ["AggregateHealthStatus", "Memory", "Status", "Health"]),
                "NetworkHealth": self._extract_health_status(data, ["AggregateHealthStatus", "Network", "Status", "Health"]),
                "PowerSuppliesHealth": self._extract_health_status(data, ["AggregateHealthStatus", "PowerSupplies", "Status", "Health"]),
                "ProcessorsHealth": self._extract_health_status(data, ["AggregateHealthStatus", "Processors", "Status", "Health"]),
                "SmartStorageBatteryHealth": self._extract_health_status(data, ["AggregateHealthStatus", "SmartStorageBattery", "Status", "Health"]),
                "StorageHealth": self._extract_health_status(data, ["AggregateHealthStatus", "Storage", "Status", "Health"]),
                "TemperaturesHealth": self._extract_health_status(data, ["AggregateHealthStatus", "Temperatures", "Status", "Health"]),
                "overall_system_health": self._extract_health_status(data, ["Status", "Health"])
            }
            logging.debug(f"System health for {ip_address}: {system_health}")
            return system_health
        return {"error": data.get("error", "Failed to retrieve system health"), "details": data.get("details", "No data")}

    async def drill_down_component_health(self, ip_address, username, password, component_url):
        """
        Drills down into a specific component's health URL to get more details.
        """
        # Ensure the URL is relative to the Redfish base, or absolute
        if not component_url.startswith("/redfish"):
            # Assume it's a relative path from the root of the Redfish service
            # e.g., "Systems/1/Memory/1" instead of "/redfish/v1/Systems/1/Memory/1"
            full_path = urljoin("/redfish/v1/", component_url)
            if not full_path.startswith("/redfish/v1"): # Ensure it's correctly joined
                 full_path = f"/redfish/v1/{component_url}" # Fallback
        else:
            full_path = component_url

        data = await self._make_request(ip_address, username, password, full_path)
        if data and not data.get("error"):
            # For drill-down, just return the full data for now,
            # allowing the agent to parse or log as needed.
            return data
        return {"error": data.get("error", f"Failed to drill down {component_url}"), "details": data.get("details", "No data")}

    async def get_ilo_health_data(self, ip_address, username, password):
        """
        Orchestrates fetching all relevant iLO health data.
        Returns a comprehensive dictionary of health statuses and alarms.
        """
        health_data = {
            "ip_address": ip_address,
            "overall_status": "OK", # Default to OK
            "overall_severity": "INFO", # Default to INFO
            "overall_message": f"iLO device {ip_address} health is OK.",
            "components": {},
            "api_errors": []
        }

        # Fetch Chassis Health
        chassis_result = await self.get_chassis_health(ip_address, username, password)
        if "error" in chassis_result:
            health_data["api_errors"].append(f"Chassis Health Error: {chassis_result['details']}")
        else:
            health_data["components"]["chassis"] = chassis_result

        # Fetch System Health
        system_result = await self.get_system_health(ip_address, username, password)
        if "error" in system_result:
            health_data["api_errors"].append(f"System Health Error: {system_result['details']}")
        else:
            health_data["components"]["system"] = system_result

        # Determine overall status
        highest_severity_rank = 0 # 0: INFO, 1: WARNING, 2: CRITICAL, 3: FATAL/ERROR
        severity_map = {
            "OK": 0, "Informational": 0, "Normal": 0,
            "Warning": 1, "Minor": 1,
            "Critical": 2, "Major": 2,
            "Fatal": 3, "Error": 3, "UNKNOWN": 0
        }

        def _update_overall_status(current_health_value, component_name, source="component"):
            nonlocal highest_severity_rank
            health_rank = severity_map.get(current_health_value, 0)
            if health_rank > highest_severity_rank:
                highest_severity_rank = health_rank
                health_data["overall_status"] = "ALARM" if health_rank > 0 else "OK"
                health_data["overall_severity"] = [k for k, v in severity_map.items() if v == health_rank][0].upper()
                health_data["overall_message"] = f"iLO {component_name} health is {current_health_value} (Source: {source})."
            elif health_rank == highest_severity_rank and health_rank > 0:
                 health_data["overall_message"] += f" Also, {component_name} health is {current_health_value} (Source: {source})."

        # 1. Check for API Errors first
        if health_data["api_errors"]:
            health_data["overall_status"] = "ERROR"
            health_data["overall_severity"] = "ERROR"
            health_data["overall_message"] = "API communication errors encountered: " + "; ".join(health_data["api_errors"])
            highest_severity_rank = 3 # Treat API errors as highest severity

        # 2. Process System Component Health (if no higher severity from API errors)
        if "system" in health_data["components"] and highest_severity_rank < 3:
            system_comp = health_data["components"]["system"]
            # Prioritize aggregate health, then drill down based on individual component health
            if system_comp.get("AggregateServerHealth") and system_comp["AggregateServerHealth"] != "OK":
                 _update_overall_status(system_comp["AggregateServerHealth"], "AggregateServerHealth", "System")
            
            # Check individual components if aggregate is not critical
            component_checks = {
                "MemorySummary": system_comp.get("MemorySummary_HealthRollup"),
                "ProcessorSummary": system_comp.get("ProcessorSummary_HealthRollup"),
                "BiosOrHardwareHealth": system_comp.get("BiosOrHardwareHealth"),
                "Fans": system_comp.get("FanHealth"),
                "Memory": system_comp.get("MemoryHealth"),
                "Network": system_comp.get("NetworkHealth"),
                "PowerSupplies": system_comp.get("PowerSuppliesHealth"),
                "Processors": system_comp.get("ProcessorsHealth"),
                "SmartStorageBattery": system_comp.get("SmartStorageBatteryHealth"),
                "Storage": system_comp.get("StorageHealth"),
                "Temperatures": system_comp.get("TemperaturesHealth"),
            }
            # List of Redfish paths for drilling down based on component type
            drill_down_paths = {
                "Memory": "/redfish/v1/Systems/1/Memory",
                "Processors": "/redfish/v1/Systems/1/Processors",
                "Network": "/redfish/v1/Systems/1/EthernetInterfaces", # Can also be NetworkInterfaces
                "Storage": "/redfish/v1/Systems/1/Storage",
                # Other paths are more complex to drill down generally or belong to Chassis
            }

            for comp_name, health_val in component_checks.items():
                if health_val and health_val != "OK" and health_val != "UNKNOWN" and severity_map.get(health_val, 0) > 0:
                    _update_overall_status(health_val, comp_name, "System Component")
                    # If health is not OK, consider drilling down (example for Memory/Processor)
                    if comp_name.replace("Health", "") in drill_down_paths:
                        drill_path = drill_down_paths.get(comp_name.replace("Health", ""))
                        logging.info(f"Drilling down into {comp_name} at {drill_path} for {ip_address}")
                        drill_data = await self.drill_down_component_health(ip_address, username, password, drill_path)
                        health_data["components"]["system"][f"{comp_name}_detailed"] = drill_data
                        if drill_data and not drill_data.get("error") and "Members" in drill_data:
                            for member_link in drill_data["Members"]:
                                member_path = member_link.get("@odata.id")
                                if member_path:
                                    member_details = await self.drill_down_component_health(ip_address, username, password, member_path)
                                    health_data["components"]["system"][f"{comp_name}_detailed_{member_path.split('/')[-1]}"] = member_details


        # 3. Process Chassis Component Health (if no higher severity from others)
        if "chassis" in health_data["components"] and highest_severity_rank < 3:
            chassis_comp = health_data["components"]["chassis"]
            if chassis_comp.get("overall_chassis_health") and chassis_comp["overall_chassis_health"] != "OK":
                _update_overall_status(chassis_comp["overall_chassis_health"], "Overall Chassis", "Chassis")
            
            component_checks = {
                "SmartStorageBattery": chassis_comp.get("SmartStorageBattery"),
                "Fans": chassis_comp.get("Fans"),
                "Temperatures": chassis_comp.get("Temperatures"),
                "PowerSupplies": chassis_comp.get("PowerSupplies")
            }
            for comp_name, health_val in component_checks.items():
                if health_val and health_val != "OK" and health_val != "UNKNOWN" and severity_map.get(health_val, 0) > 0:
                    _update_overall_status(health_val, comp_name, "Chassis Component")

        logging.info(f"Final health summary for {ip_address}: Status={health_data['overall_status']}, Severity={health_data['overall_severity']}")
        return health_data

# Global instance of the client
ilo_api_client = ILOAPIClient()
