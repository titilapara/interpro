#!/usr/bin/env python3
"""
InterPro — Professional Real-Time Interpreter Tool
===================================================
DELETE any old interpro_*.py files. Run ONLY this one.
python interpro_final.py
"""
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import tkinter.font as tkfont
import threading, queue, time, json, sys, re
import numpy as np
from pathlib import Path
from typing import Optional
from dataclasses import dataclass
from collections import deque
import websocket  # websocket-client

# ── Constants ────────────────────────────────────────────────────
TARGET_SR    = 16000
CAPTURE_MS   = 50
SUPPORTED_SR = [16000, 44100, 48000, 32000]
ALLOWED_LANGS = {"en", "es"}
VBCABLE_KW  = ["cable output","cable-b","cable-c","cable-d","vb-audio",
               "voicemeeter output","voicemeeter aux","voicemeeter vaio"]
LOOPBACK_KW = ["stereo mix","loopback","what u hear","wave out","mixage","mezcla","monitor of"]

# ── Colours ──────────────────────────────────────────────────────
BG="#090d13"; BG2="#0d1117"; BG3="#131922"; BG4="#1a2232"; BORDER="#1e2a3a"
ACCENT="#00c8ff"; GREEN="#3ddc84"; YELLOW="#f5a623"; RED="#ff5f56"
TEXT="#e2e8f0"; DIM="#64748b"; MUT="#334155"; PURPLE="#b388ff"

# ── Data ─────────────────────────────────────────────────────────
@dataclass
class Phrase:
    id:str; text:str; translation:str; lang:str
    conf:float; ts:float; partial:bool; refined:bool=False

# ── URL builder ──────────────────────────────────────────────────
def build_dg_url(lang_hint: Optional[str]) -> str:
    """
    Build Deepgram WebSocket URL.
    Uses ONLY parameters verified to work in audio_pipeline_test.py.
    NO detect_language (conflicts with language=multi).
    NO vad_events, no_delay, or other unverified params.
    """
    if lang_hint == "en":
        lang = "en-US"
    elif lang_hint == "es":
        lang = "es"
    else:
        lang = "multi"      # Deepgram auto-detects EN/ES with language=multi

    url = (
        "wss://api.deepgram.com/v1/listen"
        f"?model=nova-2"
        f"&language={lang}"
        "&interim_results=true"
        "&punctuate=true"
        "&smart_format=true"
        "&encoding=linear16"
        "&sample_rate=16000"
        "&channels=1"
        "&endpointing=300"
        "&utterance_end_ms=1000"
    )
    return url

# ── Debug logger ─────────────────────────────────────────────────
class Logger:
    def __init__(self, ui_q):
        self.ui_q = ui_q
        self._file = None
        try:
            p = Path.home() / ".interpro" / "logs"
            p.mkdir(parents=True, exist_ok=True)
            self._file = open(p / f"log_{int(time.time())}.txt", "w", encoding="utf-8")
        except: pass

    def log(self, msg, level="INFO"):
        ts = time.strftime("%H:%M:%S")
        line = f"[{ts}] {level:5s} {msg}"
        if self._file:
            try: self._file.write(line + "\n"); self._file.flush()
            except: pass
        try: self.ui_q.put_nowait(("log", level, line))
        except: pass

    def close(self):
        if self._file:
            try: self._file.close()
            except: pass

# ── Audio helpers ─────────────────────────────────────────────────
def list_input_devices():
    import sounddevice as sd
    out = []; apis = sd.query_hostapis()
    for i, d in enumerate(sd.query_devices()):
        if d["max_input_channels"] < 1: continue
        n = d["name"].lower()
        is_vb = any(k in n for k in VBCABLE_KW)
        is_lb = is_vb or any(k in n for k in LOOPBACK_KW)
        out.append({"index":i,"name":d["name"],"api":apis[d["hostapi"]]["name"],
                    "sr":int(d["default_samplerate"]),"channels":int(d["max_input_channels"]),
                    "is_vbcable":is_vb,"is_loopback":is_lb})
    return out

def find_working_sr(dev_idx, log):
    import sounddevice as sd
    for sr in SUPPORTED_SR:
        try:
            sd.check_input_settings(device=dev_idx,channels=1,dtype="float32",samplerate=sr)
            log.log(f"SR {sr}Hz: OK","AUDIO"); return sr
        except Exception as e:
            log.log(f"SR {sr}Hz: fail ({e})","AUDIO")
    return int(sd.query_devices(dev_idx)["default_samplerate"])

class Resampler:
    def __init__(self, src, dst=TARGET_SR):
        self.ratio = dst/src; self._scipy = False
        if src != dst:
            try:
                from scipy.signal import resample_poly
                import math; g=math.gcd(dst,src)
                self._up=dst//g; self._dn=src//g; self._poly=resample_poly; self._scipy=True
            except: pass
    def run(self, audio):
        if self.ratio==1.0: return audio
        if self._scipy: return self._poly(audio,self._up,self._dn).astype(np.float32)
        n=max(1,int(len(audio)*self.ratio))
        return np.interp(np.linspace(0,len(audio)-1,n),np.arange(len(audio)),audio).astype(np.float32)

# ── Translator ───────────────────────────────────────────────────
class Translator:
    def __init__(self):
        self._c={}; self._ok=False
        try: from deep_translator import GoogleTranslator; self._ok=True
        except: pass
    def translate(self, text, src, tgt):
        if not text.strip() or src==tgt or not self._ok: return ""
        k=f"{src}>{tgt}"
        if k not in self._c:
            from deep_translator import GoogleTranslator
            self._c[k]=GoogleTranslator(source=src,target=tgt)
        try: return self._c[k].translate(text) or ""
        except: self._c.pop(k,None); return ""

