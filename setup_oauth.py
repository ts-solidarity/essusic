"""One-time OAuth2 setup for YouTube authentication."""

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# YouTube TV OAuth2 credentials (same ones yt-dlp-youtube-oauth2 uses)
CLIENT_ID = "861556708454-d6dlm3lh05idd8npek18k6be8ba3oc68.apps.googleusercontent.com"
CLIENT_SECRET = "SboVhoG9s0rNafixCSGGKXAT"

TOKEN_FILE = Path.home() / ".cache" / "yt-dlp" / "youtube-oauth2" / "token.json"


def main() -> None:
    data = urllib.parse.urlencode({
        "client_id": CLIENT_ID,
        "scope": "http://gdata.youtube.com https://www.googleapis.com/auth/youtube",
    }).encode()

    req = urllib.request.Request("https://oauth2.googleapis.com/device/code", data=data)
    with urllib.request.urlopen(req) as resp:
        device = json.loads(resp.read())

    print(f"\n  Go to:       {device['verification_url']}")
    print(f"  Enter code:  {device['user_code']}\n")
    print("Waiting for authorization...")

    interval = device.get("interval", 5)
    while True:
        time.sleep(interval)
        data = urllib.parse.urlencode({
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "device_code": device["device_code"],
            "grant_type": "urn:ietf:params:oauth:2.0:device_code",
        }).encode()

        req = urllib.request.Request("https://oauth2.googleapis.com/token", data=data)
        try:
            with urllib.request.urlopen(req) as resp:
                token = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            error = json.loads(e.read()).get("error")
            if error == "authorization_pending":
                continue
            if error == "slow_down":
                interval += 2
                continue
            raise

        break

    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_FILE.write_text(json.dumps(token))
    print("Authorized! Token saved. You can now start the bot.")


if __name__ == "__main__":
    main()
