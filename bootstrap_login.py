#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""
One-time, local OAuth bootstrap for dreeve-google-health-sync.

Run this on any machine with a browser (your laptop, not the server).
It opens Google's consent screen, captures the authorization code via a
temporary local web server, exchanges it for an access + refresh token,
and writes token.json to the current directory.

Copy the resulting token.json into the volume you mount as TOKEN_DIR for
the sync container -- the container only ever refreshes it, it never
needs a browser.

Required env vars:
  GHEALTH_CLIENT_ID
  GHEALTH_CLIENT_SECRET

Optional:
  CALLBACK_PORT (default 8765)
"""
import http.server
import json
import os
import sys
import threading
import urllib.parse
import urllib.request
import webbrowser
from datetime import datetime, timedelta, timezone

AUTH_URL = "https://accounts.google.com/o/oauth2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
SCOPES = [
    "https://www.googleapis.com/auth/googlehealth.profile.readonly",
    "https://www.googleapis.com/auth/googlehealth.activity_and_fitness.readonly",
    "https://www.googleapis.com/auth/googlehealth.location.readonly",
    "https://www.googleapis.com/auth/googlehealth.health_metrics_and_measurements.readonly",
]

received = {}


class CallbackHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        received["code"] = params.get("code", [None])[0]
        received["error"] = params.get("error", [None])[0]

        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        if received["code"]:
            self.wfile.write(b"<html><body><h2>Login complete, you can close this tab.</h2></body></html>")
        else:
            self.wfile.write(b"<html><body><h2>Login failed, check the terminal.</h2></body></html>")

    def log_message(self, *args):
        pass  # silence default request logging


def main():
    client_id = os.environ.get("GHEALTH_CLIENT_ID")
    client_secret = os.environ.get("GHEALTH_CLIENT_SECRET")
    if not client_id or not client_secret:
        sys.exit("set GHEALTH_CLIENT_ID and GHEALTH_CLIENT_SECRET before running this script")

    port = int(os.environ.get("CALLBACK_PORT", "8765"))
    redirect_uri = f"http://127.0.0.1:{port}/callback"

    server = http.server.HTTPServer(("127.0.0.1", port), CallbackHandler)
    thread = threading.Thread(target=server.handle_request, daemon=True)
    thread.start()

    auth_params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "access_type": "offline",
        "prompt": "consent",
        "scope": " ".join(SCOPES),
    }
    url = f"{AUTH_URL}?{urllib.parse.urlencode(auth_params)}"

    print("Opening browser for Google consent. If it doesn't open, visit:")
    print(url)
    webbrowser.open(url)

    thread.join(timeout=300)
    if not received.get("code"):
        sys.exit(f"login failed: {received.get('error', 'timed out waiting for browser callback')}")

    data = urllib.parse.urlencode({
        "client_id": client_id,
        "client_secret": client_secret,
        "code": received["code"],
        "grant_type": "authorization_code",
        "redirect_uri": redirect_uri,
    }).encode()

    req = urllib.request.Request(TOKEN_URL, data=data, method="POST")
    with urllib.request.urlopen(req) as resp:
        token = json.loads(resp.read())

    if "refresh_token" not in token:
        sys.exit("no refresh_token in response -- remove any prior consent for this app "
                 "at https://myaccount.google.com/permissions and try again")

    token["expiry"] = (datetime.now(timezone.utc) + timedelta(seconds=token["expires_in"])).isoformat()

    with open("token.json", "w") as f:
        json.dump(token, f, indent=2)

    print("\nSaved token.json in the current directory.")
    print("Copy it into the volume you mount as TOKEN_DIR for the sync container, e.g.:")
    print("  cp token.json ./token-data/token.json")


if __name__ == "__main__":
    main()
