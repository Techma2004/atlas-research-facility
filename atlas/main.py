"""ASGI entry point: `uvicorn atlas.main:app`."""
from .server import create_app

app = create_app()
