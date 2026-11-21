"""Production WSGI entry point for Render."""
import os

if not os.environ.get("SECRET_KEY"):
    raise RuntimeError("Set SECRET_KEY in Render Environment before deployment")

from app import app, init_db

init_db()
