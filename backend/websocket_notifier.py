import asyncio
import websockets
import json
import logging
import os
from aiohttp import web # For creating a simple HTTP endpoint

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- Configuration ---
WS_HOST = os.environ.get('WS_HOST', '0.0.0.0') # Listen on all interfaces
WS_PORT = int(os.environ.get('WS_PORT', 8000)) # WebSocket port

HTTP_NOTIFY_HOST = os.environ.get('HTTP_NOTIFY_HOST', '0.0.0.0') # Listen on all interfaces
HTTP_NOTIFY_PORT = int(os.environ.get('HTTP_NOTIFY_PORT', 8001)) # HTTP notification endpoint port

# Set to store all connected WebSocket clients
CONNECTED_CLIENTS = set()

async def register_client(websocket):
    """Registers a new WebSocket client."""
    CONNECTED_CLIENTS.add(websocket)
    logging.info(f"New WebSocket client connected: {websocket.remote_address}. Total clients: {len(CONNECTED_CLIENTS)}")

async def unregister_client(websocket):
    """Unregisters a WebSocket client."""
    CONNECTED_CLIENTS.remove(websocket)
    logging.info(f"WebSocket client disconnected: {websocket.remote_address}. Total clients: {len(CONNECTED_CLIENTS)}")

async def broadcast_message(message):
    """Sends a message to all connected WebSocket clients."""
    # Convert message to JSON string if it's a dict
    message_str = json.dumps(message) if isinstance(message, dict) else message
    
    # Create a list of tasks to send message to each client
    # Filter out websockets that are not open
    send_tasks = [
        client.send(message_str) for client in CONNECTED_CLIENTS if client.open
    ]
    # Use asyncio.gather to run send tasks concurrently
    # return_exceptions=True allows the other tasks to complete even if one fails
    if send_tasks:
        done, pending = await asyncio.wait(send_tasks, timeout=5) # 5 second timeout for sending
        for task in done:
            if task.exception():
                logging.error(f"Error broadcasting message to client: {task.exception()}")
    else:
        logging.debug("No active WebSocket clients to broadcast message to.")

async def websocket_handler(websocket, path):
    """Handles WebSocket connections."""
    # This function is designed to receive 'path' as an argument by the websockets library.
    # The TypeError suggests the calling context (your local setup/websockets version) isn't providing it.
    logging.info(f"WebSocket connection established on path: {path}")
    await register_client(websocket)
    try:
        # Keep the connection open indefinitely
        # Or handle incoming messages if the frontend sends any (e.g., pings, commands)
        await websocket.wait_closed()
    finally:
        await unregister_client(websocket)

async def http_notify_handler(request):
    """
    Handles incoming HTTP POST requests to notify WebSocket clients.
    Expected to receive JSON payload from agents/consumers.
    """
    try:
        data = await request.json()
        logging.info(f"Received HTTP notification from {request.remote}: {data}")
        await broadcast_message(data)
        return web.Response(text="Notification sent", status=200)
    except json.JSONDecodeError:
        logging.error(f"Received invalid JSON from {request.remote}")
        return web.Response(text="Invalid JSON", status=400)
    except Exception as e:
        logging.error(f"Error in HTTP notification handler: {e}", exc_info=True)
        return web.Response(text=f"Internal Server Error: {e}", status=500)

async def main():
    """Main function to start WebSocket and HTTP servers."""
    # Start WebSocket server
    logging.info(f"Starting WebSocket server on ws://{WS_HOST}:{WS_PORT}")
    websocket_server_obj = await websockets.serve(websocket_handler, WS_HOST, WS_PORT)

    # Setup aiohttp application and runner
    app = web.Application()
    app.router.add_post('/notify', http_notify_handler)
    runner = web.AppRunner(app)
    await runner.setup()

    # Create and start the aiohttp TCPSite
    site = web.TCPSite(runner, HTTP_NOTIFY_HOST, HTTP_NOTIFY_PORT)
    await site.start() 

    logging.info(f"Starting HTTP notification server on http://{HTTP_NOTIFY_HOST}:{HTTP_NOTIFY_PORT}/notify")
    logging.debug("Awaiting WebSocket server to run. Aiohttp server is running in background.")
    
    try:
        # Await the WebSocket server to close. This keeps the event loop running.
        # The aiohttp server (managed by 'runner') will continue running alongside it.
        await websocket_server_obj.wait_closed()
    except asyncio.CancelledError:
        # This exception is raised when asyncio.run() or gather cancels tasks (e.g., on KeyboardInterrupt)
        logging.info("WebSocket server task cancelled, initiating aiohttp cleanup.")
    except Exception as e:
        logging.error(f"Unexpected error while awaiting WebSocket server closure: {e}", exc_info=True)
    finally:
        # Ensure aiohttp runner is properly cleaned up when the application exits
        logging.info("Shutting down aiohttp runner and its site.")
        await runner.cleanup() # This correctly cleans up the aiohttp resources

    logging.info("All servers have stopped.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("WebSocket Notifier stopped by user (KeyboardInterrupt).")
    except Exception as e:
        logging.critical(f"WebSocket Notifier terminated due to an unhandled error: {e}", exc_info=True)
