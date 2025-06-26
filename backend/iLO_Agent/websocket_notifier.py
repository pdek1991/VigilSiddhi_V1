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
    if not CONNECTED_CLIENTS:
        logging.info("No WebSocket clients connected to broadcast message.")
        return

    # Convert message to JSON string
    json_message = json.dumps(message)

    disconnected_clients = []
    for websocket in CONNECTED_CLIENTS:
        try:
            await websocket.send(json_message)
            logging.debug(f"Broadcasted to {websocket.remote_address}: {json_message}")
        except websockets.exceptions.ConnectionClosedOK:
            logging.warning(f"Client {websocket.remote_address} was already closed.")
            disconnected_clients.append(websocket)
        except Exception as e:
            logging.error(f"Error broadcasting to {websocket.remote_address}: {e}", exc_info=True)
            disconnected_clients.append(websocket)
    
    # Remove disconnected clients
    for client in disconnected_clients:
        CONNECTED_CLIENTS.discard(client)
    logging.info(f"Message broadcasted. Remaining clients: {len(CONNECTED_CLIENTS)}")


async def websocket_handler(websocket, path):
    """Handles incoming WebSocket connections."""
    await register_client(websocket)
    try:
        async for message in websocket:
            logging.info(f"Received message from WS client {websocket.remote_address}: {message}")
            # Optionally, you could parse and rebroadcast messages received from clients
            # For this use case, consumers are sending via HTTP, so this path is less critical.
    except websockets.exceptions.ConnectionClosedError as e:
        logging.info(f"WebSocket connection closed unexpectedly for {websocket.remote_address}: {e}")
    except Exception as e:
        logging.error(f"Error in WebSocket handler for {websocket.remote_address}: {e}", exc_info=True)
    finally:
        await unregister_client(websocket)

async def http_notify_handler(request):
    """HTTP endpoint to receive notifications from agents and broadcast them."""
    try:
        data = await request.json()
        logging.info(f"Received HTTP notification from {request.remote}: {json.dumps(data)}")
        await broadcast_message(data)
        return web.Response(text="Notification received and broadcasted.", status=200)
    except json.JSONDecodeError:
        logging.error(f"Invalid JSON received from {request.remote}")
        return web.Response(text="Invalid JSON", status=400)
    except Exception as e:
        logging.error(f"Error in HTTP notify handler for {request.remote}: {e}", exc_info=True)
        return web.Response(text=f"Internal Server Error: {e}", status=500)

async def main():
    """Starts both WebSocket server and HTTP notification server."""
    loop = asyncio.get_running_loop() # Get the current event loop

    logging.info(f"Starting WebSocket server on ws://{WS_HOST}:{WS_PORT}")
    # websockets.serve returns a Server object which itself is a Future/Task that can be awaited
    websocket_server_obj = await websockets.serve(websocket_handler, WS_HOST, WS_PORT)

    # Setup aiohttp application and runner
    app = web.Application()
    app.router.add_post('/notify', http_notify_handler)
    runner = web.AppRunner(app)
    await runner.setup()

    # Create aiohttp server directly using loop.create_server
    http_server_coro = loop.create_server(runner.app.make_handler(), HTTP_NOTIFY_HOST, HTTP_NOTIFY_PORT)
    http_server_obj = await http_server_coro

    logging.info(f"Starting HTTP notification server on http://{HTTP_NOTIFY_HOST}:{HTTP_NOTIFY_PORT}/notify")
    logging.debug("Awaiting both WebSocket and HTTP servers to run continuously...")
    
    # Gather both server tasks. wait_closed() will keep them alive until explicitly stopped.
    await asyncio.gather(
        websocket_server_obj.wait_closed(), # The websockets server object has a wait_closed() method
        http_server_obj.wait_closed() # The aiohttp server object also has a wait_closed() method
    )
    logging.debug("Both servers have stopped. This message should not appear unless manually stopped.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("Server stopped by user.")
    except Exception as e:
        logging.critical(f"Server crashed: {e}", exc_info=True)
