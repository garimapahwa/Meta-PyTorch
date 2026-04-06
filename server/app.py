"""ASGI entry point and CLI launcher for multi-mode deployment."""

from __future__ import annotations

import os

import uvicorn

from app import app


def main() -> None:
    """Run the FastAPI app via a console entry point."""
    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "7860"))
    uvicorn.run("server.app:app", host=host, port=port)

