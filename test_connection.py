#!/usr/bin/env python3
"""
Quick Deepgram connection test — run this to see the exact error.
python test_connection.py
"""
import websocket
import json
import time
import threading
from pathlib import Path

# Load API key
keyfile = Path.home() / ".interpro" / "deepgram.key"
try:
    api_key = keyfile.read_text().strip()
    print(f"API key loaded: {api_key[:8]}...{api_key[-4:]}")
except:
    api_key = input("Paste your Deepgram API key: ").strip()

URL = (
    "wss://api.deepgram.com/v1/listen"
    "?model=nova-2&language=multi&interim_results=true"
    "&encoding=linear16&sample_rate=16000&channels=1"
)

print(f"\nConnecting to: {URL[:60]}...")
print(f"Header: Authorization: Token {api_key[:8]}...\n")

result = {"done": False, "success": False, "error": None}

def on_open(ws):
    print("✓ WebSocket OPENED successfully")
    result["success"] = True
    result["done"] = True

def on_error(ws, error):
    print(f"✗ WebSocket ERROR: {error}")
    print(f"  Type: {type(error)}")
    result["error"] = str(error)
    result["done"] = True

def on_close(ws, code, msg):
    print(f"WebSocket CLOSED: code={code} msg={msg}")
    result["done"] = True

def on_message(ws, msg):
    print(f"Message: {msg[:100]}")

ws = websocket.WebSocketApp(
    URL,
    header=[f"Authorization: Token {api_key}"],
    on_open=on_open,
    on_error=on_error,
    on_close=on_close,
    on_message=on_message,
)

t = threading.Thread(target=ws.run_forever, daemon=True)
t.start()

# Wait up to 10s
for i in range(100):
    if result["done"]:
        break
    time.sleep(0.1)
    if i % 10 == 9:
        print(f"  Waiting... {(i+1)//10}s")

if not result["done"]:
    print("✗ TIMEOUT — no response after 10s")
elif result["success"]:
    print("\n✓ CONNECTION WORKS — problem is in the app code")
    ws.close()
else:
    print(f"\n✗ CONNECTION FAILED")
    print(f"  Error: {result['error']}")
    print("\nCommon causes:")
    print("  • websocket-client version issue — try: pip install websocket-client --upgrade")
    print("  • SSL certificate issue")
    print("  • Proxy blocking wss://")

input("\nPress Enter to exit...")
