#!/usr/bin/env python3
"""
InterPro Audio Pipeline Diagnostic Tool
========================================
Run this FIRST to verify every stage of the audio pipeline:
  1. List all audio devices and find VB-Cable
  2. Verify sample rate compatibility
  3. Capture 5 seconds of audio and measure actual signal
  4. Test Deepgram WebSocket connection
  5. Stream 5 seconds of audio to Deepgram and verify transcript packets
  6. Report exact failure point if any

USAGE:
  python audio_pipeline_test.py
  python audio_pipeline_test.py --api-key YOUR_KEY
"""
import sys
import time
import json
import argparse
import threading
import numpy as np
from pathlib import Path

def hr(label=""):
    print("\n" + "=" * 70)
    if label: print(f"  {label}")
    if label: print("=" * 70)

def log(msg, color=""):
    colors = {"red": "\033[91m", "green": "\033[92m", "yellow": "\033[93m",
              "blue": "\033[94m", "cyan": "\033[96m", "reset": "\033[0m"}
    if color and sys.stdout.isatty():
        print(f"{colors.get(color,'')}{msg}{colors['reset']}")
    else:
        print(msg)

# ───────────────────────────────────────────────────────────────
# STAGE 1: Verify dependencies
# ───────────────────────────────────────────────────────────────
hr("STAGE 1: Checking dependencies")
deps = {}
for name in ["sounddevice", "numpy", "websocket"]:
    try:
        __import__(name)
        deps[name] = True
        log(f"  ✓ {name}", "green")
    except ImportError:
        deps[name] = False
        log(f"  ✗ {name} — pip install {'websocket-client' if name=='websocket' else name}", "red")

if not all(deps.values()):
    log("\nInstall missing packages first.", "red")
    sys.exit(1)

import sounddevice as sd
import websocket

# ───────────────────────────────────────────────────────────────
# STAGE 2: List all devices, find VB-Cable
# ───────────────────────────────────────────────────────────────
hr("STAGE 2: Audio devices inventory")

devices = sd.query_devices()
hostapis = sd.query_hostapis()

log("\nALL DEVICES:", "cyan")
input_devs = []
for i, d in enumerate(devices):
    api = hostapis[d["hostapi"]]["name"]
    ch_in = d["max_input_channels"]
    ch_out = d["max_output_channels"]
    flags = []
    nl = d["name"].lower()
    if "cable" in nl or "vb-audio" in nl: flags.append("VB-CABLE")
    if "stereo mix" in nl or "loopback" in nl: flags.append("LOOPBACK")
    flag_str = f" [{', '.join(flags)}]" if flags else ""
    marker = "►" if flags else " "
    print(f"  {marker} [{i:2d}] in={ch_in} out={ch_out}  sr={int(d['default_samplerate']):5d}  ({api:18s}) {d['name']}{flag_str}")
    if ch_in > 0:
        input_devs.append((i, d, api))

log(f"\nDEFAULTS:", "cyan")
def_in_idx = sd.default.device[0]
def_out_idx = sd.default.device[1]
print(f"  Default input  : [{def_in_idx}] {sd.query_devices(def_in_idx)['name']}")
print(f"  Default output : [{def_out_idx}] {sd.query_devices(def_out_idx)['name']}")

# Find best capture device
log("\nCAPTURE CANDIDATE SEARCH:", "cyan")
best = None
for i, d, api in input_devs:
    nl = d["name"].lower()
    if "cable output" in nl:
        best = (i, d, api, "VB-Cable Output")
        break
if not best:
    for i, d, api in input_devs:
        if "vb-audio" in d["name"].lower():
            best = (i, d, api, "VB-Audio device")
            break
if not best:
    for i, d, api in input_devs:
        if "stereo mix" in d["name"].lower():
            best = (i, d, api, "Stereo Mix")
            break

if best:
    log(f"  ✓ Will capture from: [{best[0]}] {best[1]['name']}  ({best[2]})", "green")
    log(f"    Type: {best[3]}", "green")
else:
    log("  ✗ No VB-Cable or Stereo Mix found!", "red")
    log("  Available inputs:", "yellow")
    for i, d, api in input_devs:
        print(f"    [{i}] {d['name']} ({api})")
    log("\nFIX: Install VB-Cable from https://vb-audio.com/Cable/", "yellow")
    sys.exit(1)

CAPTURE_IDX = best[0]
CAPTURE_DEV = best[1]

# ───────────────────────────────────────────────────────────────
# STAGE 3: Find compatible sample rate
# ───────────────────────────────────────────────────────────────
hr("STAGE 3: Sample rate compatibility check")

