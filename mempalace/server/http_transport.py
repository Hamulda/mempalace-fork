"""HTTP transport: serve_http via Starlette + Uvicorn."""
import json
import logging
import sys

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

logger = logging.getLogger("mempalace_mcp")


def serve_http(host="127.0.0.1", port=8765, server=None):
    """Run MemPalace FastMCP server over HTTP using Starlette + Uvicorn."""
    try:
        from starlette.applications import Starlette
        import uvicorn
    except ImportError:
        logger.error("HTTP transport requires starlette and uvicorn.")
        sys.exit(1)

    if server is None:
        from .factory import create_server
        server = create_server(shared_server_mode=True)

    async def http_handle(request: Request) -> Response:
        content_type = request.headers.get("content-type", "")

        if request.method == "GET":
            return JSONResponse({"error": "SSE not implemented. Use POST with application/json."}, status_code=400)

        if request.method == "POST" and "application/json" in content_type:
            try:
                body = await request.body()
                request_data = json.loads(body)
            except (json.JSONDecodeError, UnicodeDecodeError):
                return JSONResponse({"error": "Invalid JSON"}, status_code=400)

            response_data = server.handle_request(request_data)
            if response_data is None:
                return Response(status_code=204)
            return JSONResponse(response_data)

        return JSONResponse({"error": "Unsupported media type"}, status_code=415)

    async def health(request: Request) -> Response:
        return JSONResponse({"status": "ok", "transport": "http"})

    routes = [
        Route("/mcp", http_handle, methods=["GET", "POST"]),
        Route("/health", health, methods=["GET"]),
    ]

    app = Starlette(routes=routes)
    logger.info("MemPalace FastMCP HTTP server starting at http://%s:%d/mcp", host, port)
    uvicorn.run(app, host=host, port=port, log_level="warning")
