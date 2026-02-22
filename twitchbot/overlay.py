import json
from pathlib import Path

from websockets.http11 import Response
from websockets.datastructures import Headers

from .logger import logger

overlay_clients = set()

OVERLAY_DIR = Path(__file__).resolve().parent.parent / "overlay"


def serve_overlay(connection, request):
    if request.path == "/nowplaying":
        html_path = OVERLAY_DIR / "nowplaying.html"
        if html_path.exists():
            return Response(200, "OK", Headers({
                "Content-Type": "text/html; charset=utf-8",
                "Access-Control-Allow-Origin": "*",
            }), html_path.read_bytes())
        return Response(404, "Not Found", Headers({}), b"File not found")
    if not request.headers.get("Upgrade"):
        return Response(426, "Upgrade Required", Headers({}), b"WebSocket connections only")
    return None  # continue with WebSocket upgrade


async def overlay_handler(websocket):
    overlay_clients.add(websocket)
    logger.info("Overlay connected (clients=%d)", len(overlay_clients))
    try:
        async for _ in websocket:
            pass
    except Exception as e:
        logger.error("Overlay websocket error: %s", e)
    finally:
        overlay_clients.discard(websocket)
        logger.info("Overlay disconnected (clients=%d)", len(overlay_clients))


async def overlay_broadcast(data: dict):
    if not overlay_clients:
        logger.info("No overlay clients connected for broadcast.")
        return

    message = json.dumps(data)
    dead = []

    for ws in overlay_clients:
        try:
            await ws.send(message)
        except Exception as e:
            logger.error("Error sending to overlay client: %s", e)
            dead.append(ws)

    for ws in dead:
        overlay_clients.discard(ws)

    logger.info(
        "Broadcasted overlay message to %d clients: %s",
        len(overlay_clients),
        data.get("command", list(data.keys())),
    )