# ── Deepgram Stream ──────────────────────────────────────────────
class DeepgramStream:
    """
    Connects to Deepgram using websocket-client.
    Exact same pattern as audio_pipeline_test.py (proven working).
    """
    def __init__(self, api_key, ui_q, translator, log):
        self.api_key=api_key; self.ui_q=ui_q
        self.translator=translator; self.log=log
        self._ws=None; self._connected=False; self._running=False
        self._audio_q=queue.Queue(maxsize=500)
        self._lang_hint=None; self._phrase_n=0; self._cur_id=None
        self._bytes_sent=0; self._msgs_recv=0
        self._first_audio_t=None; self._first_resp_t=None
        self._open_evt=threading.Event()

    def connect(self, lang_hint=None):
        self._lang_hint=lang_hint; self._running=True
        self._open_evt.clear(); self._connected=False

        url = build_dg_url(lang_hint)
        self.log.log(f"URL: {url}","WS")      # Log FULL URL so we can verify

        self._ws = websocket.WebSocketApp(
            url,
            header=[f"Authorization: Token {self.api_key}"],
            on_open=self._on_open,
            on_message=self._on_message,
            on_error=self._on_error,
            on_close=self._on_close,
        )
        threading.Thread(
            target=self._ws.run_forever,
            kwargs={"skip_utf8_validation":True},
            daemon=True, name="dg-ws"
        ).start()

        opened = self._open_evt.wait(timeout=10.0)
        if not opened or not self._connected:
            self.log.log("Connection failed or timed out","ERROR")
            return False

        threading.Thread(target=self._sender,daemon=True,name="dg-send").start()
        self.log.log("Connected and sender started","WS")
        return True

    def _on_open(self, ws):
        self._connected=True; self._open_evt.set()
        self.log.log("WebSocket OPEN ✓","WS")
        try: self.ui_q.put_nowait(("ws_status","connected","Deepgram connected ✓"))
        except: pass

    def _on_message(self, ws, raw):
        self._msgs_recv+=1
        try: data=json.loads(raw)
        except: return
        t=data.get("type","")

        if t=="Metadata":
            self.log.log(f"Metadata OK","DG")

        elif t=="Results":
            if self._first_resp_t is None:
                self._first_resp_t=time.time()
                if self._first_audio_t:
                    lat=(self._first_resp_t-self._first_audio_t)*1000
                    self.log.log(f"First transcript latency={lat:.0f}ms","DG")
                    try: self.ui_q.put_nowait(("latency",lat))
                    except: pass

            alts=data.get("channel",{}).get("alternatives",[{}])
            if not alts: return
            alt=alts[0]; text=alt.get("transcript","").strip()
            is_final=data.get("is_final",False); conf=float(alt.get("confidence",0.0))
            if not text: return

            lang=self._lang(alt,data)
            if not is_final:
                if self._cur_id is None:
                    self._phrase_n+=1; self._cur_id=f"ph_{self._phrase_n:05d}"
                pid=self._cur_id
            else:
                pid=self._cur_id or f"ph_{self._phrase_n+1:05d}"
                if self._cur_id is None: self._phrase_n+=1
                self._cur_id=None

            tgt="es" if lang=="en" else "en"
            tr=self.translator.translate(text,lang,tgt)
            p=Phrase(id=pid,text=text,translation=tr,lang=lang,
                     conf=conf,ts=time.time(),partial=not is_final)
            self.log.log(f"{'FINAL' if is_final else 'part'} [{lang}] {text[:50]}","DG")
            try: self.ui_q.put_nowait(("phrase",p))
            except: pass
            try: self.ui_q.put_nowait(("ws_bytes",self._bytes_sent))
            except: pass

        elif t=="SpeechStarted":
            try: self.ui_q.put_nowait(("speech_start",None,None))
            except: pass

        elif t=="UtteranceEnd":
            self._cur_id=None

        elif t=="Error":
            self.log.log(f"DG Error: {data}","ERROR")
            try: self.ui_q.put_nowait(("ws_status","error",str(data.get("message",""))))
            except: pass

    def _on_error(self, ws, err):
        self.log.log(f"WS error: {err}","ERROR")
        self._connected=False; self._open_evt.set()
        try: self.ui_q.put_nowait(("ws_status","error",str(err)[:80]))
        except: pass

    def _on_close(self, ws, code, msg):
        self._connected=False
        self.log.log(f"WS closed code={code}","WS")
        if self._running:
            try: self.ui_q.put_nowait(("ws_status","warn","Disconnected"))
            except: pass

    def _sender(self):
        last_ka=time.time()
        while self._running:
            # Send audio
            while self._connected:
                try: pcm=self._audio_q.get(timeout=0.02)
                except queue.Empty: break
                try:
                    self._ws.send(pcm,websocket.ABNF.OPCODE_BINARY)
                    self._bytes_sent+=len(pcm)
                    if self._first_audio_t is None:
                        self._first_audio_t=time.time()
                        self.log.log(f"First audio sent {len(pcm)}B","WS")
                except Exception as e:
                    self.log.log(f"Send error:{e}","ERROR"); self._connected=False; break
            # Keepalive every 8s
            if self._connected and time.time()-last_ka>8.0:
                try: self._ws.send(json.dumps({"type":"KeepAlive"})); last_ka=time.time()
                except: pass
            time.sleep(0.01)

    def _lang(self, alt, data):
        d=(data.get("channel",{}).get("detected_language","") or
           data.get("metadata",{}).get("detected_language",""))
        if not d:
            words=alt.get("words",[])
            d=words[0].get("language","") if words else ""
        if not d: d=self._lang_hint or "en"
        d=d.lower().split("-")[0]
        return d if d in ALLOWED_LANGS else (self._lang_hint if self._lang_hint in ALLOWED_LANGS else "en")

    def send_pcm(self, pcm):
        try: self._audio_q.put_nowait(pcm)
        except queue.Full: pass

    def stop(self):
        self._running=False
        try:
            if self._ws and self._connected:
                self._ws.send(json.dumps({"type":"CloseStream"}))
                time.sleep(0.2); self._ws.close()
        except: pass

