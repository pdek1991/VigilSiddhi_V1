import requests
import xml.etree.ElementTree as ET
import logging
from datetime import datetime
import re
import urllib3

# Suppress insecure request warning for self-signed certificates (common with iLO/IRD)
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# Default IRD API settings
HEADERS = {'Content-Type': 'text/xml; charset=UTF-8'}
VERIFY_SSL = False # Set to True in production with proper SSL certificates

class IRDAPIClient:
    """
    A client for interacting with Cisco D9800 IRD devices.
    Encapsulates login and various status data fetching methods.
    """
    def __init__(self):
        # Session IDs cache: {ip_address: session_id}
        self.session_ids = {}

    def _send_request(self, ip, url_path, method='GET', data=None, username=None, password=None, session_id=None):
        """Helper to send HTTP requests to the IRD, handling SSL verification and session IDs."""
        url = f"https://{ip}{url_path}"
        headers = HEADERS.copy()
        if session_id:
            headers["X-SESSION-ID"] = session_id
        
        auth = (username, password) if username and password else None

        try:
            if method == 'POST':
                response = requests.post(url, data=data, headers=headers, verify=VERIFY_SSL, auth=auth, timeout=15)
            else: # GET
                response = requests.get(url, headers=headers, verify=VERIFY_SSL, auth=auth, timeout=15)
            response.raise_for_status() # Raise HTTPError for bad responses (4xx or 5xx)
            return response.text
        except requests.exceptions.HTTPError as e:
            logging.error(f"HTTP Error for {ip}{url_path}: {e.response.status_code} - {e.response.text}")
            return None
        except requests.exceptions.ConnectionError as e:
            logging.error(f"Connection Error for {ip}{url_path}: {e}")
            return None
        except requests.exceptions.Timeout:
            logging.error(f"Request to {ip}{url_path} timed out.")
            return None
        except Exception as e:
            logging.error(f"An unexpected error occurred for {ip}{url_path}: {e}")
            return None

    def login(self, ip, username, password):
        """
        Logs into the IRD device and retrieves a session ID.
        Caches the session ID for subsequent requests.
        """
        if ip in self.session_ids:
            return self.session_ids[ip] # Return cached ID if available

        url_path = "/ws/v1/table?t=return"
        payload = f"""<HDR><LOGIN><UID>{username}</UID><USERPASS>{password}</USERPASS></LOGIN></HDR>"""
        
        response_text = self._send_request(ip, url_path, method='POST', data=payload.strip())
        if response_text:
            try:
                root = ET.fromstring(response_text)
                session_id_elem = root.find('.//SESSION_ID')
                if session_id_elem is not None and session_id_elem.text:
                    session_id = session_id_elem.text
                    self.session_ids[ip] = session_id
                    logging.info(f"Successfully logged into IRD {ip}. Session ID: {session_id[:8]}...")
                    return session_id
            except ET.ParseError as e:
                logging.error(f"XML Parse Error during login for {ip}: {e}")
            except Exception as e:
                logging.error(f"Error parsing login response for {ip}: {e}")
        logging.warning(f"Failed to log into IRD {ip}. Invalid response or credentials.")
        return None

    def _get_ird_data(self, ip, url_path, session_id):
        """Helper to fetch XML data from a specific IRD endpoint."""
        response_text = self._send_request(ip, url_path, session_id=session_id)
        if response_text:
            try:
                return ET.fromstring(response_text)
            except ET.ParseError as e:
                logging.error(f"XML Parse Error for {ip}{url_path}: {e}")
            except Exception as e:
                logging.error(f"Error parsing XML response for {ip}{url_path}: {e}")
        return None

    def get_rf_status(self, ip, session_id):
        """
        Fetches RF status data for all active RF inputs.
        Parses detailed fields including lock status, modulation, input rate, etc.
        """
        root = self._get_ird_data(ip, "/ws/v2/status/input/rf", session_id)
        if root is None: return None # Still return None if _get_ird_data failed

        rf_inputs = []
        # Iterate through each <rf> element based on the provided XML
        for rf_elem in root.findall('.//rf'):
            try:
                # Check if the input is active
                active_status = rf_elem.findtext('.//act', 'No')
                if active_status == 'Yes':
                    input_data = {}
                    # Use card, port, and stream to form a unique ID for each RF input
                    card = rf_elem.findtext('.//card', 'N/A')
                    port = rf_elem.findtext('.//port', 'N/A')
                    stream = rf_elem.findtext('.//stream', 'N/A')
                    input_data['id'] = f"Card{card}_Port{port}_Stream{stream}"
                    
                    # Parse numerical values, stripping whitespace
                    input_data['input_level_dbm'] = float(rf_elem.findtext('.//siglevel', '0.0').strip())
                    input_data['cn_margin'] = float(rf_elem.findtext('.//cnmargin', '0.0').strip())
                    
                    satlock_status = rf_elem.findtext('.//satlock', 'UNKNOWN')
                    if satlock_status == 'Lock+Sig':
                        input_data['lock_status'] = 'Lock+Sig'
                    else:
                        # If not 'Lock+Sig', use the raw value. This will be flagged as critical alarm by agent.
                        input_data['lock_status'] = satlock_status
                        
                    input_data['modulation'] = rf_elem.findtext('.//mod', 'UNKNOWN')
                    input_data['input_bw'] = float(rf_elem.findtext('.//inputrate', '0.0').strip()) # Input Bandwidth
                    input_data['system_id'] = rf_elem.findtext('.//transid', 'N/A') # Transponder ID mapped to System ID
                    input_data['polarity'] = rf_elem.findtext('.//pol', 'N/A') # Polarization (H/V)
                    input_data['frequency_mhz'] = float(rf_elem.findtext('.//dnlkfreq', '0.0').strip()) # Downlink Frequency
                    input_data['symbol_rate'] = float(rf_elem.findtext('.//symrate', '0.0').strip()) # Symbol Rate

                    rf_inputs.append(input_data)
            except Exception as e:
                logging.warning(f"Error parsing RF input entry for {ip}: {e}")
        return rf_inputs # Return empty list if no active inputs, not None

    def get_asi_status(self, ip, session_id):
        """Fetches ASI input status."""
        root = self._get_ird_data(ip, "/ws/v2/status/input/asi", session_id)
        if root is None: return None # Still return None if _get_ird_data failed
        
        asi_status_list = []
        # Iterate through each <asi> element based on provided XML
        for asi_input_elem in root.findall('.//asi'):
            try:
                active_status = asi_input_elem.findtext('.//act', 'No')
                # Only process active ASI inputs
                if active_status == 'Yes':
                    card = asi_input_elem.findtext('.//card', 'N/A')
                    port = asi_input_elem.findtext('.//port', 'N/A')
                    stream = asi_input_elem.findtext('.//stream', 'N/A')
                    lock_status = asi_input_elem.findtext('.//asilock', 'UNKNOWN')
                    
                    # Extract inputrate as input_bw
                    input_rate_text = asi_input_elem.findtext('.//inputrate', '0.0')
                    input_bw = float(input_rate_text.strip()) if input_rate_text else 0.0

                    # Extract transid as system_id
                    system_id = asi_input_elem.findtext('.//transid', 'N/A')

                    asi_entry = {
                        "id": f"Card{card}_Port{port}_Stream{stream}", # Composite ID
                        "lock_status": lock_status,
                        "message": "ASI Locked" if lock_status == "Lock" else "ASI Not Locked",
                        "input_bw": input_bw,  # Added input bandwidth
                        "system_id": system_id  # Added system ID from transid
                    }
                    asi_status_list.append(asi_entry)
            except Exception as e:
                logging.warning(f"Error parsing ASI input in {ip}: {e}")
        return asi_status_list # Return empty list if no active inputs, not None

    def get_moip_output_status(self, ip, session_id):
        """Fetches MOIP output status and determines if locked based on ts1rate."""
        root = self._get_ird_data(ip, "/ws/v2/status/output/moip", session_id)
        if root is None: return None
        
        moip_output_status = {}
        try:
            # Based on the provided XML, ts1rate is directly under <moip>
            ts1rate_text = root.findtext('.//moip/ts1rate', '0.0')
            ts1rate = float(ts1rate_text)
            moip_output_status['ts1rate'] = ts1rate
            moip_output_status['locked'] = True if ts1rate != 0.0 else False
        except Exception as e:
            logging.error(f"Error parsing MOIP output status for {ip}: {e}")
            moip_output_status['locked'] = False # Default to not locked on error
        
        return moip_output_status

    def get_moip_output_config(self, ip, session_id):
        """Fetches MOIP output configuration details (multicast IP and port)."""
        root = self._get_ird_data(ip, "/ws/v2/service_cfg/output/moip", session_id)
        if root is None: return None
        
        moip_config = {}
        try:
            # mcastip and destport are direct children of <moip> based on provided XML
            moip_config['multicast_ip'] = root.findtext('.//moip/mcastip', 'N/A')
            moip_config['dest_port'] = root.findtext('.//moip/destport', 'N/A')
        except Exception as e:
            logging.error(f"Error parsing MOIP output config for {ip}: {e}")
        return moip_config

    def get_moip_input_status(self, ip, session_id):
        """Fetches MOIP input status."""
        root = self._get_ird_data(ip, "/ws/v2/status/input/moip", session_id)
        if root is None: return None # Still return None if _get_ird_data failed
        
        moip_input_list = []
        # Iterate through each moip input element based on provided XML
        for moip_in_elem in root.findall('.//moip'):
            try:
                lock_status = moip_in_elem.findtext('.//moiplock', 'UNKNOWN')
                dest_ip = moip_in_elem.findtext('.//destip', 'N/A')
                input_id = moip_in_elem.findtext('.//inputid', 'N/A') # Assuming inputid exists for identification
                
                moip_input_entry = {
                    "id": input_id,
                    "dest_ip": dest_ip,
                    "lock_status": lock_status,
                    "is_locked": True if lock_status == "Lock+Sig" else False
                }
                moip_input_list.append(moip_input_entry)
            except Exception as e:
                logging.warning(f"Error parsing MOIP input entry for {ip}: {e}")
        return moip_input_list # Return empty list if no inputs, not None


    def get_channel_status(self, ip, session_id):
        """
        Fetches PE (Program Element) status for active programs.
        Parses program number, channel name, service ID, and infers video/audio presence.
        """
        root = self._get_ird_data(ip, f"/ws/v2/status/pe", session_id)
        if root is None: return None # Still return None if _get_ird_data failed

        channel_statuses = []
        # Iterate through each record element based on provided XML
        for prgm_elem in root.findall('.//record'):
            try:
                prgm_status = prgm_elem.findtext('.//prgmstatus', 'Inactive')
                if prgm_status == 'Active':
                    channel_data = {}
                    channel_data['program_number'] = prgm_elem.findtext('.//chn', 'N/A')
                    channel_data['channel_name'] = prgm_elem.findtext('.//chname', 'UNKNOWN')
                    channel_data['service_id'] = prgm_elem.findtext('.//pmtpid', 'N/A')
                    
                    # Infer video_present and audio_present based on program status as per request
                    channel_data['video_present'] = True
                    channel_data['audio_present'] = True
                    
                    channel_statuses.append(channel_data)
            except Exception as e:
                logging.warning(f"Error parsing active channel status for {ip}: {e}")
        return channel_statuses # Return empty list if no active channels, not None

    def get_ethernet_status(self, ip, session_id):
        """Fetches Ethernet interface status, focusing on Data1/Data2 and IP range."""
        # Corrected endpoint as per user's curl output
        root = self._get_ird_data(ip, "/ws/v2/status/device/eth", session_id)
        if root is None: return None # Still return None if _get_ird_data failed
        
        eth_interfaces = []
        # Iterate through each eth element based on provided XML
        for eth_int_elem in root.findall('.//eth'):
            try:
                name = eth_int_elem.findtext('.//name', 'UNKNOWN')
                link_status = eth_int_elem.findtext('.//link', 'UNKNOWN') # "Link Up" / "Link Down"
                ipv4_addr = eth_int_elem.findtext('.//ipv4addr', 'N/A') # IPv4 Address
                
                eth_entry = {
                    "name": name,
                    "link_status": link_status,
                    "ipv4_addr": ipv4_addr,
                    "alarm": False, # Default to no alarm
                    "alarm_message": ""
                }

                # Check Data1/Data2 and IP range with link status
                if name in ["Data1", "Data2"]:
                    # Check if IP is in 10.10.X.X range
                    if re.match(r'^10\.10\.\d{1,3}\.\d{1,3}$', ipv4_addr):
                        if link_status != 'Link Up':
                            eth_entry['alarm'] = True
                            eth_entry['alarm_message'] = f"Critical Alarm: {name} IP ({ipv4_addr}) in 10.10.X.X range but Link is '{link_status}'."
                
                eth_interfaces.append(eth_entry)
            except Exception as e:
                logging.warning(f"Error parsing Ethernet interface in {ip}: {e}")
        return eth_interfaces # Return empty list if no interfaces, not None

    def get_power_supply_status(self, ip, session_id):
        """Fetches Power Supply status, raising alarm if not 'Ok'."""
        root = self._get_ird_data(ip, "/ws/v2/status/device/power", session_id)
        if root is None: return None # Still return None if _get_ird_data failed
        
        power_status_list = []
        # Iterate through each power element based on provided XML
        for ps_elem in root.findall('.//power'):
            try:
                name = ps_elem.findtext('.//displayBoardName', 'UNKNOWN_PS') # Using displayBoardName
                status = ps_elem.findtext('.//status', 'UNKNOWN') # "Ok" / "Fault" etc.
                
                ps_entry = {
                    "name": name,
                    "status": status,
                    "alarm": False,
                    "alarm_message": ""
                }

                if status != 'Ok':
                    ps_entry['alarm'] = True
                    ps_entry['alarm_message'] = f"Alarm: Power Supply '{name}' Status is '{status}'."
                
                power_status_list.append(ps_entry)
            except Exception as e:
                logging.warning(f"Error parsing Power Supply in {ip}: {e}")
        return power_status_list # Return empty list if no power supplies, not None

    def get_fault_status(self, ip, session_id):
        """Fetches active fault/alarm messages with severity, details, and timestamp."""
        # Corrected endpoint as per request
        root = self._get_ird_data(ip, "/ws/v2/status/faults/status", session_id)
        if root is None: return None # Still return None if _get_ird_data failed
        
        faults = []
        # Iterate through each status element based on provided XML
        for fault_elem in root.findall('.//status'):
            try:
                # The user mentioned <type> for severity, <details> for message, <setsince> for timestamp
                alarm_type = fault_elem.findtext('.//type', 'N/A') # Severity
                details = fault_elem.findtext('.//details', '') # Alarm message details
                set_since = fault_elem.findtext('.//setsince', '') # Timestamp

                faults.append({
                    "type": alarm_type, # Severity (e.g., Warning, Critical)
                    "details": details, # Alarm message details
                    "set_since": set_since # Timestamp since error
                })
            except Exception as e:
                logging.warning(f"Error parsing fault entry for {ip}: {e}")
        return faults # Return empty list if no faults, not None

# Global instance of the client
ird_api_client = IRDAPIClient()
