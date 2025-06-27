import asyncio
import websockets
import json
import logging
import os
from aiohttp import web
# Import WebSocketServerProtocol for type checking in broadcast_message, if needed later.
# Note: DeprecationWarning might appear depending on websockets library version.
# For websockets 10+, websockets.server.WebSocketServerProtocol is the intended import.
from websockets.server import WebSocketServerProtocol 
# Import State for explicit connection state checking in broadcast_message
from websockets.protocol import State 

# Set logging level to INFO for production readiness. 
# Change to DEBUG for more verbose output during development/debugging.
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- Configuration ---
WS_HOST = os.environ.get('WS_HOST', '0.0.0.0') # Listen on all interfaces
WS_PORT = int(os.environ.get('WS_PORT', 8000)) # WebSocket port

HTTP_NOTIFY_HOST = os.environ.get('HTTP_NOTIFY_HOST', '0.0.0.0') # Listen on all interfaces
HTTP_NOTIFY_PORT = int(os.environ.get('HTTP_NOTIFY_PORT', 8001)) # HTTP notification endpoint port

# Set to store all connected WebSocket clients
CONNECTED_CLIENTS = set()

async def register_client(websocket):
    """
    Registers a new WebSocket client. 
    The strict type check `isinstance(websocket, WebSocketServerProtocol)` is removed here permanently
    as it was causing issues in certain environments, preventing valid connections from registering.
    The assumption is that `websocket_handler` only receives valid WebSocket connections after a successful handshake.
    """
    CONNECTED_CLIENTS.add(websocket)
    logging.info(f"New WebSocket client connected: {websocket.remote_address}. Total clients: {len(CONNECTED_CLIENTS)}")

async def unregister_client(websocket):
    """Unregisters a WebSocket client upon disconnection."""
    if websocket in CONNECTED_CLIENTS:
        CONNECTED_CLIENTS.remove(websocket)
        logging.info(f"WebSocket client disconnected: {websocket.remote_address}. Total clients: {len(CONNECTED_CLIENTS)}")
    else:
        # Log a warning if attempting to unregister a client not found in the set,
        # which might indicate a double-unregister or an issue with client tracking.
        logging.warning(f"Attempted to unregister a client not in CONNECTED_CLIENTS: {websocket.remote_address if hasattr(websocket, 'remote_address') else 'unknown'}")

async def broadcast_message(message):
    """
    Sends a message to all connected and actively OPEN WebSocket clients.
    Handles disconnected or invalid clients gracefully by removing them from the set.
    """
    # Convert message to JSON string if it's a dictionary; otherwise, use as is.
    message_str = json.dumps(message) if isinstance(message, dict) else message
    
    send_tasks = []
    clients_to_remove = []

    logging.debug(f"Broadcast initiated. Total clients in set: {len(CONNECTED_CLIENTS)}")
    
    if not CONNECTED_CLIENTS:
        logging.debug("CONNECTED_CLIENTS set is empty, no messages to broadcast.")
        return # No clients to send to

    # Iterate over a copy of the set to allow modification (removal of clients) during iteration.
    for client in list(CONNECTED_CLIENTS): 
        client_info = f"Client {client.remote_address if hasattr(client, 'remote_address') else 'unknown'}"
        
        # Check if the client has a 'state' attribute and is in an OPEN state.
        # This is the primary check for robust message delivery.
        if hasattr(client, 'state') and client.state == State.OPEN: 
            logging.debug(f"Attempting to send message to {client_info} (state: {client.state}).")
            send_tasks.append(client.send(message_str))
        else:
            # If the client is not in the OPEN state or lacks the 'state' attribute (indicating an issue),
            # log a warning and mark it for removal from the connected clients set.
            logging.warning(f"{client_info} is not in OPEN state ({getattr(client, 'state', 'N/A')}) or is an invalid object. Marking for removal.")
            clients_to_remove.append(client)

    # Remove clients that were identified as closed or invalid outside the main loop.
    for client in clients_to_remove:
        if client in CONNECTED_CLIENTS: # Double-check before removing to prevent KeyError
            CONNECTED_CLIENTS.remove(client)
            logging.info(f"Removed client {client.remote_address if hasattr(client, 'remote_address') else 'unknown'} from CONNECTED_CLIENTS. Total clients: {len(CONNECTED_CLIENTS)}")

    # Execute all send tasks concurrently.
    if send_tasks:
        logging.debug(f"Executing {len(send_tasks)} send tasks.")
        # asyncio.wait allows other tasks to run even if some send tasks fail.
        # return_exceptions=True prevents early termination if a single send fails.
        done, pending = await asyncio.wait(send_tasks, timeout=5) # 5-second timeout for sending operations
        for task in done:
            if task.exception():
                # Log any exceptions that occurred during sending, but do not re-raise.
                # This ensures that a failure to send to one client doesn't prevent others from receiving.
                logging.error(f"Error broadcasting message to a client: {task.exception()}", exc_info=True)
    else:
        logging.debug("No active WebSocket clients remaining to broadcast message to after filtering.")