working_rates = []
for sr in [16000, 22050, 32000, 44100, 48000]:
    try:
        sd.check_input_settings(device=CAPTURE_IDX, channels=1,
                                 dtype="float32", samplerate=sr)
        working_rates.append(sr)
        log(f"  ✓ {sr} Hz mono OK", "green")
    except Exception as e:
        log(f"  ✗ {sr} Hz mono: {e}", "yellow")

# Also try stereo
log("\nStereo:")
for sr in [44100, 48000]:
    try:
        sd.check_input_settings(device=CAPTURE_IDX, channels=2,
                                 dtype="float32", samplerate=sr)
        log(f"  ✓ {sr} Hz stereo OK", "green")
    except Exception as e:
        log(f"  ✗ {sr} Hz stereo: {e}", "yellow")

if not working_rates:
    log("✗ No working sample rate found!", "red")
    sys.exit(1)

CAPTURE_SR = working_rates[0]
log(f"\nUsing capture sample rate: {CAPTURE_SR} Hz", "green")

# ───────────────────────────────────────────────────────────────
# STAGE 4: Capture 5 seconds and analyse
# ───────────────────────────────────────────────────────────────
hr("STAGE 4: Real audio capture test (5 seconds)")
log("\n>>> PLAY AUDIO NOW (YouTube, music, anything) <<<", "yellow")
log("Capturing for 5 seconds...", "cyan")

captured = []
rms_history = []

def cap_callback(indata, frames, t, status):
    if status:
        log(f"  ⚠ Status: {status}", "yellow")
    mono = indata.mean(axis=1) if indata.ndim > 1 else indata[:, 0]
    captured.append(mono.copy())
    rms = float(np.sqrt(np.mean(mono ** 2)))
    rms_history.append(rms)
    # Live print
    bar = "█" * int(min(rms * 100, 50))
    sys.stdout.write(f"\r  RMS: {rms:.5f}  {bar:<50}")
    sys.stdout.flush()

try:
    with sd.InputStream(device=CAPTURE_IDX, channels=1,
                        samplerate=CAPTURE_SR,
                        blocksize=int(CAPTURE_SR * 0.1),  # 100ms blocks
                        dtype="float32", callback=cap_callback):
        sd.sleep(5000)
    print()
except Exception as e:
    log(f"\n✗ Capture failed: {e}", "red")
    sys.exit(1)

audio = np.concatenate(captured)
total_rms = float(np.sqrt(np.mean(audio ** 2)))
total_peak = float(np.max(np.abs(audio)))
avg_rms = np.mean(rms_history)

log("\nCAPTURE RESULTS:", "cyan")
print(f"  Duration       : {len(audio)/CAPTURE_SR:.2f}s ({len(audio)} samples)")
print(f"  Total RMS      : {total_rms:.6f}")
print(f"  Peak amplitude : {total_peak:.6f}")
print(f"  Avg block RMS  : {avg_rms:.6f}")
print(f"  Active blocks  : {sum(1 for r in rms_history if r > 0.001)}/{len(rms_history)}")

if total_rms < 0.0001:
    log("\n✗ AUDIO IS SILENT — VB-Cable is NOT receiving system audio.", "red")
    log("\nFIX:", "yellow")
    log("  1. Right-click speaker → Sound settings → More sound settings", "yellow")
    log("  2. Playback tab → right-click 'CABLE Input' → Set as Default Device", "yellow")
    log("  3. Play audio in Chrome and run this test again", "yellow")
    sys.exit(1)
elif total_rms < 0.002:
    log("\n⚠ Audio is very quiet — check volume levels", "yellow")
else:
    log("\n✓ Audio capture working — real signal detected", "green")

# Save sample for inspection
import wave
out_path = Path("audio_test_capture.wav")
with wave.open(str(out_path), "wb") as wf:
    wf.setnchannels(1)
    wf.setsampwidth(2)
    wf.setframerate(CAPTURE_SR)
    wf.writeframes((audio * 32767).astype(np.int16).tobytes())
log(f"  Sample saved to: {out_path.absolute()}", "blue")

# ───────────────────────────────────────────────────────────────
# STAGE 5: Deepgram WebSocket connection test
# ───────────────────────────────────────────────────────────────
hr("STAGE 5: Deepgram WebSocket test")

parser = argparse.ArgumentParser()
parser.add_argument("--api-key", default=None)
args, _ = parser.parse_known_args()

api_key = args.api_key
if not api_key:
    # Try loading from disk
    keyfile = Path.home() / ".interpro" / "deepgram.key"
    if keyfile.exists():
        api_key = keyfile.read_text().strip()
        log(f"  Loaded API key from {keyfile}", "blue")

if not api_key:
    api_key = input("  Enter your Deepgram API key (or paste from console.deepgram.com): ").strip()

