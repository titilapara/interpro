# InterPro — Real-Time Interpreter

A lightweight desktop app that listens to any audio source on your PC and shows live transcription + translation side-by-side. Built for interpreters, language learners, and anyone who needs to follow a conversation in a second language.

## Features

- **Real-time transcription** via [Deepgram Nova-2](https://deepgram.com/) (WebSocket streaming)
- **Live translation** — English ↔ Spanish, or multilingual auto-detect
- **Loopback audio support** — works with VB-Cable, Stereo Mix, or any virtual audio device
- **Dark GUI** built with Tkinter — no browser, no internet UI, runs fully offline except for the API calls
- **Standalone `.exe`** — buildable with PyInstaller for Windows

## Requirements

- Python 3.10+
- A [Deepgram API key](https://console.deepgram.com/) (free tier available)
- Windows 10/11

Install dependencies:

```bash
pip install websocket-client sounddevice numpy scipy deep_translator
```

## Quick start

1. Save your Deepgram API key to `~/.interpro/deepgram.key`
2. Run the app:
   ```bash
   python interpro_final.py
   ```
3. Select your audio input device and click **Start**

## Test your connection

If the app won't connect, run the standalone test first:

```bash
python test_connection.py
```

It will tell you exactly whether the problem is your API key, SSL, or a proxy.

## Build a Windows `.exe`

```bash
build_exe.bat
```

The finished executable lands in `dist\InterPro.exe`.

## File overview

| File | Purpose |
|---|---|
| `interpro_final.py` | Main application |
| `test_connection.py` | Deepgram connection debugger |
| `audio_pipeline_test.py` | Audio capture / device test |
| `create_icon.py` | Generates the app icon |
| `build_exe.bat` | PyInstaller build script |
| `InterPro.bat` | Launcher (no console window) |
