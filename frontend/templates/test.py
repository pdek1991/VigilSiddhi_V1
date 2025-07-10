import asyncio
import websockets
import os
import smtplib
import ssl
from email.mime.text import MIMEText
import telegram
import json  # <--- ADD THIS IMPORT
from datetime import datetime # <--- ADD THIS IMPORT

# --- Configuration Variables ---
# ... (your existing configuration variables like WS_URL, TELEGRAM_BOT_TOKEN, etc.) ...

WS_URL = "ws://127.0.0.1:8000" # Default for local testing
TELEGRAM_BOT_TOKEN = "7956839377:AAFL0G31dKyDM2M8YpVH-2nwlFc3HSxwVhw"
TELEGRAM_CHAT_ID = "1981811783"
GMAIL_ADDRESS = "pdek1991@gmail.com"
GMAIL_APP_PASSWORD = "kcohhtabahwiuixt"


# Initialize Telegram Bot
bot = telegram.Bot(token=TELEGRAM_BOT_TOKEN)

async def send_telegram_message(message_text):
    """Sends a message to the configured Telegram chat."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram API token or Chat ID not configured. Skipping Telegram notification.")
        return
    try:
        await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message_text)
        print(f"Sent message to Telegram: {message_text}")
    except telegram.error.TelegramError as e:
        print(f"Error sending message to Telegram: {e}")

def send_gmail_message(subject, body):
    """Sends an email via Gmail SMTP."""
    if not GMAIL_ADDRESS or not GMAIL_APP_PASSWORD:
        print("Gmail address or App Password not configured. Skipping Gmail notification.")
        return

    try:
        msg = MIMEText(body)
        msg["Subject"] = subject
        msg["From"] = GMAIL_ADDRESS
        msg["To"] = GMAIL_ADDRESS # Send to yourself, or another configured recipient

        context = ssl.create_default_context()
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=context) as server: # Use port 465 for SSL
            server.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
            server.sendmail(GMAIL_ADDRESS, GMAIL_ADDRESS, msg.as_string())
        print(f"Sent email with subject: {subject}")
    except Exception as e:
        print(f"Error sending email: {e}")

async def websocket_message_handler():
    """Connects to the websocket and processes incoming messages."""
    print(f"Attempting to connect to WebSocket at {WS_URL}")
    while True:
        try:
            async with websockets.connect(WS_URL) as websocket:
                print("WebSocket connection established.")
                async for message in websocket:
                    print(f"Received raw message from WebSocket: {message}")

                    try:
                        # Parse the JSON string into a Python dictionary
                        payload = json.loads(message)

                        # Extract data from the payload
                        device_name = payload.get("device_name", "N/A")
                        ip_address = payload.get("ip", "N/A")
                        msg_content = payload.get("message", "N/A")
                        severity = payload.get("severity", "N/A").upper() # Ensure uppercase for consistency
                        
                        # Parse and reformat the time
                        # The time format from payload is like "2025-06-29T17:01:17.899331Z"
                        # We want "29/06/2025, 22:31:19" (assuming local timezone or desired format)
                        # Remove 'Z' for UTC and parse, then format.
                        # Note: The example output (22:31:19) suggests a timezone conversion from 17:01:17.899331Z
                        # This simple parsing will keep it in the script's local timezone.
                        # If you need exact UTC or a specific timezone, more advanced datetime handling is required.
                        try:
                            # Remove the 'Z' and parse without timezone info, then assume UTC for input
                            dt_object_utc = datetime.fromisoformat(payload.get("time", "").replace('Z', ''))
                            # Convert to local time if needed (simple approach, adjust for exact timezone if critical)
                            # This will print the time in the system's local timezone.
                            formatted_time = dt_object_utc.strftime("%d/%m/%Y, %H:%M:%S")
                        except ValueError:
                            formatted_time = "N/A"
                            print(f"Warning: Could not parse time: {payload.get('time')}")


                        # Construct the desired output string
                        formatted_output = (
                            f"Device Name: {device_name}\n"
                            f"IP Address: {ip_address}\n"
                            f"Message: {msg_content}\n"
                            f"Severity: {severity}\n"
                            f"Time: {formatted_time}\n"
                            # You can add other fields from the payload if desired, e.g., group_name, type
                            # f"Group Name: {payload.get('group_name', 'N/A')}\n"
                            # f"Type: {payload.get('type', 'N/A')}"
                        )

                        # Set subject for Gmail (can be dynamic based on severity or device)
                        notification_subject = f"Alert: {severity} on {device_name} ({ip_address})"
                        notification_body = formatted_output # Use the formatted string for both

                        # Send to Telegram (asynchronous)
                        await send_telegram_message(notification_body)

                        # Send to Gmail (synchronous)
                        send_gmail_message(notification_subject, notification_body)

                    except json.JSONDecodeError as e:
                        print(f"Error decoding JSON from WebSocket message: {e}. Message: {message}")
                    except KeyError as e:
                        print(f"Missing key in WebSocket payload: {e}. Payload: {payload}")
                    except Exception as e:
                        print(f"An unexpected error occurred during message processing: {e}. Message: {message}")

        except websockets.exceptions.ConnectionClosedOK:
            print("WebSocket connection closed normally. Attempting to reconnect...")
        except websockets.exceptions.ConnectionClosedError as e:
            print(f"WebSocket connection closed with error: {e}. Attempting to reconnect...")
        except Exception as e:
            print(f"An unexpected error occurred during websocket connection: {e}. Attempting to reconnect in 5 seconds...")
        await asyncio.sleep(5) # Wait before attempting to reconnect

if __name__ == "__main__":
    # Ensure variables are set before starting
    if not all([TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, GMAIL_ADDRESS, GMAIL_APP_PASSWORD]):
        print("Please set all required variables (or environment variables for production): WS_URL, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, GMAIL_ADDRESS, GMAIL_APP_PASSWORD")
    else:
        asyncio.run(websocket_message_handler())