if not api_key or len(api_key) < 20:
    log("  ✗ Invalid API key", "red")
    sys.exit(1)

log(f"  API key: {api_key[:8]}...{api_key[-4:]} ({len(api_key)} chars)", "blue")

# ───────────────────────────────────────────────────────────────
# STAGE 6: Stream test audio to Deepgram and watch for transcripts
# ───────────────────────────────────────────────────────────────
hr("STAGE 6: Stream live audio to Deepgram (15 seconds)")

ws_state = {
    "connected": False,
    "metadata_received": False,
    "first_audio_sent": None,
    "first_response": None,
    "transcripts": [],
    "errors": [],
    "bytes_sent": 0,
    "messages_received": 0,
}

DG_URL = (
    "wss://api.deepgram.com/v1/listen"
    "?model=nova-2"
    "&language=multi"
    "&interim_results=true"
    "&punctuate=true"
    "&smart_format=true"
    "&encoding=linear16"
    "&sample_rate=16000"
    "&channels=1"
    "&endpointing=300"
    "&utterance_end_ms=1000"
    "&vad_events=true"
)

def ws_on_open(ws):
    ws_state["connected"] = True
    log("  ✓ WebSocket OPEN", "green")

def ws_on_message(ws, msg):
    ws_state["messages_received"] += 1
    try:
        data = json.loads(msg)
    except Exception as e:
        log(f"  ✗ Bad message: {e}", "red")
        return
    msg_type = data.get("type", "?")

    if msg_type == "Metadata":
        ws_state["metadata_received"] = True
        log(f"  ✓ METADATA received — model: {data.get('model_info',{}).get('name','?')}", "green")

    elif msg_type == "Results":
        if ws_state["first_response"] is None:
            ws_state["first_response"] = time.time()
            latency = ws_state["first_response"] - ws_state["first_audio_sent"]
            log(f"  ✓ First transcript! Latency: {latency*1000:.0f}ms", "green")

        alts = data.get("channel", {}).get("alternatives", [{}])
        text = alts[0].get("transcript", "").strip()
        is_final = data.get("is_final", False)
        if text:
            tag = "[FINAL]" if is_final else "[partial]"
            log(f"  {tag} {text}", "cyan" if is_final else "blue")
            ws_state["transcripts"].append((is_final, text))

    elif msg_type == "SpeechStarted":
        log(f"  → Speech started", "blue")
    elif msg_type == "UtteranceEnd":
        log(f"  → Utterance end", "blue")
    elif msg_type == "Error":
        log(f"  ✗ DEEPGRAM ERROR: {data}", "red")
        ws_state["errors"].append(data)
    else:
        log(f"  ? Unknown message type: {msg_type}: {data}", "yellow")

def ws_on_error(ws, err):
    log(f"  ✗ WS error: {err}", "red")
    ws_state["errors"].append(str(err))

def ws_on_close(ws, code, msg):
    log(f"  → WS closed: code={code} msg={msg}", "yellow")

# Connect
log("\nConnecting to Deepgram...", "cyan")
ws = websocket.WebSocketApp(
    DG_URL,
    header=[f"Authorization: Token {api_key}"],
    on_open=ws_on_open,
    on_message=ws_on_message,
    on_error=ws_on_error,
    on_close=ws_on_close,
)

ws_thread = threading.Thread(target=ws.run_forever, daemon=True, name="ws-test")
ws_thread.start()

# Wait for connection
for _ in range(50):
    if ws_state["connected"]: break
    time.sleep(0.1)

if not ws_state["connected"]:
    log("\n✗ Could not connect to Deepgram", "red")
    if ws_state["errors"]:
        log("Errors:", "red")
        for e in ws_state["errors"]: log(f"  {e}", "red")
    log("\nCommon causes:", "yellow")
    log("  • Wrong API key (check console.deepgram.com)", "yellow")
    log("  • No internet connection", "yellow")
    log("  • Firewall blocking wss://", "yellow")
    sys.exit(1)

# Stream audio
log("\n>>> PLAY AUDIO NOW (speak or play YouTube) <<<", "yellow")
log("Streaming live audio to Deepgram for 15 seconds...", "cyan")

# Resample to 16kHz if needed
need_resample = CAPTURE_SR != 16000
if need_resample:
    log(f"  Resampling {CAPTURE_SR}Hz → 16000Hz", "blue")

stop_streaming = threading.Event()

