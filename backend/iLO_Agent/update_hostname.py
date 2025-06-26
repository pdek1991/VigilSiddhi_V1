import os
import logging
import requests
import urllib3
import mysql.connector
from mysql.connector import Error
from concurrent.futures import ThreadPoolExecutor, as_completed

# Disable SSL warnings
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Logging setup
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- Configuration ---
MYSQL_HOST = os.environ.get('MYSQL_HOST', '192.168.56.30')
MYSQL_DATABASE = os.environ.get('MYSQL_DATABASE', 'vigil_siddhi')
MYSQL_USER = os.environ.get('MYSQL_USER', 'vigilsiddhi')
MYSQL_PASSWORD = os.environ.get('MYSQL_PASSWORD', 'vigilsiddhi')
MAX_THREADS = 10

def get_ilo_hostname(ip, username, password):
    """Fetch HostName from iLO Redfish API"""
    try:
        url = f"https://{ip}/redfish/v1/Systems/1"
        r = requests.get(url, auth=(username, password), verify=False, timeout=10)
        if r.status_code == 200:
            return r.json().get("HostName", None)
        else:
            logging.warning(f"[{ip}] Redfish HTTP error {r.status_code}")
    except Exception as e:
        logging.error(f"[{ip}] Redfish Exception: {e}")
    return None

def update_device_name_thread(ip_id, ip, username, password, old_device_name):
    """Single-threaded function to fetch and update one device"""
    hostname = get_ilo_hostname(ip, username, password)
    if hostname:
        try:
            conn = mysql.connector.connect(
                host=MYSQL_HOST,
                database=MYSQL_DATABASE,
                user=MYSQL_USER,
                password=MYSQL_PASSWORD
            )
            cursor = conn.cursor()
            update_sql = """
                UPDATE device_ips
                SET device_name = %s,
                    Group_name = %s,
                    last_updated = NOW()
                WHERE ip_id = %s
            """
            cursor.execute(update_sql, (hostname, old_device_name, ip_id))
            conn.commit()
            logging.info(f"[UPDATED] {ip} → HostName: {hostname}, Group: {old_device_name}")
        except Error as e:
            logging.error(f"[{ip}] MySQL Error: {e}")
        finally:
            if conn.is_connected():
                cursor.close()
                conn.close()
    else:
        logging.warning(f"[SKIPPED] {ip} → HostName not fetched")

def update_device_names_parallel():
    try:
        conn = mysql.connector.connect(
            host=MYSQL_HOST,
            database=MYSQL_DATABASE,
            user=MYSQL_USER,
            password=MYSQL_PASSWORD
        )
        if conn.is_connected():
            cursor = conn.cursor(dictionary=True)
            logging.info("Connected to MySQL")

            # Check if Group_name exists
            cursor.execute("""
                SELECT COUNT(*) AS count FROM information_schema.columns
                WHERE table_schema=%s AND table_name='device_ips' AND column_name='Group_name'
            """, (MYSQL_DATABASE,))
            if cursor.fetchone()['count'] == 0:
                cursor.execute("ALTER TABLE device_ips ADD COLUMN Group_name VARCHAR(255) DEFAULT NULL")
                logging.info("Added 'Group_name' column to device_ips")

            # Fetch data
            cursor.execute("SELECT ip_id, ip_address, username, password, device_name FROM device_ips")
            rows = cursor.fetchall()

        conn.close()

        # Launch threads
        with ThreadPoolExecutor(max_workers=MAX_THREADS) as executor:
            futures = []
            for row in rows:
                futures.append(
                    executor.submit(
                        update_device_name_thread,
                        row['ip_id'],
                        row['ip_address'],
                        row['username'],
                        row['password'],
                        row['device_name']
                    )
                )
            # Wait for all to complete
            for f in as_completed(futures):
                pass

    except Error as e:
        logging.error(f"MySQL Error: {e}")
    finally:
        if conn.is_connected():
            conn.close()
            logging.info("MySQL connection closed.")

if __name__ == "__main__":
    update_device_names_parallel()
