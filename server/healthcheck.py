#!/usr/bin/env python3
"""Container healthcheck: /v1/health with the bearer token (Docker HEALTHCHECK)."""
import os
import sys
import urllib.request

token = os.environ.get("AMBIENT_TOKEN", "")
port = os.environ.get("AMBIENT_SERVER__PORT", "8378")
req = urllib.request.Request(
    f"http://127.0.0.1:{port}/v1/health",
    headers={"Authorization": f"Bearer {token}"})
try:
    sys.exit(0 if urllib.request.urlopen(req, timeout=4).status == 200 else 1)
except Exception:  # noqa: BLE001
    sys.exit(1)