def stream_callback(indata, frames, t, status):
    if stop_streaming.is_set(): return
    if status:
        log(f"  ⚠ Stream status: {status}", "yellow")

    mono = indata.mean(axis=1) if indata.ndim > 1 else indata[:, 0]

    # Resample if needed
    if need_resample:
        n = int(len(mono) * 16000 / CAPTURE_SR)
        if n < 1: return
        mono = np.interp(np.linspace(0, len(mono)-1, n),
                          np.arange(len(mono)), mono).astype(np.float32)

    rms = float(np.sqrt(np.mean(mono ** 2)))

    # Send PCM int16
    pcm = (mono * 32767).astype(np.int16).tobytes()

    if ws_state["connected"] and not stop_streaming.is_set():
        try:
            ws.send(pcm, websocket.ABNF.OPCODE_BINARY)
            if ws_state["first_audio_sent"] is None:
                ws_state["first_audio_sent"] = time.time()
                log(f"  → First audio packet sent ({len(pcm)} bytes)", "green")
            ws_state["bytes_sent"] += len(pcm)
        except Exception as e:
            log(f"  ✗ Send failed: {e}", "red")

    # Print level
    bar = "█" * int(min(rms * 100, 40))
    sent_kb = ws_state["bytes_sent"] / 1024
    sys.stdout.write(f"\r  RMS:{rms:.4f} {bar:<40} sent:{sent_kb:.1f}KB  resp:{ws_state['messages_received']}")
    sys.stdout.flush()

try:
    with sd.InputStream(device=CAPTURE_IDX, channels=1,
                        samplerate=CAPTURE_SR,
                        blocksize=int(CAPTURE_SR * 0.05),  # 50ms blocks
                        dtype="float32", callback=stream_callback):
        sd.sleep(15000)
    print()
except Exception as e:
    log(f"\n✗ Stream failed: {e}", "red")

stop_streaming.set()

# Send close frame
try:
    ws.send(json.dumps({"type": "CloseStream"}))
    time.sleep(1.0)
    ws.close()
except: pass

# ───────────────────────────────────────────────────────────────
# Final report
# ───────────────────────────────────────────────────────────────
hr("FINAL REPORT")

stage_status = lambda ok, msg: log(f"  {'✓' if ok else '✗'} {msg}", "green" if ok else "red")

stage_status(True, f"Stage 1: Dependencies installed")
stage_status(True, f"Stage 2: VB-Cable detected at index {CAPTURE_IDX}")
stage_status(True, f"Stage 3: Sample rate {CAPTURE_SR}Hz works")
stage_status(total_rms > 0.0001, f"Stage 4: Audio capture (RMS={total_rms:.5f})")
stage_status(ws_state["connected"], "Stage 5: Deepgram WS connection")
stage_status(ws_state["metadata_received"], "Stage 6a: Deepgram metadata received")
stage_status(ws_state["bytes_sent"] > 0, f"Stage 6b: Audio sent to Deepgram ({ws_state['bytes_sent']} bytes)")
stage_status(ws_state["first_response"] is not None, f"Stage 6c: Transcripts received ({len(ws_state['transcripts'])})")

if ws_state["transcripts"]:
    log("\nLast 5 transcripts received:", "cyan")
    for is_final, text in ws_state["transcripts"][-5:]:
        tag = "[FINAL]  " if is_final else "[partial]"
        print(f"  {tag} {text}")

if ws_state["errors"]:
    log("\nErrors during test:", "red")
    for e in ws_state["errors"]: log(f"  {e}", "red")

# Diagnose
log("\nDIAGNOSIS:", "cyan")
if total_rms < 0.0001:
    log("  ✗ AUDIO CAPTURE BROKEN — VB-Cable not receiving system audio", "red")
    log("    → Set CABLE Input as Default Playback device in Windows", "yellow")
elif not ws_state["connected"]:
    log("  ✗ WEBSOCKET BROKEN — check API key and internet", "red")
elif ws_state["bytes_sent"] == 0:
    log("  ✗ AUDIO NOT BEING SENT — capture callback issue", "red")
elif ws_state["first_response"] is None:
    log("  ✗ DEEPGRAM NOT RESPONDING — audio sent but no transcripts", "red")
    log("    → Audio may be silent or incompatible format", "yellow")
    log("    → Check console.deepgram.com → Usage to see if credits consumed", "yellow")
elif not ws_state["transcripts"]:
    log("  ⚠ Connection works but no transcripts (silent audio?)", "yellow")
else:
    log("  ✓ FULL PIPELINE WORKING ✓", "green")
    log(f"    {len(ws_state['transcripts'])} transcripts received", "green")
    log(f"    {ws_state['bytes_sent']/1024:.1f}KB sent", "green")
    log(f"    First-response latency: {(ws_state['first_response']-ws_state['first_audio_sent'])*1000:.0f}ms", "green")

print()
input("Press Enter to exit...")