# ── Audio Capture ────────────────────────────────────────────────
class AudioCapture:
    def __init__(self, dev_idx, on_chunk, on_level, log):
        self.dev_idx=dev_idx; self.on_chunk=on_chunk
        self.on_level=on_level; self.log=log
        self._stream=None; self._running=False
        self._sr=None; self._rs=None; self._wave=deque(maxlen=200)
        self._bytes_cap=0

    def start(self):
        import sounddevice as sd
        info=sd.query_devices(self.dev_idx)
        sr=find_working_sr(self.dev_idx,self.log)
        self._sr=sr; self._rs=Resampler(sr,TARGET_SR)
        ch=min(2,int(info["max_input_channels"]))
        block=int(sr*CAPTURE_MS/1000)
        self.log.log(f"Opening [{self.dev_idx}] '{info['name']}' sr={sr} ch={ch} block={block}","AUDIO")

        # Try WASAPI loopback first (captures system audio without Stereo Mix)
        opened=False
        try:
            import sounddevice as sd
            extra=sd.WasapiSettings(loopback=True)
            out_idx=sd.default.device[1]
            out_info=sd.query_devices(out_idx)
            out_sr=find_working_sr(out_idx,self.log)
            self._rs=Resampler(out_sr,TARGET_SR)
            self._stream=sd.InputStream(
                device=out_idx,channels=2,samplerate=out_sr,
                blocksize=int(out_sr*CAPTURE_MS/1000),dtype="float32",
                extra_settings=extra,callback=self._cb,latency="low")
            self._stream.start(); opened=True
            self.log.log(f"WASAPI loopback on output [{out_idx}] '{out_info['name']}' sr={out_sr}","AUDIO")
        except Exception as e:
            self.log.log(f"WASAPI loopback failed: {e} — using direct input","AUDIO")

        if not opened:
            self._stream=sd.InputStream(
                device=self.dev_idx,channels=ch,samplerate=sr,
                blocksize=block,dtype="float32",callback=self._cb,latency="low")
            self._stream.start()
            self.log.log("Direct input stream started","AUDIO")

        self._running=True
        return info["name"]

    def _cb(self, indata, frames, t, status):
        if not self._running: return
        if status: self.log.log(f"Stream status: {status}","WARN")
        mono=indata.mean(axis=1) if indata.ndim>1 else indata[:,0]
        # Pre-emphasis: amplifies high frequencies lost in muffled/distant audio
        mono=np.append(mono[0:1], mono[1:]-0.97*mono[:-1]).astype(np.float32)
        # Soft normalise: bring quiet or muffled input up to a consistent level
        peak=float(np.max(np.abs(mono)))
        if peak>0.001: mono=(mono/peak)*0.85
        chunk=self._rs.run(mono)
        if len(chunk)<1: return
        rms=float(np.sqrt(np.mean(chunk**2)))
        self._bytes_cap+=len(chunk)*2
        self._wave.append(rms)
        try: self.on_level(min(1.0,rms*12),rms>0.003,self._bytes_cap)
        except: pass
        try: self.on_chunk(chunk)
        except: pass

    def stop(self):
        self._running=False
        if self._stream:
            try: self._stream.stop(); self._stream.close()
            except: pass
            self._stream=None

    @property
    def waveform(self): return list(self._wave)