async def websocket_handler(websocket, path=None): 
    """
    Handles individual WebSocket connections.
    This function is called by websockets.serve for each new connection.
    """
    logging.info(f"WebSocket connection established on path: {path if path else '/'}")
    await register_client(websocket) # Register the new client
    try:
        # Keep the connection open indefinitely until the client disconnects.
        # This waits for the WebSocket connection to be closed by the client or server.
        await websocket.wait_closed()
    finally:
        # Ensure the client is unregistered when the connection is closed.
        await unregister_client(websocket)

async def http_notify_handler(request):
    """
    Handles incoming HTTP POST requests to notify WebSocket clients.
    This endpoint allows other services/agents to send notifications to the WebSocket server.
    """
    try:
        data = await request.json() # Expects a JSON payload
        logging.info(f"Received HTTP notification from {request.remote}: {data}")
        await broadcast_message(data) # Broadcast the received data to all WebSocket clients
        return web.Response(text="Notification sent", status=200)
    except json.JSONDecodeError:
        logging.error(f"Received invalid JSON payload from {request.remote}.")
        return web.Response(text="Invalid JSON payload", status=400)
    except Exception as e:
        logging.error(f"Error in HTTP notification handler for {request.remote}: {e}", exc_info=True)
        return web.Response(text=f"Internal Server Error: {e}", status=500)

async def main():
    """
    Main function to initialize and run both the WebSocket server and the HTTP notification server.
    These servers run concurrently within the same asyncio event loop.
    """
    # Start the WebSocket server
    logging.info(f"Starting WebSocket server on ws://{WS_HOST}:{WS_PORT}")
    websocket_server_obj = await websockets.serve(websocket_handler, WS_HOST, WS_PORT)

    # Setup the aiohttp application for the HTTP notification endpoint
    app = web.Application()
    app.router.add_post('/notify', http_notify_handler)
    runner = web.AppRunner(app)
    await runner.setup()

    # Create and start the aiohttp TCP site
    site = web.TCPSite(runner, HTTP_NOTIFY_HOST, HTTP_NOTIFY_PORT)
    await site.start() 

    logging.info(f"Starting HTTP notification server on http://{HTTP_NOTIFY_HOST}:{HTTP_NOTIFY_PORT}/notify")
    logging.debug("Awaiting WebSocket server to run. Aiohttp server is running in background.")
    
    try:
        # Keep the event loop running by awaiting the WebSocket server to close.
        # The aiohttp server (managed by 'runner') will continue running alongside it.
        await websocket_server_obj.wait_closed()
    except asyncio.CancelledError:
        logging.info("WebSocket server task cancelled, initiating aiohttp cleanup.")
    except Exception as e:
        logging.error(f"Unexpected error while awaiting WebSocket server closure: {e}", exc_info=True)
    finally:
        # Ensure the aiohttp runner and its site are properly cleaned up when the application exits.
        logging.info("Shutting down aiohttp runner and its site.")
        await runner.cleanup() 

    logging.info("All servers have stopped.")

if __name__ == "__main__":
    try:
        # Run the main asynchronous function. Handles graceful shutdown on KeyboardInterrupt.
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("WebSocket Notifier stopped by user (KeyboardInterrupt).")
    except Exception as e:
        logging.critical(f"WebSocket Notifier terminated due to an unhandled error: {e}", exc_info=True)