# ── Main App ─────────────────────────────────────────────────────
class App:
    def __init__(self):
        self.root=tk.Tk()
        self.root.title("InterPro — Professional Interpreter")
        self.root.geometry("1280x800"); self.root.minsize(1000,680)
        self.root.configure(bg=BG)
        try: self.root.state("zoomed")
        except: pass

        # State
        self.recording=False; self.session_start=None
        self.committed={}; self.active_id=None; self.active_phrase=None
        self.seg_count=0; self.word_count=0; self._last_meter=0.0
        self._para_text=""; self._para_trans=""; self._para_lang="en"
        self._para_conf=0.0; self._para_ts=None; self._para_timer=None
        self._para_lock=threading.Lock(); self._pending_dev=None

        # Core
        self.ui_q=queue.Queue(maxsize=500)
        self.log=Logger(self.ui_q)
        self.translator=Translator()
        self.devices=[]; self._dg=None; self._cap=None

        self.log.log("InterPro starting","INFO")
        self.log.log(f"Build URL test: {build_dg_url(None)[:80]}","INFO")

        # Tk vars
        self.api_key_var=tk.StringVar(value=self._load_key())
        self.lang_var=tk.StringVar(value="auto")
        self.dev_var=tk.StringVar()
        self.show_tr=tk.BooleanVar(value=True)
        self.autoscroll=tk.BooleanVar(value=True)
        self.font_size=tk.IntVar(value=14)
        self.show_debug=tk.BooleanVar(value=True)  # ON by default so user can see logs

        self._build()
        self._load_devices()
        self._poll()

        self.root.protocol("WM_DELETE_WINDOW",self._quit)
        self.root.mainloop()

    def _keypath(self): return Path.home()/".interpro"/"deepgram.key"
    def _load_key(self):
        try: return self._keypath().read_text().strip()
        except: return ""
    def _save_key_file(self,k):
        p=self._keypath(); p.parent.mkdir(parents=True,exist_ok=True); p.write_text(k.strip())

    # ── UI ──────────────────────────────────────────────────────
    def _build(self):
        # Header
        hdr=tk.Frame(self.root,bg=BG2,height=56); hdr.pack(fill="x"); hdr.pack_propagate(False)
        tk.Frame(self.root,bg=BORDER,height=1).pack(fill="x")
        tk.Label(hdr,text="◈  InterPro",bg=BG2,fg=ACCENT,font=("Helvetica",17,"bold")).pack(side="left",padx=20)
        lf=tk.Frame(hdr,bg=ACCENT,padx=7,pady=3); lf.pack(side="left",pady=18)
        tk.Label(lf,text="LIVE",bg=ACCENT,fg=BG,font=("Helvetica",8,"bold")).pack()
        self.eng_lbl=tk.Label(hdr,text="",bg=BG2,fg=DIM,font=("Helvetica",9)); self.eng_lbl.pack(side="left",padx=14)
        tk.Checkbutton(hdr,text="Debug",variable=self.show_debug,bg=BG2,fg=DIM,
            selectcolor=BG3,activebackground=BG2,font=("Helvetica",9),
            command=self._toggle_debug).pack(side="right",padx=8)
        self.hdr_status=tk.Label(hdr,text="Ready",bg=BG2,fg=DIM,font=("Helvetica",10))
        self.hdr_status.pack(side="right",padx=20)

        # Body
        body=tk.Frame(self.root,bg=BG); body.pack(fill="both",expand=True)
        sb=tk.Frame(body,bg=BG2,width=280); sb.pack(side="left",fill="y"); sb.pack_propagate(False)
        tk.Frame(body,bg=BORDER,width=1).pack(side="left",fill="y")
        self._build_sb(sb)
        mid=tk.Frame(body,bg=BG); mid.pack(side="left",fill="both",expand=True)
        self._build_mid(mid)
        self.dbg_frame=tk.Frame(body,bg=BG2,width=360); self.dbg_frame.pack_propagate(False)
        self._build_dbg(self.dbg_frame)
        if self.show_debug.get(): self.dbg_frame.pack(side="right",fill="y")

    def _build_sb(self,sb):
        p=dict(padx=16)
        # Record btn
        rw=tk.Frame(sb,bg=BG2); rw.pack(fill="x",pady=(20,0),**p)
        self.rec_btn=tk.Button(rw,text="⏺   Start Recording",bg=BG4,fg=TEXT,
            relief="flat",font=("Helvetica",12,"bold"),activebackground=ACCENT,
            activeforeground=BG,cursor="hand2",pady=12,bd=0,command=self._toggle)
        self.rec_btn.pack(fill="x")
        self.rec_lbl=tk.Label(rw,text="Ready",bg=BG2,fg=DIM,font=("Helvetica",9))
        self.rec_lbl.pack(pady=(5,0))
        self._div(sb)

        # API Key
        self._sec(sb,"DEEPGRAM API KEY")
        aw=tk.Frame(sb,bg=BG2); aw.pack(fill="x",**p)
        kr=tk.Frame(aw,bg=BG2); kr.pack(fill="x")
        self.key_entry=tk.Entry(kr,textvariable=self.api_key_var,bg=BG3,fg=TEXT,
            insertbackground=ACCENT,relief="flat",font=("Helvetica",9),show="*")
        self.key_entry.pack(side="left",fill="x",expand=True,ipady=5)
        tk.Button(kr,text="Save",bg=BG4,fg=ACCENT,relief="flat",font=("Helvetica",8),
            cursor="hand2",padx=8,command=self._save_key).pack(side="left",padx=(4,0))
        tk.Label(aw,text="console.deepgram.com",bg=BG2,fg=MUT,font=("Helvetica",8)).pack(anchor="w",pady=(2,0))
        self._div(sb)

        # Device
        self._sec(sb,"AUDIO SOURCE")
        dw=tk.Frame(sb,bg=BG2); dw.pack(fill="x",**p)
        style=ttk.Style(); style.theme_use("clam")
        style.configure("IP.TCombobox",fieldbackground=BG3,background=BG3,
            foreground=TEXT,bordercolor=BORDER,arrowcolor=DIM,
            selectbackground=BG4,selectforeground=TEXT)
        style.map("IP.TCombobox",fieldbackground=[("readonly",BG3)],foreground=[("readonly",TEXT)])
        self.dev_combo=ttk.Combobox(dw,textvariable=self.dev_var,state="readonly",
            style="IP.TCombobox",font=("Helvetica",9))
        self.dev_combo.pack(fill="x"); self.dev_combo.bind("<<ComboboxSelected>>",self._on_dev)
        self.dev_lbl=tk.Label(dw,text="Scanning...",bg=BG2,fg=ACCENT,
            font=("Helvetica",8),wraplength=240,justify="left")
        self.dev_lbl.pack(anchor="w",pady=(4,0))
        self._div(sb)

        # Language
        self._sec(sb,"DIRECTION")
        lw=tk.Frame(sb,bg=BG2); lw.pack(fill="x",**p)
        for v,l in [("auto","Auto EN ↔ ES"),("en","English → Spanish"),("es","Spanish → English")]:
            tk.Radiobutton(lw,text=l,variable=self.lang_var,value=v,bg=BG2,fg=DIM,
                selectcolor=BG3,activebackground=BG2,activeforeground=TEXT,
                font=("Helvetica",9)).pack(anchor="w",pady=1)
        self._div(sb)

        # Meter
        self._sec(sb,"AUDIO LEVEL")
        mw=tk.Frame(sb,bg=BG2); mw.pack(fill="x",**p)
        mbg=tk.Frame(mw,bg=BG4,height=10); mbg.pack(fill="x"); mbg.pack_propagate(False)
        self.meter=tk.Frame(mbg,bg=GREEN,height=10); self.meter.place(x=0,y=0,relheight=1,relwidth=0)
        self.meter_lbl=tk.Label(mw,text="○ Standby",bg=BG2,fg=DIM,font=("Helvetica",8))
        self.meter_lbl.pack(anchor="w",pady=(4,0))
        self.bytes_lbl=tk.Label(mw,text="0 KB captured",bg=BG2,fg=MUT,font=("Helvetica",8))
        self.bytes_lbl.pack(anchor="w")
        self._div(sb)

        # Stats
        self._sec(sb,"SESSION")
        sw=tk.Frame(sb,bg=BG2); sw.pack(fill="x",**p)
        sw.columnconfigure(0,weight=1); sw.columnconfigure(1,weight=1)
        self.sv_time=self._stat(sw,"0:00","Duration",0,0)
        self.sv_words=self._stat(sw,"0","Words",0,1)
        self.sv_segs=self._stat(sw,"0","Segments",1,0)
        self.sv_lang=self._stat(sw,"—","Language",1,1)
        self._div(sb)

        # Pipeline
        self._sec(sb,"STATUS")
        self.pipe_lbl=tk.Label(sb,text="Idle",bg=BG2,fg=DIM,font=("Helvetica",9),wraplength=248,justify="left")
        self.pipe_lbl.pack(anchor="w",padx=16)

        # Bottom
        btn=tk.Frame(sb,bg=BG2); btn.pack(side="bottom",fill="x",padx=16,pady=14)
        tk.Checkbutton(btn,text="Show translation",variable=self.show_tr,bg=BG2,fg=DIM,
            selectcolor=BG3,activebackground=BG2,font=("Helvetica",9),
            command=self._rebuild).pack(anchor="w")
        tk.Checkbutton(btn,text="Auto-scroll",variable=self.autoscroll,bg=BG2,fg=DIM,
            selectcolor=BG3,activebackground=BG2,font=("Helvetica",9)).pack(anchor="w")
        tk.Button(btn,text="Export",bg=BG4,fg=TEXT,relief="flat",font=("Helvetica",9),
            cursor="hand2",pady=6,command=self._export).pack(fill="x",pady=(8,4))
        tk.Button(btn,text="Clear",bg=BG4,fg=DIM,relief="flat",font=("Helvetica",9),
            cursor="hand2",pady=6,command=self._clear).pack(fill="x")

    def _build_mid(self,mid):
        # Live bar
        live=tk.Frame(mid,bg=BG2,pady=14); live.pack(fill="x")
        tk.Frame(mid,bg=BORDER,height=1).pack(fill="x")
        tr=tk.Frame(live,bg=BG2); tr.pack(fill="x",padx=22)
        self.live_dot=tk.Label(tr,text="●",bg=BG2,fg=MUT,font=("Helvetica",9)); self.live_dot.pack(side="left")
        tk.Label(tr,text="  LIVE CAPTION",bg=BG2,fg=MUT,font=("Helvetica",8,"bold")).pack(side="left")
        self.lat_lbl=tk.Label(tr,text="",bg=BG2,fg=MUT,font=("Helvetica",8)); self.lat_lbl.pack(side="right")
        self.live_cap=tk.Label(live,text="Waiting for speech...",bg=BG2,fg=DIM,
            font=("Helvetica",13),anchor="w",wraplength=900,justify="left")
        self.live_cap.pack(fill="x",padx=22,pady=(8,3))
        self.live_tr=tk.Label(live,text="",bg=BG2,fg=TEXT,font=("Helvetica",17,"bold"),
            anchor="w",wraplength=900,justify="left")
        self.live_tr.pack(fill="x",padx=22,pady=(0,4))
        tk.Frame(mid,bg=BORDER,height=1).pack(fill="x")

        # Feed
        ff=tk.Frame(mid,bg=BG); ff.pack(fill="both",expand=True)
        sbar=tk.Scrollbar(ff,bg=BG3,troughcolor=BG2,width=5,relief="flat"); sbar.pack(side="right",fill="y")
        self.feed=tk.Text(ff,bg=BG,fg=TEXT,font=("Helvetica",14),relief="flat",bd=0,
            padx=24,pady=20,spacing1=2,spacing3=10,wrap="word",yscrollcommand=sbar.set,
            state="disabled",cursor="arrow")
        self.feed.pack(side="left",fill="both",expand=True); sbar.config(command=self.feed.yview)
        mono=next((f for f in ["JetBrains Mono","Consolas","Courier New"] if f in tkfont.families()),"Courier")
        self.feed.tag_config("ts",foreground=MUT,font=(mono,9))
        self.feed.tag_config("badge_en",foreground=ACCENT,font=(mono,8,"bold"))
        self.feed.tag_config("badge_es",foreground=YELLOW,font=(mono,8,"bold"))
        self.feed.tag_config("txt_en",foreground=DIM,font=("Helvetica",12))
        self.feed.tag_config("txt_es",foreground=DIM,font=("Helvetica",12))
        self.feed.tag_config("trans",foreground=TEXT,font=("Helvetica",15,"bold"))

        # Confidence strip
        tk.Frame(mid,bg=BORDER,height=1).pack(fill="x",side="bottom")
        cs=tk.Frame(mid,bg=BG2,pady=7); cs.pack(fill="x",side="bottom")
        tk.Label(cs,text="CONF",bg=BG2,fg=MUT,font=("Helvetica",8)).pack(side="left",padx=(18,8))
        cbg=tk.Frame(cs,bg=BG4,width=120,height=4); cbg.pack(side="left"); cbg.pack_propagate(False)
        self.conf_bar=tk.Frame(cbg,bg=ACCENT,height=4); self.conf_bar.place(x=0,y=0,relheight=1,relwidth=0)
        self.conf_lbl=tk.Label(cs,text="—",bg=BG2,fg=DIM,font=("Helvetica",8)); self.conf_lbl.pack(side="left",padx=6)

    def _build_dbg(self,frm):
        tk.Frame(frm,bg=BORDER,width=1).pack(side="left",fill="y")
        inner=tk.Frame(frm,bg=BG2); inner.pack(side="left",fill="both",expand=True)
        tk.Label(inner,text="DEBUG LOG",bg=BG2,fg=ACCENT,font=("Helvetica",10,"bold")).pack(anchor="w",padx=14,pady=(14,6))

        # Stats
        sg=tk.Frame(inner,bg=BG2); sg.pack(fill="x",padx=14,pady=(0,10))
        self.d_cap=self._dr(sg,"Captured","0 B",0)
        self.d_sent=self._dr(sg,"Sent to DG","0 B",1)
        self.d_msgs=self._dr(sg,"DG responses","0",2)
        self.d_ws=self._dr(sg,"WS","disconnected",3)

        # Waveform
        self.wave_cv=tk.Canvas(inner,bg=BG,height=60,highlightthickness=1,highlightbackground=BORDER)
        self.wave_cv.pack(fill="x",padx=14,pady=(0,10))

        # Log
        lf=tk.Frame(inner,bg=BG); lf.pack(fill="both",expand=True,padx=14,pady=(0,14))
        ls=tk.Scrollbar(lf,bg=BG3,troughcolor=BG2,width=5); ls.pack(side="right",fill="y")
        mono=next((f for f in ["Consolas","Courier New"] if f in tkfont.families()),"Courier")
        self.log_txt=tk.Text(lf,bg=BG,fg=TEXT,font=(mono,8),relief="flat",bd=0,
            padx=6,pady=4,wrap="word",yscrollcommand=ls.set,state="disabled",height=20)
        self.log_txt.pack(side="left",fill="both",expand=True); ls.config(command=self.log_txt.yview)
        self.log_txt.tag_config("INFO",foreground=TEXT)
        self.log_txt.tag_config("AUDIO",foreground=ACCENT)
        self.log_txt.tag_config("WS",foreground=PURPLE)
        self.log_txt.tag_config("DG",foreground=GREEN)
        self.log_txt.tag_config("ERROR",foreground=RED)
        self.log_txt.tag_config("WARN",foreground=YELLOW)

    def _dr(self,p,label,val,row):
        tk.Label(p,text=label,bg=BG2,fg=DIM,font=("Helvetica",8)).grid(row=row,column=0,sticky="w",pady=1)
        lbl=tk.Label(p,text=val,bg=BG2,fg=TEXT,font=("Helvetica",8,"bold"))
        lbl.grid(row=row,column=1,sticky="e",pady=1); p.columnconfigure(1,weight=1); return lbl

    def _toggle_debug(self):
        if self.show_debug.get(): self.dbg_frame.pack(side="right",fill="y")
        else: self.dbg_frame.pack_forget()

    def _div(self,p): tk.Frame(p,bg=BORDER,height=1).pack(fill="x",pady=10)
    def _sec(self,p,t): tk.Label(p,text=t,bg=BG2,fg=MUT,font=("Helvetica",8,"bold")).pack(anchor="w",padx=16,pady=(0,4))
    def _stat(self,p,v,k,r,c):
        f=tk.Frame(p,bg=BG2); f.grid(row=r,column=c,sticky="w",padx=(0,16),pady=4)
        lbl=tk.Label(f,text=v,bg=BG2,fg=TEXT,font=("Helvetica",20)); lbl.pack(anchor="w")
        tk.Label(f,text=k,bg=BG2,fg=DIM,font=("Helvetica",8)).pack(anchor="w"); return lbl

    # ── Devices ─────────────────────────────────────────────────
    def _load_devices(self):
        try: self.devices=list_input_devices()
        except Exception as e: self.log.log(f"Device scan error: {e}","ERROR"); return
        names=[]; best=0
        for i,d in enumerate(self.devices):
            tag=" VB-Cable" if d["is_vbcable"] else " Loopback" if d["is_loopback"] else ""
            names.append(f"[{d['index']}] {d['name']}{tag}")
            if d["is_vbcable"] and not any(self.devices[j]["is_vbcable"] for j in range(i)): best=i
        if names:
            self.dev_combo["values"]=names; self.dev_combo.current(best); self._on_dev()
        else:
            self.dev_combo["values"]=["No input devices"]; self.dev_lbl.config(text="None found",fg=RED)

    def _on_dev(self,_=None):
        i=self.dev_combo.current()
        if 0<=i<len(self.devices):
            d=self.devices[i]
            tag="VB-Cable OK" if d["is_vbcable"] else "Loopback OK" if d["is_loopback"] else "Warning: not a loopback"
            self.dev_lbl.config(text=f"{d['name']} — {tag}",fg=ACCENT if(d["is_vbcable"] or d["is_loopback"]) else YELLOW)

    def _save_key(self):
        k=self.api_key_var.get().strip()
        if k: self._save_key_file(k); self.hdr_status.config(text="Key saved",fg=GREEN)
        else: self.hdr_status.config(text="Enter key first",fg=YELLOW)

    # ── Record ──────────────────────────────────────────────────
    def _toggle(self):
        if self.recording: self._stop()
        else: self._start()

    def _start(self):
        api_key=self.api_key_var.get().strip()
        if not api_key:
            messagebox.showerror("No API Key","Enter your Deepgram API key first.\nconsole.deepgram.com"); return
        idx=self.dev_combo.current()
        if idx<0 or idx>=len(self.devices):
            messagebox.showerror("No Device","Select an audio input device."); return
        dev=self.devices[idx]
        lang=self.lang_var.get(); hint=None if lang=="auto" else lang

        self.rec_btn.config(state="disabled",text="Connecting...")
        self.pipe_lbl.config(text="Connecting to Deepgram...",fg=YELLOW)
        self.hdr_status.config(text="Connecting...",fg=YELLOW)

        # Connect in background — _poll handles connect_result so the UI stays live
        self._dg=DeepgramStream(api_key,self.ui_q,self.translator,self.log)
        self._pending_dev=dev
        threading.Thread(target=self._do_connect,args=(self._dg,hint),daemon=True).start()

    def _do_connect(self,dg,hint):
        ok=dg.connect(lang_hint=hint)
        try: self.ui_q.put_nowait(("connect_result",ok))
        except: pass

    def _finish_start(self):
        dev=self._pending_dev
        self._cap=AudioCapture(dev["index"],self._on_chunk,self._on_level,self.log)
        try: dev_name=self._cap.start()
        except Exception as e:
            self.log.log(f"Audio start failed: {e}","ERROR")
            self._dg.stop()
            self.rec_btn.config(state="normal",text="⏺   Start Recording",bg=BG4,fg=TEXT)
            self.pipe_lbl.config(text="Audio error — check Debug log",fg=RED)
            self.hdr_status.config(text="Audio error",fg=RED)
            messagebox.showerror("Audio Error",f"{e}\n\nSet CABLE Input as Default Playback."); return

        self.recording=True; self.session_start=time.time()
        self.rec_btn.config(state="normal",text="⏹   Stop Recording",bg=ACCENT,fg=BG)
        self.rec_lbl.config(text=f"● {dev_name}",fg=ACCENT)
        self.live_dot.config(fg=ACCENT)
        self.live_cap.config(text="Listening…",fg=DIM)
        self.eng_lbl.config(text="Deepgram Nova-2",fg=DIM)
        self.pipe_lbl.config(text="✓ Streaming to Deepgram",fg=GREEN)
        self.hdr_status.config(text="Recording",fg=GREEN)

    def _connect_failed(self):
        self.rec_btn.config(state="normal",text="⏺   Start Recording",bg=BG4,fg=TEXT)
        self.pipe_lbl.config(text="Connection failed — check Debug log",fg=RED)
        self.hdr_status.config(text="Connection failed",fg=RED)
        messagebox.showerror("Connection Failed",
            "Could not connect to Deepgram.\n\nSee the Debug panel for the exact error.\n\n"
            "The URL being used is logged there.")

    def _stop(self):
        self.recording=False
        if self._cap: self._cap.stop(); self._cap=None
        if self._dg: self._dg.stop(); self._dg=None
        self._para_flush()
        self.rec_btn.config(text="⏺   Start Recording",bg=BG4,fg=TEXT)
        self.rec_lbl.config(text="Ready",fg=DIM)
        self.live_dot.config(fg=MUT)
        self.live_cap.config(text="Waiting for speech…",fg=TEXT)
        self.live_tr.config(text=""); self._set_meter(0,False)
        self.pipe_lbl.config(text="Idle",fg=DIM)
        self.hdr_status.config(text="Ready",fg=DIM)
        self._commit_active()

    # ── Audio callbacks ─────────────────────────────────────────
    def _on_chunk(self,chunk):
        if self._dg:
            pcm=(chunk*32767).astype(np.int16).tobytes()
            self._dg.send_pcm(pcm)

    def _on_level(self,lvl,spk,bcap):
        try: self.ui_q.put_nowait(("level",lvl,spk,bcap))
        except: pass

    # ── Poll ─────────────────────────────────────────────────────
    def _poll(self):
        try:
            while True:
                item=self.ui_q.get_nowait(); k=item[0]
                if k=="level":
                    _,lvl,spk,bcap=item
                    self._set_meter(lvl,spk)
                    self.bytes_lbl.config(text=f"{bcap/1024:.1f} KB captured")
                    self.d_cap.config(text=f"{bcap/1024:.1f} KB")
                    if self._dg:
                        self.d_sent.config(text=f"{self._dg._bytes_sent/1024:.1f} KB")
                        self.d_msgs.config(text=str(self._dg._msgs_recv))
                elif k=="connect_result":
                    _,ok=item
                    if ok: self._finish_start()
                    else: self._connect_failed()
                elif k=="phrase": self._handle(item[1])
                elif k=="flush_para": self._para_flush()
                elif k=="ws_status":
                    _,st,msg=item
                    c={"connected":GREEN,"warn":YELLOW,"error":RED}.get(st,DIM)
                    self.pipe_lbl.config(text=msg,fg=c)
                    self.hdr_status.config(text=msg,fg=c)
                    self.d_ws.config(text=msg,fg=c)
                elif k=="ws_bytes":
                    if self._dg: self.d_sent.config(text=f"{self._dg._bytes_sent/1024:.1f} KB")
                elif k=="latency":
                    self.lat_lbl.config(text=f"{int(item[1])}ms")
                elif k=="log":
                    _,level,line=item
                    self._add_log(line,level)
                elif k=="speech_start":
                    self.live_dot.config(fg=ACCENT)
        except queue.Empty: pass

        if self.recording and self.session_start:
            e=int(time.time()-self.session_start)
            self.sv_time.config(text=f"{e//60}:{e%60:02d}")

        if self.show_debug.get() and self._cap:
            self._draw_wave()

        self.root.after(33,self._poll)

    def _set_meter(self,lvl,spk):
        now=time.time()
        if now-self._last_meter<0.033: return
        self._last_meter=now; pct=min(1.0,lvl*4.0)
        self.meter.config(bg=GREEN if pct<0.55 else YELLOW if pct<0.82 else RED)
        self.meter.place(relwidth=pct)
        self.meter_lbl.config(text="● SPEECH DETECTED" if spk else "○ Silence",fg=ACCENT if spk else DIM)

    def _draw_wave(self):
        w=self.wave_cv.winfo_width(); h=self.wave_cv.winfo_height()
        if w<10 or h<10: return
        self.wave_cv.delete("all")
        data=self._cap.waveform if self._cap else []
        if not data: return
        mx=max(max(data),0.01)
        for i,v in enumerate(data):
            x=i*w/len(data); bh=(v/mx)*(h-4)
            self.wave_cv.create_line(x,h,x,h-bh,fill=GREEN if v<0.05 else YELLOW if v<0.15 else RED,width=1)

    def _add_log(self,line,level):
        self.log_txt.config(state="normal")
        self.log_txt.insert("end",line+"\n",level)
        lc=int(self.log_txt.index("end-1c").split(".")[0])
        if lc>400: self.log_txt.delete("1.0",f"{lc-400}.0")
        self.log_txt.see("end"); self.log_txt.config(state="disabled")

    # ── Feed ─────────────────────────────────────────────────────
    PARA_MIN_SENTENCES = 4    # need 4 complete sentences before even considering a flush
    PARA_MIN_WORDS     = 30   # AND at least 30 words — kills "You're." solo flushes
    PARA_SILENCE_SEC   = 4.5  # true paragraph break = 4.5s of silence
    PARA_SILENCE_MID   = 3.5  # still long — speakers pause between sentences naturally
    PARA_MAX_WORDS     = 120  # hard cap raised so long thoughts stay together

    def _handle(self,p):
        self.live_cap.config(text=p.text+("  ▋" if p.partial else ""),
            fg="#475569",font=("Helvetica",12))
        self.live_tr.config(text=p.translation if self.show_tr.get() and p.translation else "",
            fg=TEXT,font=("Helvetica",17,"bold"))
        self.conf_bar.place(relwidth=p.conf); self.conf_lbl.config(text=f"{int(p.conf*100)}%")
        self.sv_lang.config(text=p.lang.upper())
        if p.partial:
            self.active_id=p.id; self.active_phrase=p
        elif p.refined:
            if p.id in self.committed: self.committed[p.id]=p; self._rebuild()
        else:
            self.active_id=None; self.active_phrase=None
            self._para_add(p)
        if self.autoscroll.get(): self.feed.see("end")

    def _para_add(self, p):
        new_text  = p.text.strip()
        new_trans = (p.translation or "").strip()

        # Duplicate / overlap detection:
        # If new text starts with or is contained in our buffer, ignore it.
        # If our buffer starts with the new text, it's a subset — also ignore.
        if self._para_text:
            buf_lower = self._para_text.lower().strip()
            new_lower = new_text.lower()
            if (new_lower in buf_lower or
                buf_lower.endswith(new_lower) or
                new_lower == buf_lower):
                # Pure duplicate — skip entirely
                return
            # If new text starts with what we already have (Deepgram extended it)
            # replace last portion rather than appending
            if new_lower.startswith(buf_lower[:min(30, len(buf_lower))]):
                # Deepgram gave us a superset — replace buffer with new text
                self._para_text  = new_text
                self._para_trans = new_trans
            else:
                # Genuinely new content — append
                sep  = " " if self._para_text  else ""
                sep2 = " " if self._para_trans else ""
                self._para_text  += sep  + new_text
                self._para_trans += sep2 + new_trans
        else:
            self._para_text  = new_text
            self._para_trans = new_trans

        self._para_lang  = p.lang
        self._para_conf  = max(self._para_conf, p.conf)
        if self._para_ts is None: self._para_ts = p.ts

        if self._para_timer:
            self._para_timer.cancel(); self._para_timer = None

        words   = len(self._para_text.split())
        ends    = bool(re.search(r'[.!?](\s|$)', self._para_text))
        n_sents = len([s for s in re.split(r'[.!?]+', self._para_text) if s.strip()])

        if ends and n_sents >= self.PARA_MIN_SENTENCES and words >= self.PARA_MIN_WORDS:
            self._para_flush()
        elif words >= self.PARA_MAX_WORDS:
            self._para_flush()
        else:
            # If a sentence is already complete, wait less before flushing
            wait = self.PARA_SILENCE_MID if ends else self.PARA_SILENCE_SEC
            self._para_timer = threading.Timer(wait, self._sched_flush)
            self._para_timer.daemon = True
            self._para_timer.start()

    @staticmethod
    def _clean_text(text):
        """Remove stutters, normalise spacing, capitalise, ensure closing punctuation."""
        # Collapse word-level stutters: "I I want" → "I want"
        text = re.sub(r'\b(\w+)(\s+\1)+\b', r'\1', text, flags=re.IGNORECASE)
        # Collapse filler phrases Deepgram occasionally duplicates
        text = re.sub(r'(\b\w[\w\s]{0,20}?)\s+\1', r'\1', text)
        text = ' '.join(text.split())
        if not text: return text
        # Capitalise first letter
        text = text[0].upper() + text[1:]
        # Ensure paragraph ends with punctuation
        if text[-1] not in '.!?…': text += '.'
        return text

    def _sched_flush(self):
        try: self.ui_q.put_nowait(("flush_para", None))
        except: pass

    def _para_flush(self):
        with self._para_lock:
            if not self._para_text.strip(): return
            if self._para_timer:
                self._para_timer.cancel(); self._para_timer = None
            text=self._para_text.strip(); trans=self._para_trans.strip()
            lang=self._para_lang; conf=self._para_conf; ts=self._para_ts or time.time()
            self._para_text=""; self._para_trans=""
            self._para_conf=0.0; self._para_ts=None
        text  = App._clean_text(text)
        if trans and trans[-1] not in '.!?…': trans += '.'
        p = Phrase(id=f"para_{self.seg_count:04d}",
                   text=text, translation=trans,
                   lang=lang, conf=conf, ts=ts, partial=False)
        self._append(p)
        if self.autoscroll.get(): self.feed.see("end")

    def _append(self,p):
        self.committed[p.id]=p; self.seg_count+=1; self.word_count+=len(p.text.split())
        self.sv_segs.config(text=str(self.seg_count)); self.sv_words.config(text=str(self.word_count))
        self.feed.config(state="normal"); self._row(p); self.feed.config(state="disabled")

    def _row(self,p):
        ts=time.strftime("%H:%M:%S",time.localtime(p.ts))
        self.feed.insert("end",f"{ts}   ","ts")
        self.feed.insert("end",f"[{p.lang.upper()}]  ",f"badge_{p.lang}")
        self.feed.insert("end",p.text,f"txt_{p.lang}")
        if self.show_tr.get() and p.translation:
            self.feed.insert("end","\n    → "+p.translation,"trans")
        self.feed.insert("end","\n")

    def _rebuild(self):
        self.feed.config(state="normal"); self.feed.delete("1.0","end")
        for p in self.committed.values(): self._row(p)
        self.feed.config(state="disabled")

    def _commit_active(self):
        p=self.active_phrase
        if p and p.id not in self.committed: self._append(p)
        self.active_id=None; self.active_phrase=None

    def _export(self):
        if not self.committed: messagebox.showinfo("Empty","Record first."); return
        path=filedialog.asksaveasfilename(defaultextension=".txt",
            filetypes=[("Text","*.txt")],initialfile=f"interpro_{time.strftime('%Y%m%d_%H%M%S')}.txt")
        if not path: return
        lines=["InterPro Transcript",f"Generated: {time.strftime('%Y-%m-%d %H:%M:%S')}","="*60,""]
        for p in self.committed.values():
            ts=time.strftime("%H:%M:%S",time.localtime(p.ts))
            lines.append(f"[{ts}] [{p.lang.upper()}]  {p.text}")
            if p.translation: lines.append(f"             -> {p.translation}")
            lines.append("")
        with open(path,"w",encoding="utf-8") as f: f.write("\n".join(lines))
        messagebox.showinfo("Exported",f"Saved:\n{path}")

    def _clear(self):
        if not messagebox.askyesno("Clear","Delete all segments?"): return
        self.committed.clear(); self.active_id=self.active_phrase=None
        self.seg_count=self.word_count=0
        self.feed.config(state="normal"); self.feed.delete("1.0","end"); self.feed.config(state="disabled")
        self.sv_segs.config(text="0"); self.sv_words.config(text="0"); self.sv_lang.config(text="—")
        self.live_cap.config(text="Waiting for speech…",fg=TEXT); self.live_tr.config(text="")
        if self.recording: self.session_start=time.time(); self.sv_time.config(text="0:00")

    def _quit(self): self._stop(); self.log.close(); self.root.destroy()


# ── Entry ────────────────────────────────────────────────────────
if __name__=="__main__":
    missing=[]
    for pkg,pip in [("sounddevice","sounddevice"),("numpy","numpy"),
                    ("websocket","websocket-client"),("deep_translator","deep-translator")]:
        try: __import__(pkg)
        except: missing.append(pip)
    if missing:
        root=tk.Tk(); root.withdraw()
        messagebox.showerror("Missing",f"Run:\npip install {' '.join(missing)}")
        sys.exit(1)
    App()
