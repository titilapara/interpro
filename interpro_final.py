#!/usr/bin/env python3
"""
Sayvo — Professional Real-Time Interpreter Tool
===================================================
  LIVE zone   — what's being said right now (Deepgram transcript)
  Bubble zone — each finished paragraph + its Claude (claude-haiku-4-5) translation
"""
__version__ = "44"
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import tkinter.font as tkfont
import customtkinter as ctk
import threading, queue, time, json, sys, re
import numpy as np
from pathlib import Path
from dataclasses import dataclass
from collections import deque, Counter
import websocket  # websocket-client

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

# ── Constants ────────────────────────────────────────────────────
TARGET_SR    = 16000
CAPTURE_MS   = 30
SUPPORTED_SR = [16000, 44100, 48000, 32000]

LANGUAGES = {
    "Auto Detect": ("multi", "auto"),
    "English":     ("en-US", "en"),
    "Spanish":     ("es",    "es"),
    "French":      ("fr",    "fr"),
    "German":      ("de",    "de"),
    "Italian":     ("it",    "it"),
    "Portuguese":  ("pt",    "pt"),
    "Dutch":       ("nl",    "nl"),
    "Russian":     ("ru",    "ru"),
    "Ukrainian":   ("uk",    "uk"),
    "Polish":      ("pl",    "pl"),
    "Swedish":     ("sv",    "sv"),
    "Turkish":     ("tr",    "tr"),
    "Arabic":      ("ar",    "ar"),
    "Hindi":       ("hi",    "hi"),
    "Japanese":    ("ja",    "ja"),
    "Korean":      ("ko",    "ko"),
    "Chinese":     ("zh",    "zh-CN"),
}
DG_TO_TRANS = {
    "en":"en","es":"es","fr":"fr","de":"de","it":"it","pt":"pt",
    "nl":"nl","ru":"ru","uk":"uk","pl":"pl","sv":"sv","tr":"tr",
    "ar":"ar","hi":"hi","ja":"ja","ko":"ko","zh":"zh-CN",
}
LANG_NAMES   = list(LANGUAGES.keys())
LANG_NAMES_T = [n for n in LANG_NAMES if n != "Auto Detect"]
# translation-code → display name  (e.g. "es" → "Spanish", "zh-CN" → "Chinese")
TRANS_NAME   = {v[1]: k for k, v in LANGUAGES.items() if k != "Auto Detect"}

VBCABLE_KW  = ["cable output","cable-b","cable-c","cable-d","vb-audio",
               "voicemeeter output","voicemeeter aux","voicemeeter vaio"]
LOOPBACK_KW = ["stereo mix","loopback","what u hear","wave out","mixage","mezcla","monitor of"]

# ── Palette ──────────────────────────────────────────────────────
BG="#0a0c13"; BG2="#0f121c"; BG3="#161b2c"; BG4="#1e2540"; BORDER="#28304d"
ACCENT="#6c7ffb"; GREEN="#2dd4a7"; YELLOW="#f5c542"; RED="#e5484d"
TEXT="#eef1ff"; DIM="#5b6b8f"; MUT="#182136"; PURPLE="#a78bfa"
ACCENT_HOV="#5468ea"; RED_HOV="#c93d42"

# ── Bubble / speaker colors ───────────────────────────────────────
BUBBLE_EN  = "#111c38"   # English paragraph — deep blue
BUBBLE_ES  = "#1d1233"   # Spanish paragraph — deep purple
BUBBLE_OTH = "#10131d"   # other / unknown

# ── Typography ───────────────────────────────────────────────────
F  = "Segoe UI"
FM = "Consolas"

# ── Data ─────────────────────────────────────────────────────────
@dataclass
class Phrase:
    id: str; text: str; translation: str; lang: str
    conf: float; ts: float; partial: bool
    refined: bool = False; speaker: int = -1
    ts_end: float = 0.0   # absolute end time from Deepgram word timestamps

# ── URL builder ──────────────────────────────────────────────────
def build_dg_url(src_lang_name: str, keyterms: list[str] | None = None) -> str:
    lang = LANGUAGES.get(src_lang_name, ("multi","auto"))[0]
    base = (
        "wss://api.deepgram.com/v1/listen"
        f"?model=nova-3"
        f"&language={lang}"
        "&interim_results=true"
        "&no_delay=true"
        "&punctuate=true"
        "&smart_format=true"
        "&diarize=true"
        "&encoding=linear16"
        "&sample_rate=16000"
        "&channels=1"
        "&endpointing=500"
        "&utterance_end_ms=1000"
    )
    if keyterms:
        from urllib.parse import quote
        base += "".join(f"&keyterm={quote(t)}" for t in keyterms if t.strip())
    return base

# ── Number / currency formatters ─────────────────────────────────
# Single spoken digits only (0-9). Larger number words are deliberately
# NOT converted: piecewise conversion mangles compounds — "dos mil cuatro"
# (2004) used to become "2 1000 4". Deepgram smart_format and Claude both
# handle compound numbers correctly; we only help with digit-by-digit
# dictation (phones, account numbers).
_DIGIT_WORDS = {
    "zero":"0","oh":"0","one":"1","two":"2","three":"3","four":"4",
    "five":"5","six":"6","seven":"7","eight":"8","nine":"9",
    "cero":"0","uno":"1","una":"1","dos":"2","tres":"3","cuatro":"4",
    "cinco":"5","seis":"6","siete":"7","ocho":"8","nueve":"9",
}
def _words_to_digits(text: str) -> str:
    """Normalise DIGIT-DICTATION runs (phone / account / confirmation numbers)
    into individual space-separated digits — even when Deepgram hands back a
    MIX of spoken words, single digits and 2-digit chunks
    ("seis uno 15 7 4 4 9 0 0" -> "6 1 1 5 7 4 4 9 0 0"). Only fires when a
    contiguous run of 3+ number tokens is MOSTLY single digits, so ordinary
    counts like "20 30 40" are left alone. This is what stops the LLM from
    dropping/duplicating digits in a spoken number."""
    toks = text.split(' ')
    out, i = [], 0

    def digtok(tok):
        # -> (digit_string, is_single, ender_punct) or (None, False, "")
        stripped = tok.rstrip('.,;:!?…')
        tail = tok[len(stripped):]
        ender = ''.join(c for c in tail if c in '.!?…')   # commas don't end a run
        core = _deaccent(stripped.strip('¿¡()"').lower())
        if core in _DIGIT_WORDS:
            return _DIGIT_WORDS[core], True, ender
        if core.isdigit() and 1 <= len(core) <= 2:
            return core, (len(core) == 1), ender
        return None, False, ""

    while i < len(toks):
        d0, _s, _e = digtok(toks[i])
        if d0 is None:
            out.append(toks[i]); i += 1; continue
        run, j = [], i
        while j < len(toks):
            dj, sj, ej = digtok(toks[j])
            if dj is None:
                break
            run.append((dj, sj, ej)); j += 1
            if ej:                       # a sentence-ender closes the run
                break
        singles = sum(1 for d, s, e in run if s)
        if len(run) >= 3 and singles >= 2 and singles / len(run) >= 0.6:
            digits = ''.join(d for d, s, e in run)
            out.append(' '.join(digits) + (run[-1][2] or "")); i = j
        else:
            out.extend(toks[i:j]); i = j
    return ' '.join(out)

def _join_digit_runs(text: str) -> str:
    """After phone formatting, join any remaining run of 4+ single spaced
    digits into one solid number (account / confirmation / ID). Phone-length
    runs (7/10/11) are already turned into formatted phones before this."""
    return re.sub(r'(?<!\d)\d(?: \d){3,}(?!\d)',
                  lambda m: m.group(0).replace(' ', ''), text)

# ── Compound spoken numbers (ES + EN grammar parser) ─────────────
# "doscientos cincuenta y tres" → 253, "dos mil veinticuatro" → 2024,
# "two hundred fifty three" → 253, "eighteen hundred" → 1800.
# Deterministic with guardrails: lone units/articles ("una vez"),
# lone "mil"/"cien"/"hundred" ("mil gracias") are never touched.
_NW_UNIT = {  # value, category 'u'
    "un":1,"uno":1,"una":1,"dos":2,"tres":3,"cuatro":4,"cinco":5,
    "seis":6,"siete":7,"ocho":8,"nueve":9,
    "one":1,"two":2,"three":3,"four":4,"five":5,"six":6,"seven":7,
    "eight":8,"nine":9,
}
_NW_TEEN = {  # 10-29 single words — safe as lone words
    "diez":10,"once":11,"doce":12,"trece":13,"catorce":14,"quince":15,
    "dieciseis":16,"diecisiete":17,"dieciocho":18,"diecinueve":19,
    "veinte":20,"veintiun":21,"veintiuno":21,"veintiuna":21,"veintidos":22,
    "veintitres":23,"veinticuatro":24,"veinticinco":25,"veintiseis":26,
    "veintisiete":27,"veintiocho":28,"veintinueve":29,
    "ten":10,"eleven":11,"twelve":12,"thirteen":13,"fourteen":14,
    "fifteen":15,"sixteen":16,"seventeen":17,"eighteen":18,"nineteen":19,
}
_NW_TENS = {
    "treinta":30,"cuarenta":40,"cincuenta":50,"sesenta":60,"setenta":70,
    "ochenta":80,"noventa":90,
    "twenty":20,"thirty":30,"forty":40,"fifty":50,"sixty":60,
    "seventy":70,"eighty":80,"ninety":90,
}
_NW_HUND_ES = {  # absolute values
    "ciento":100,"doscientos":200,"doscientas":200,"trescientos":300,
    "trescientas":300,"cuatrocientos":400,"cuatrocientas":400,
    "quinientos":500,"quinientas":500,"seiscientos":600,"seiscientas":600,
    "setecientos":700,"setecientas":700,"ochocientos":800,"ochocientas":800,
    "novecientos":900,"novecientas":900,"cien":100,
}
_NW_MULT = {"mil":1000,"thousand":1000,"hundred":100,
            "millon":1000000,"millones":1000000,"million":1000000}
_NW_RISKY_ALONE = set(_NW_UNIT) | {"cien","ciento","mil"} | set(_NW_MULT)

def _deaccent(w: str) -> str:
    import unicodedata
    return ''.join(c for c in unicodedata.normalize("NFD", w)
                   if unicodedata.category(c) != "Mn")

def _lowest_place(n):
    lp, t = 1, int(n)
    while t and t % 10 == 0:
        lp *= 10; t //= 10
    return lp

def _concat_place_value(text):
    # Merge adjacent DIGIT groups that form a place-value number:
    # "200 5" -> 205, "1000 800" -> 1800, "1000 100 1" -> 1101, "20 5" -> 25.
    # A run can only START on a round number (trailing zero, >=10), so single
    # dictated digits ("9 5 4 …" for phones) never merge — they stay separate.
    toks = text.split(' ')
    out, i = [], 0
    while i < len(toks):
        core = toks[i].rstrip('.,;:!?…')
        tail = toks[i][len(core):]
        if (core.isdigit() and len(core) >= 2 and int(core) % 10 == 0
                and _lowest_place(core) > 1 and not tail):
            V = int(core); end = i; etail = ""
            j = i + 1
            while j < len(toks):
                c2 = toks[j].rstrip('.,;:!?…'); t2 = toks[j][len(c2):]
                if c2.isdigit() and 0 < int(c2) < _lowest_place(V):
                    V += int(c2); end = j; etail = t2
                    if t2: break              # stop after trailing punctuation
                    j += 1; continue
                break
            out.append(str(V) + etail); i = end + 1
        else:
            out.append(toks[i]); i += 1
    return ' '.join(out)

def _compound_numbers_to_digits(text: str) -> str:
    # Toll-free prefix first — "one eight hundred" is 1-800, not 801/900
    text = re.sub(r'(?i)\b(?:one|uno|un)[,\s\-]+(?:eight\s+hundred|ochocientos)\b',
                  "1-800", text)
    # EN hyphenated compounds: "fifty-three" → "fifty three"
    text = re.sub(r'(?i)\b(twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety)-'
                  r'(one|two|three|four|five|six|seven|eight|nine)\b',
                  r'\1 \2', text)

    tokens = text.split(' ')
    out, i = [], 0

    def ntok(tok):
        # -> (value, kind, is_word). kind: u/t/d/h/m are building blocks,
        # 'x' = a complete standalone digit group (25, 205, 1800…) that is
        # NOT a simple block. Understands both spoken WORDS and DIGITS so a
        # half-numeralized "doscientos 5" or "18 hundred" still combines.
        core = _deaccent(tok.strip('.,;:!?¿¡()"…').lower())
        for table, kind in ((_NW_UNIT,'u'), (_NW_TEEN,'t'), (_NW_TENS,'d'),
                            (_NW_HUND_ES,'h'), (_NW_MULT,'m')):
            if core in table:
                return table[core], kind, True
        if core.isdigit():
            n = int(core)
            if   1 <= n <= 9:   return n, 'u', False
            elif 10 <= n <= 19: return n, 't', False
            elif n in (20,30,40,50,60,70,80,90): return n, 'd', False
            elif n in (100,200,300,400,500,600,700,800,900): return n, 'h', False
            elif n in (1000, 1000000): return n, 'm', False
            return n, 'x', False
        return None, None, None

    def _ok_next(prev, cur):
        if prev is None: return True
        pv, pk = prev; cv, ck = cur
        if pk in ('u', 't'): return ck == 'm'
        if pk == 'd':        return ck in ('u', 'm')
        if pk == 'h':        return ck in ('u', 't', 'd', 'm')
        if pk == 'm':
            if pv == 100:    return ck in ('u', 't', 'd') or (ck == 'm' and cv >= 1000)
            return ck in ('u', 't', 'd', 'h')
        return True

    while i < len(tokens):
        v0, k0, w0 = ntok(tokens[i])
        if k0 is None:
            out.append(tokens[i]); i += 1; continue

        # Decide whether to START a compound here.
        start = False
        if w0 and k0 in ('u','t','d','h','m'):
            start = True                      # any spoken number-word may start
        elif (not w0) and k0 in ('u','t','d'):
            # a bare digit starts ONLY if a scale WORD follows ("18 hundred");
            # never a plain digit run (keeps phone dictation intact).
            if i + 1 < len(tokens):
                _, k1, w1 = ntok(tokens[i+1])
                if w1 and k1 in ('h','m'):
                    start = True
        if not start:
            out.append(tokens[i]); i += 1; continue

        seq = []; raw_end = i; j = i
        while j < len(tokens):
            vj, kj, wj = ntok(tokens[j])
            if kj in ('u','t','d','h','m'):
                prev = (seq[-1][0], seq[-1][1]) if seq else None
                if not _ok_next(prev, (vj, kj)):
                    break
                seq.append((vj, kj, wj)); raw_end = j
                if tokens[j].rstrip() != tokens[j].rstrip('.,;:!?…'):
                    j += 1; break
                j += 1; continue
            if kj == 'x':
                break                         # a complete digit group won't merge here
            cj = _deaccent(tokens[j].strip('.,;:!?¿¡()"').lower())
            if cj in ("y", "and") and seq and j + 1 < len(tokens):
                _, k3, _w = ntok(tokens[j+1])
                prev_k = seq[-1][1]
                if k3 in ('u','t','d') and (
                        (cj == "y" and prev_k == 'd') or (cj == "and")):
                    j += 1; continue
            break

        core0 = _deaccent(tokens[i].strip('.,;:!?¿¡()"…').lower())
        # lone risky WORD ("mil gracias", "una vez") stays a word
        if len(seq) == 1 and w0 and core0 in _NW_RISKY_ALONE:
            out.append(tokens[i]); i += 1; continue
        # a single digit that didn't actually combine → leave as spoken
        if len(seq) == 1 and not w0:
            out.append(tokens[i]); i += 1; continue

        total = current = 0
        for vj, kj, _ in seq:
            if kj in ('u','t','d','h'):
                current += vj
            elif kj == 'm':
                if vj == 100:
                    current = (current if current else 1) * 100
                else:
                    total += (current if current else 1) * vj
                    current = 0
        value = total + current
        if value <= 0:
            out.append(tokens[i]); i += 1; continue
        tail = tokens[raw_end][len(tokens[raw_end].rstrip('.,;:!?…')):]
        out.append(str(value) + tail)
        i = raw_end + 1

    # Second pass: stitch place-value digit groups Deepgram already split.
    return _concat_place_value(' '.join(out))

def _split_sentences(text: str) -> list:
    """Split a Deepgram final into individual sentences so each can be
    flushed separately.  Splits at .!?… followed by a space+uppercase letter
    or end-of-string.  Avoids splitting on common abbreviations (Dr., Mr.,
    Mrs., St., No.) which would produce false splits."""
    # Protect common abbreviations
    text = re.sub(r'\b(Dr|Mr|Mrs|Ms|St|No|vs|etc|approx|appt)\.\s', r'\1__DOT__ ', text)
    parts = re.split(r'(?<=[.!?…])\s+(?=[A-ZÁÉÍÓÚÑ¿¡])', text)
    result = []
    for p in parts:
        p = p.replace('__DOT__', '.').strip()
        if p:
            result.append(p)
    return result or [text]

def _format_currency(text: str) -> str:
    # Combine Spanish compound numbers: "40 y 3" → "43"
    # Skip if chained ("80 y 3 y 1" = separate amounts, leave as-is)
    text = re.sub(
        r'\b(20|30|40|50|60|70|80|90)\s+y\s+([1-9])\b(?!\s+y\s+\d)',
        lambda m: str(int(m.group(1)) + int(m.group(2))), text)
    text = re.sub(r'\$?(\d[\d,]*(?:\.\d+)?)\s+dollars?\s+and\s+(\d+)\s+cents?',
        lambda m: f"${m.group(1)}.{int(m.group(2)):02d}", text, flags=re.IGNORECASE)
    text = re.sub(r'\$?(\d[\d,]*(?:\.\d+)?)\s+dollars?(?!\s+and)',
        lambda m: f"${m.group(1)}", text, flags=re.IGNORECASE)
    text = re.sub(r'\$?(\d[\d,]*(?:\.\d+)?)\s+d[oó]lares?\s+y\s+(\d+)\s+centavos?',
        lambda m: f"${m.group(1)}.{int(m.group(2)):02d}", text, flags=re.IGNORECASE)
    text = re.sub(r'\$?(\d[\d,]*(?:\.\d+)?)\s+d[oó]lares?(?!\s+y)',
        lambda m: f"${m.group(1)}", text, flags=re.IGNORECASE)
    # "X pesos" → "$X"
    text = re.sub(r'\$?(\d[\d,]*(?:\.\d+)?)\s+pesos?',
        lambda m: f"${m.group(1)}", text, flags=re.IGNORECASE)
    return text

def _format_phone(text: str) -> str:
    """Collapse runs of space-separated single digits into phone number format.
    Deepgram outputs digit-by-digit speech as "9 5 4 8 0 6 6 1 2 0".
    Handles 7-digit local, 10-digit US, and 11-digit (+1) numbers."""
    def _reformat(m):
        digits = ''.join(c for c in m.group(0) if c.isdigit())
        n = len(digits)
        if n == 11 and digits[0] == '1':
            return f"+1 ({digits[1:4]}) {digits[4:7]}-{digits[7:]}"
        if n == 10:
            return f"({digits[:3]}) {digits[3:6]}-{digits[6:]}"
        if n == 7:
            return f"{digits[:3]}-{digits[3:]}"
        return m.group(0)
    # Toll-free: 1-8xx followed by 7 digits in any spacing/grouping
    def _tollfree(m):
        digits = re.sub(r'\D', '', m.group(2))
        return f"1-{m.group(1)}-{digits[:3]}-{digits[3:]}"
    text = re.sub(
        r'(?<!\d)1[\s\-]?(8(?:00|88|77|66|55|44|33))[\s\-]?'
        r'((?:\d[\s\-]?){6}\d)(?!\d)',
        _tollfree, text)
    # Match: one digit followed by 6–10 more (space + digit) pairs — total 7–11 digits
    return re.sub(r'(?<!\d)\d(?: \d){6,10}(?!\d)', _reformat, text)

# ── Key-fact extraction ──────────────────────────────────────────
_FACT_PATTERNS = [
    # phones: (954) 806-6120 / 1-800-555-1234 / 555-123-4567 / 806-6120
    ("📞", re.compile(r'(?:\+1 )?\(\d{3}\) \d{3}-\d{4}'
                      r'|\b1-8\d{2}-\d{3}-\d{4}\b'
                      r'|\b\d{3}-\d{3}-\d{4}\b'
                      r'|\b\d{3}-\d{4}\b')),
    ("$",  re.compile(r'\$\d[\d,]*(?:\.\d{1,2})?')),
    ("✉",  re.compile(r'\b[\w.+-]+@[\w-]+\.\w+\b')),
    # word-boundary guards: without them 'Decide 5' matched as a date
    ("📅", re.compile(r'\b(?:January|February|March|April|May|June|July|August|September|October|November|December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sept|Sep|Oct|Nov|Dec)\b\.? \d{1,2}(?:st|nd|rd|th)?\b'
                      r'|\b\d{1,2}/\d{1,2}(?:/\d{2,4})?\b', re.IGNORECASE)),
    ("#",  re.compile(r'\b\d{5,}\b')),
]

def _extract_facts(text: str):
    """Pull out the details people ask to repeat: phones, money, emails,
    dates, account/reference numbers. Returns [(icon, value)] deduped."""
    seen, out = set(), []
    for icon, pat in _FACT_PATTERNS:
        for m in pat.findall(text):
            if m not in seen:
                seen.add(m); out.append((icon, m))
    return out

# ── Debug logger ─────────────────────────────────────────────────
class Logger:
    def __init__(self, ui_q):
        self.ui_q = ui_q; self._file = None
        try:
            p = Path.home() / ".interpro" / "logs"
            p.mkdir(parents=True, exist_ok=True)
            self._file = open(p / f"log_{int(time.time())}.txt", "w", encoding="utf-8")
        except: pass

    def log(self, msg, level="INFO"):
        ts = time.strftime("%H:%M:%S")
        line = f"[{ts}] {level:5s} {msg}"
        if self._file:
            try: self._file.write(line+"\n"); self._file.flush()
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
                    "is_vbcable":is_vb,"is_loopback":is_lb,
                    "is_output_loopback":False,"backend":"sd"})
    # ── "What you hear" devices — WASAPI loopback via pyaudiowpatch ──
    # Every output device (headset, speakers) becomes selectable; capturing
    # it records everything PLAYING through it (never the microphone).
    try:
        import pyaudiowpatch as pyaudio
        p = pyaudio.PyAudio()
        for d in p.get_loopback_device_info_generator():
            name = d["name"].replace(" [Loopback]", "")
            out.append({"index":int(d["index"]),"name":name,"api":"WASAPI Loopback",
                        "sr":int(d["defaultSampleRate"]),
                        "channels":int(d["maxInputChannels"]),
                        "is_vbcable":False,"is_loopback":True,
                        "is_output_loopback":True,"backend":"wpatch"})
        p.terminate()
    except Exception:
        pass  # pyaudiowpatch not installed / not Windows — feature simply absent
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
    """Streaming polyphase resampler (numpy only): device rate -> 16 kHz.

    Replaces scipy.signal.resample_poly. Importing scipy took ~10 s on a cold
    start and froze the window on the first Start of every launch, and it
    added ~70 MB to the build. Same filter design as resample_poly
    (Kaiser-windowed sinc, beta 5, half-length 10*max(up, down)), but the
    filter state carries across chunks, so there are no seams at chunk
    edges (per-chunk resample_poly zero-padded every block boundary)."""
    def __init__(self, src, dst=TARGET_SR):
        import math
        src, dst = int(src), int(dst)
        g = math.gcd(dst, src)
        self.L, self.M = dst // g, src // g          # up, down
        self.ratio = dst / src
        if self.L == self.M:
            return
        R = max(self.L, self.M); half = 10 * R
        n = np.arange(-half, half + 1, dtype=np.float64)
        h = np.sinc(n / R) * np.kaiser(2 * half + 1, 5.0)
        h *= self.L / h.sum()                        # unity gain after zero-stuffing
        K = -(-len(h) // self.L)                     # taps per phase
        hp = np.zeros((self.L, K))
        for p in range(self.L):
            taps = h[p::self.L]; hp[p, :len(taps)] = taps
        self._hp, self._K = hp.astype(np.float32), K
        self._hist = np.zeros(K - 1, np.float32)     # last K-1 input samples
        self._n_in = 0                               # input samples consumed
        self._k = 0                                  # next output sample index

    def run(self, audio):
        if self.L == self.M:
            return audio
        x = np.concatenate([self._hist, np.asarray(audio, np.float32)])
        base = self._n_in - len(self._hist)          # stream index of x[0]
        self._n_in += len(audio)
        k_end = -(-(self._n_in * self.L) // self.M)  # outputs whose input exists
        ks = np.arange(self._k, k_end, dtype=np.int64)
        self._k = k_end
        self._hist = x[len(x) - (self._K - 1):]
        if len(ks) == 0:
            return np.zeros(0, np.float32)
        m = ks * self.M
        idx = (m // self.L - base)[:, None] - np.arange(self._K)[None, :]
        return np.einsum("ij,ij->i", x[idx], self._hp[m % self.L]).astype(np.float32)

# ── Translator ───────────────────────────────────────────────────
_CLAUDE_SYS = """\
You are an expert professional translator specializing in live spoken audio that has \
been captured and transcribed by a real-time speech-to-text API (Deepgram Nova-3). \
Your translations power a live interpreter desktop application used by professionals \
who need accurate, natural-sounding output in real time.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CORE RULES — FOLLOW THESE WITHOUT EXCEPTION
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

1. OUTPUT ONLY THE TRANSLATED TEXT.
   Never add explanations, labels, notes, alternatives in parentheses, or any \
commentary. Your entire response must be the translated sentence(s) and nothing else.

1a. YOU ARE A SILENT ENGINE — NEVER REFUSE, NEVER TALK TO THE USER. (NON-NEGOTIABLE)
   You are a machine translation component, not a chat assistant. The user NEVER \
reads a message written by you — they only ever read a translation. Therefore:
   - NEVER refuse to translate. NEVER say "I'm unable to", "I can't provide", \
"this appears to be", "please clarify", "please provide", or anything similar.
   - NEVER comment on, question, or apologize for the quality of the input.
   - NEVER ask for a corrected or clarified source. There is no one to ask.
   If the input is garbled, corrupted, nonsensical, or only partially intelligible \
(transcription errors are common and expected), do your BEST-EFFORT translation of \
whatever meaning you can recover, translating recognizable words literally. \
ONLY if the input is so completely unintelligible that not one word can be rendered, \
output a single em-dash: — (nothing else). A refusal paragraph is ALWAYS the wrong \
answer — a rough or partial translation, or a bare "—", is always correct.

2. CLEAN UP SPEECH ARTIFACTS NATURALLY.
   Spoken transcripts routinely contain disfluencies. Remove or repair:
   - Filler words: "um", "uh", "er", "like", "you know", "I mean"
   - Spanish fillers: "este", "o sea", "pues", "bueno", "o sea que"
   - French fillers: "euh", "ben", "enfin", "voilà"
   - Stutters and false starts: "I — I was going to — I wanted to say"
   - Repeated words: "the the meeting is" → "the meeting is"
   - Mid-sentence restarts: keep the last coherent version of the thought
   The cleaned translation must read as if spoken fluently by a native speaker.

3. TRANSLATE IDIOMS AND EXPRESSIONS — NEVER WORD-FOR-WORD.
   Find the natural equivalent in the target language. Examples:
   - "bajo el agua" (ES) → "under the table" or "off the books" (EN), not "under the water"
   - "it's raining cats and dogs" (EN) → "está lloviendo a cántaros" (ES)
   - "costar un ojo de la cara" (ES) → "cost an arm and a leg" (EN)
   - "matar dos pájaros de un tiro" (ES) → "kill two birds with one stone" (EN)
   - "auf den Punkt bringen" (DE) → "get to the point" (EN)

4. WATCH FOR FALSE COGNATES AND TRICKY PAIRS.
   - "actual" (ES) = current/present, NOT actual
   - "embarazada" (ES) = pregnant, NOT embarrassed
   - "éxito" (ES) = success, NOT exit
   - "sensible" (ES/FR) = sensitive, NOT sensible
   - "realizar" (ES) = to carry out/accomplish, NOT to realize (unless contextually clear)
   - "assistir" (PT) = to attend, NOT to assist
   - "préservatif" (FR) = condom, NOT preservative
   - "genial" (ES) = great/brilliant, NOT genial (friendly)
   - "compromiso" (ES) = commitment or compromise depending on context — read carefully
   - "recordar" (ES) = to remember, NOT to record

5. REGISTER AND TONE MATCHING.
   Match the formality of the source text:
   - Casual conversation → casual, contractions ok ("don't", "we're", "gonna")
   - Formal business/academic → formal, no contractions
   - Medical or legal content → precise technical vocabulary, no paraphrasing
   - Emotional speech (urgent, distressed) → preserve the emotional intensity

6. MUFFLED / LOW-CONFIDENCE AUDIO — [LOW CONFIDENCE] TAG.
   When the input begins with [LOW CONFIDENCE], the audio was partially inaudible \
or muffled. You must:
   a. Infer the most probable meaning from context
   b. Clean up garbled segments aggressively — produce fluent output even if some \
words are unclear
   c. Never output "[LOW CONFIDENCE]" in your translation
   d. If the text is completely unintelligible, return a single em-dash: —

7. NUMBERS, NAMES, AND PROPER NOUNS.
   - Keep proper names unchanged (people, places, brands, products)
   - NEVER translate phone numbers, account numbers, zip codes, or any digit sequence — \
copy them exactly as written. "(954) 806-6120" stays "(954) 806-6120". \
"$350.25" stays "$350.25". Digit sequences are NOT words, do not speak them as words.
   - Street addresses: keep the number and street name as-is, only translate generic \
words like "Street/Calle", "Avenue/Avenida" if they appear in writing.
   - Acronyms: keep source acronym if well-known internationally (CEO, NASA, FBI)

8. PUNCTUATION AND CAPITALIZATION.
   Apply standard punctuation rules for the target language. Spanish requires \
inverted question/exclamation marks at sentence start (¿, ¡). \
Capitalize sentence-initially and for proper nouns only.

9. REGIONAL SPANISH AWARENESS.
   Callers come from every Spanish-speaking region. Understand regional \
vocabulary on input, but produce NEUTRAL Latin American Spanish on output \
unless the conversation clearly establishes a region.
   - Caribbean (DR, PR, Cuba): "guagua" = bus, "chin" = a little bit, \
"colmado" = corner store, "ahorita" = in a while (NOT right now), \
"cuadre" = arrangement/deal, "chele" = cent/penny, "yipeta" = SUV
   - Mexico/Central America: "ahorita" = right now, "camión" = bus (MX), \
"pisto" = money (Guat/Hond), "chamba" = work/job, "colonia" = neighborhood
   - South America: "guita"/"plata" = money, "departamento" = apartment, \
"celular" = cell phone (universal LatAm; use it, not "móvil")
   - Voseo ("vos tenés") appears from Central America and Argentina — \
understand it, but reply in standard "usted/tú" forms.
   - Default to "usted" in business, medical, legal and customer-service \
contexts; use "tú" only when the conversation is clearly casual/familiar.

10. CALL-CENTER PHRASEBOOK — render these naturally, not literally.
   - "How may I help you today?" → "¿En qué puedo ayudarle hoy?"
   - "Can I place you on a brief hold?" → "¿Le puedo poner en espera un momento?"
   - "Thank you for your patience." → "Gracias por su paciencia."
   - "For quality purposes this call may be recorded" → \
"Esta llamada puede ser grabada por motivos de calidad"
   - "Let me transfer you to that department" → \
"Permítame transferirle a ese departamento"
   - "Can you verify your identity?" → "¿Puede verificar su identidad?"
   - "date of birth" → "fecha de nacimiento"; "billing address" → \
"dirección de facturación"; "mailing address" → "dirección postal"
   - "Is there anything else I can help you with?" → \
"¿Hay algo más en que pueda ayudarle?"
   - "Have a great rest of your day" → "Que tenga un excelente día"
   - "You're welcome" → "Con gusto" / "De nada" (match warmth of source)

11. BANKING / INSURANCE / BENEFITS TERMINOLOGY — exact standard renderings.
   - checking account = cuenta de cheques / cuenta corriente; \
savings account = cuenta de ahorros; routing number = número de ruta; \
account balance = saldo de la cuenta; statement = estado de cuenta; \
overdraft = sobregiro; wire transfer = transferencia bancaria; \
late fee = cargo por pago tardío; interest rate = tasa de interés; \
credit score = puntaje de crédito; down payment = pago inicial / enganche; \
payment plan = plan de pagos; due date = fecha de vencimiento
   - deductible = deducible; copay = copago; premium = prima; \
coverage = cobertura; claim = reclamo / reclamación; \
policy number = número de póliza; beneficiary = beneficiario; \
out-of-pocket maximum = gasto máximo de bolsillo; \
prior authorization = autorización previa; provider = proveedor
   - Social Security = Seguro Social; SSI = Seguridad de Ingreso \
Suplementario; disability benefits = beneficios por discapacidad; \
workers' compensation = compensación laboral / compensación al trabajador; \
unemployment benefits = beneficios de desempleo; food stamps / SNAP = \
cupones de alimentos / SNAP; public assistance = asistencia pública; \
child support = manutención de menores; alimony = pensión alimenticia
   - lease = contrato de arrendamiento; landlord = arrendador / dueño; \
tenant = inquilino; utilities = servicios públicos; \
eviction = desalojo; security deposit = depósito de seguridad

12. MEDICAL / LEGAL QUICK TABLE.
   - appointment = cita; referral = referido / referencia; \
prescription = receta; dosage = dosis; side effects = efectos secundarios; \
emergency room = sala de emergencias; primary care physician = \
médico de cabecera / médico primario; symptoms = síntomas; \
follow-up = seguimiento; lab work = análisis de laboratorio
   - hearing = audiencia; plaintiff = demandante; defendant = demandado; \
testimony = testimonio; subpoena = citación; settlement = acuerdo; \
attorney = abogado; court date = fecha de corte / fecha de audiencia; \
under oath = bajo juramento; case number = número de caso

13. NUMBERS, DATES AND SPELLED-OUT LETTERS.
   - Digits stay digits, exactly as written: "(954) 806-6120", "$1,250.75", \
"2004" — copy them character for character. Never expand digits to words.
   - Spoken compound numbers become digits naturally: "dos mil cuatro" → \
"2004"; "nineteen ninety" → "1990"; "cuatrocientos ochenta" → "480"; \
"doscientos cincuenta y tres" → "253"; "one eight hundred" → "1-800".
   - Dates: render clearly for the target audience — "February 19th, 2004" → \
"19 de febrero de 2004"; "el 3 de marzo" → "March 3rd".
   - Letter spell-outs must survive exactly: "B as in boy, A as in apple" → \
"B de boy, A de apple" (keep the LETTERS; the mnemonic words may adapt).
   - Alphanumeric codes ("claim number A as in apple 4 7 9") — keep every \
letter and digit in order; never merge or reorder them.
   - NEVER invent, complete, or merge digits. Translate EXACTLY the digits \
present in the input — no more, no fewer. If the caller gives a partial \
number ("468 38 95"), output that partial number verbatim. Do NOT combine \
it with phone numbers or digits from earlier context, and do NOT reformat \
partial digits into a full phone number. A wrong-but-plausible number is \
far worse than a visibly partial one.

14. WHAT NEVER GETS TRANSLATED.
   - Person names, company names, street names, product names, email \
addresses, usernames, websites — copy them verbatim.
   - Do not "helpfully" localize: "Main Street" stays "Main Street"; \
"John" never becomes "Juan".
   - If the source mixes languages ("ok, entonces mañana same time"), \
translate the whole meaning into the single target language.

15. IDIOM TABLE — natural equivalents, both directions.
   EN → ES:
   - "to make ends meet" → "llegar a fin de mes"
   - "give someone a heads up" → "avisarle con anticipación"
   - "long story short" → "en resumen" / "para no hacer el cuento largo"
   - "it slipped my mind" → "se me pasó" / "se me olvidó por completo"
   - "we're on the same page" → "estamos de acuerdo"
   - "bear with me" → "tenga paciencia un momento" / "deme un segundo"
   - "as soon as possible" → "lo antes posible"
   - "out of the blue" → "de la nada" / "de repente"
   - "cut to the chase" → "ir al grano"
   - "back and forth" → "de un lado a otro" / "idas y vueltas"
   ES → EN:
   - "estar en las nubes" → "to have one's head in the clouds"
   - "no tener pelos en la lengua" → "to not mince words"
   - "meter la pata" → "to mess up" / "to put one's foot in it"
   - "al pie de la letra" → "to the letter" / "word for word"
   - "de buenas a primeras" → "out of nowhere"
   - "hacer la vista gorda" → "to turn a blind eye"
   - "estar entre la espada y la pared" → "to be between a rock and a hard place"
   - "tomar el pelo" → "to pull someone's leg"
   - "costar trabajo" → "to be hard/difficult (for someone)"
   - "quedar en algo" → "to agree on something"

16. DISFLUENCY REPAIR — more patterns to clean silently.
   - Restart with correction: "I called on Mon— on Tuesday" → \
translate only "I called on Tuesday".
   - Trailing repetition from the transcriber: "the payment the payment \
is due Friday" → one occurrence only.
   - Abandoned clause: "Well, what I — anyway, the address is wrong" → \
"Anyway, the address is wrong."
   - Spanish self-repair: "fui el lunes, digo, el martes" → \
"I went on Tuesday" (the "digo" correction wins).
   - Echoed confirmations ("okay okay okay") → a single "Okay."
   - Interrupted sentence that resumes: keep the resumed, complete version.
   - Never translate hesitation sounds: "eh", "em", "mmm", "este" → drop.

17. APPOINTMENTS, COLLECTIONS AND FOLLOW-UPS.
   - "Would you like a reminder call?" → "¿Desea una llamada de recordatorio?"
   - "We can reschedule at no cost" → "Podemos reprogramar sin costo"
   - "Your account is past due" → "Su cuenta está vencida" / \
"Su cuenta tiene un saldo vencido"
   - "to set up a payment arrangement" → "establecer un acuerdo de pago"
   - "a settlement offer" → "una oferta de liquidación"
   - "Your services may be interrupted" → "Sus servicios podrían ser \
interrumpidos"
   - "to waive the fee" → "eximir el cargo" / "no cobrar el cargo"
   - "We'll send a confirmation by text" → "Le enviaremos una confirmación \
por mensaje de texto"
   - "walk-in" → "sin cita"; "same-day appointment" → "cita para el mismo día"
   - "callback number" → "número de devolución de llamada" / \
"número para devolverle la llamada"

18. REGISTER PAIRS — match the speaker's energy.
   - Casual EN "gotcha, no worries" → ES "listo, no se preocupe" (usted) \
or "listo, tranquilo" (tú) — pick by established register.
   - Formal ES "quedo a sus órdenes" → EN "I remain at your service" is \
too stiff; prefer "happy to help with anything else".
   - Upset caller: keep the intensity honestly — "esto es un abuso" → \
"this is outrageous", never soften to "this is inconvenient".
   - Apologies: EN "I do apologize for the inconvenience" → \
"Le pido disculpas por el inconveniente" (not the weaker "lo siento").

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
EXAMPLES OF CORRECT BEHAVIOR
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Input (ES→EN): "o sea que, este, lo que quiero decir es que el proyecto \
está — está muy avanzado ya"
Output: "What I mean is that the project is already very far along."

Input (EN→ES): "um so the the meeting is going to be, uh, pushed back to Thursday"
Output: "La reunión se va a posponer al jueves."

Input [LOW CONFIDENCE] (ES→EN): "el contr— contrato se firm— este semana"
Output: "The contract will be signed this week."

Input (EN→ES): "we need to think outside the box on this one"
Output: "Necesitamos pensar de manera creativa en esto."

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
FINAL REMINDER
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Your output is displayed directly on screen in real time. There is no human editor \
after you. Every response must be clean, natural, and ready to read aloud. \
Output ONLY the translated text."""

class Translator:
    """Claude (claude-haiku-4-5) translation for each finished bubble.
    Google Translate was removed in v43: the free endpoint is unofficial
    (against Google's terms for a paid product) and it got the user's
    connection blocked as "unusual traffic"."""
    def __init__(self):
        self._claude       = None
        self.log           = None   # set by App so API errors reach the debug panel

    def set_anthropic_key(self, key):
        self._claude = None
        key = key.strip()
        if key:
            try:
                from anthropic import Anthropic
                self._claude = Anthropic(api_key=key)
            except ImportError:
                pass

    def claude_translate(self, text: str, src: str, tgt: str, conf: float = 1.0,
                         context: list = None, domain: str = "Auto",
                         summary: str = "", glossary: dict = None):
        """Claude translation with prompt caching.
        Returns (translation, engine) where engine is 'claude' or 'none'.
        One retry: after a network blip, or a reply that was a note instead
        of a translation, try again without the conversation context (stray
        context, like a wrong "Hindi" label, is what usually confuses it)."""
        if not text.strip() or src == tgt or not self._claude: return "", "none"
        result = self._claude_translate(text, src, tgt, conf,
                                        context, domain, summary, glossary)
        if not result:
            time.sleep(0.4)
            result = self._claude_translate(text, src, tgt, conf,
                                            None, domain, "", glossary)
        return (result, "claude") if result else ("", "none")

    # Domain-specific translation hints (appended to user message, not system prompt,
    # so the system prompt stays cacheable / stable).
    _DOMAIN_HINTS = {
        "Medical":
            "Domain: Medical — STRICT MODE. Preserve all symptoms, dosages, medication names, "
            "and clinical terms exactly as spoken. Do NOT infer a diagnosis or simplify "
            "symptom descriptions. Preserve patient uncertainty ('I think I feel', 'kind of', "
            "'maybe') — a misinterpretation could affect clinical decisions.",
        "Legal":
            "Domain: Legal — STRICT MODE. Preserve exact legal phrasing and every word of "
            "testimony. Do NOT remove hesitations, repetitions, or uncertainty markers "
            "('I think', 'maybe', 'I'm not sure'). Do NOT paraphrase, summarize, or omit "
            "any word. Translate verbatim — a court record depends on it.",
        "Business":
            "Domain: Business — use formal professional register. Preserve company names, "
            "titles, financial figures, and industry-specific terms.",
        "Customer Support":
            "Domain: Customer Support — warm, clear, patient tone. "
            "Simplify technical jargon so a non-expert audience can follow.",
        "Casual":
            "Domain: Casual — relaxed, natural conversational tone. Contractions are fine.",
    }

    _CHAT_RE = re.compile(
        r"\b(translat\w*|traduc\w*|source (text|language)|the input|input text|"
        r"resubmit|re-submit|transcri\w*|clarif\w*|(provided|this) text)\b", re.I)

    @classmethod
    def _looks_like_chat(cls, src_text, out):
        """True when Claude wrote a note TO the user ("I appreciate you testing
        my system… please resubmit…") instead of a translation. A real
        translation is about as long as its source (EN/ES differ by ~1.3x at
        most); these notes are far longer AND talk about the translating task
        itself. Both must be true, so a caller genuinely saying "I can't
        provide that" is safe."""
        too_long = len(out) > 2 * len(src_text) + 60
        return too_long and bool(cls._CHAT_RE.search(out))

    def _claude_translate(self, text, src, tgt, conf,
                          context=None, domain="Auto", summary="", glossary=None):
        try:
            quality = "" if conf >= 0.78 else "[LOW CONFIDENCE] "

            # ── Hybrid memory: summary + last 5 recent ────────────
            # summary covers the full session history (generated every ~8 bubbles).
            # context holds only the last 5 exchanges — together they cost far fewer
            # tokens than sending 10+ full bubbles on every call.
            ctx_parts = []
            if summary:
                ctx_parts.append(f"Conversation summary: {summary}")
            if context:
                lines = []
                for c in context[-5:]:
                    spk = (c.get("spk_name") or
                           (f"Speaker {c['spk']}" if c.get("spk", -1) >= 0 else ""))
                    prefix = f"{spk}: " if spk else ""
                    lines.append(f"  {prefix}[{c['lang'].upper()}] {c['text']}")
                    if c.get("translation"):
                        lines.append(f"    → {c['translation']}")
                if lines:
                    ctx_parts.append("Recent exchanges:\n" + "\n".join(lines))
            ctx_block = ("\n".join(ctx_parts)
                         + "\n\n(Context above is for reference only — do NOT retranslate it.)\n\n"
                         if ctx_parts else "")

            # ── Glossary injection ────────────────────────────────
            glossary_block = ""
            if glossary:
                rules = "\n".join(f'  • "{k}": {v}' for k, v in glossary.items())
                glossary_block = ("Terminology rules — apply these without exception:\n"
                                  + rules + "\n\n")

            # ── Domain instruction ────────────────────────────────
            domain_hint = self._DOMAIN_HINTS.get(domain, "")
            domain_block = f"\n{domain_hint}" if domain_hint else ""

            user_msg = (f"{ctx_block}{glossary_block}Translate from {src} to {tgt}:"
                        f"{domain_block}\n\n{quality}{text}")

            resp = self._claude.messages.create(
                model="claude-haiku-4-5",
                max_tokens=600,
                temperature=0.1,
                system=[{"type":"text","text":_CLAUDE_SYS,
                         "cache_control":{"type":"ephemeral"}}],
                messages=[{"role":"user","content": user_msg}]
            )
            out = resp.content[0].text.strip()
            if self._looks_like_chat(text, out):
                if self.log:
                    self.log.log("Claude replied with a note instead of a translation, discarded", "WARN")
                return ""          # claude_translate() retries once, plainer
            return out
        except Exception as e:
            if self.log: self.log.log(f"Claude API error: {e}", "ERROR")
            return ""

# ── Deepgram Stream ──────────────────────────────────────────────

# ── Translation Memory ───────────────────────────────────────────
class TranslationMemory:
    """Persistent exact-match cache of Claude translations — a lightweight
    'translation memory'. Phone work repeats itself verbatim (greetings,
    legal disclosures, closing scripts): the first occurrence pays for a
    Claude call, every repeat afterwards is free and fills instantly.
    Exact-match only, so nothing wrong can be 'learned'."""

    MAX_ROWS = 5000

    def __init__(self, log=None, path=None):
        self.log = log
        self._path = path or str(Path.home() / ".interpro" / "tm.db")
        try:
            con = self._con()
            con.execute(
                "CREATE TABLE IF NOT EXISTS tm("
                "src TEXT NOT NULL, src_lang TEXT NOT NULL,"
                "tgt_lang TEXT NOT NULL, domain TEXT NOT NULL DEFAULT '',"
                "tr TEXT NOT NULL, hits INTEGER DEFAULT 0, last_used REAL,"
                "PRIMARY KEY(src, src_lang, tgt_lang, domain))")
            con.commit(); con.close()
            self.ok = True
        except Exception:
            self.ok = False

    def _con(self):
        import sqlite3
        Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        return sqlite3.connect(self._path, timeout=2)

    @staticmethod
    def _norm(text: str) -> str:
        return ' '.join(text.split()).strip().lower()

    def lookup(self, text, src, tgt, domain=""):
        if not self.ok: return None
        key = self._norm(text)
        if not key: return None
        try:
            con = self._con()
            row = con.execute(
                "SELECT tr FROM tm WHERE src=? AND src_lang=? AND tgt_lang=? AND domain=?",
                (key, src, tgt, domain)).fetchone()
            if row:
                con.execute(
                    "UPDATE tm SET hits=hits+1, last_used=? "
                    "WHERE src=? AND src_lang=? AND tgt_lang=? AND domain=?",
                    (time.time(), key, src, tgt, domain))
                con.commit()
                if self.log: self.log.log("TM hit — saved a Claude call", "INFO")
            con.close()
            return row[0] if row else None
        except Exception:
            return None

    def store(self, text, src, tgt, tr, domain=""):
        if not self.ok or not tr or tr == "—": return
        key = self._norm(text)
        if not key: return
        try:
            con = self._con()
            con.execute(
                "INSERT OR REPLACE INTO tm(src, src_lang, tgt_lang, domain, tr, hits, last_used) "
                "VALUES(?,?,?,?,?,"
                "COALESCE((SELECT hits FROM tm WHERE src=? AND src_lang=? AND tgt_lang=? AND domain=?),0),"
                "?)",
                (key, src, tgt, domain, tr, key, src, tgt, domain, time.time()))
            con.execute(
                "DELETE FROM tm WHERE rowid NOT IN ("
                "SELECT rowid FROM tm ORDER BY hits DESC, last_used DESC LIMIT ?)",
                (self.MAX_ROWS,))
            con.commit(); con.close()
        except Exception:
            pass

class SpeechGate:
    """Smart pause. Deepgram bills every second of audio it receives, so long
    stretches with NO human voice (silence, hold music, background noise) are
    held back instead of streamed. Uses the Silero VAD neural model, which is
    trained to tell voices apart from music and noise.

    Built so it can never cost a word (the crude v17 loudness gate did):
      * pauses only after IDLE_S of continuous non-speech, so ordinary pauses
        in a conversation never trigger it, and by then Deepgram has long
        since finalised the last sentence;
      * while paused it keeps the last PREROLL_S of audio and, the moment a
        voice appears, sends that first, so opening syllables still arrive;
      * the audio itself is never altered: it is sent untouched or held;
      * if Deepgram is still producing words it stays open regardless;
      * any problem (model missing, onnxruntime error) means it simply sends
        everything, exactly like before (fail-open)."""
    FRAME, CTX, SR = 512, 64, 16000
    THRESH    = 0.30    # deliberately low: when unsure, treat it as speech
    IDLE_S    = 8.0
    PREROLL_S = 1.5
    _session = None
    _load_err = None
    _lock = threading.Lock()

    @classmethod
    def preload(cls):
        """Load the model once, in the background (onnxruntime import is slow
        on weak PCs, so it must never delay pressing Start)."""
        with cls._lock:
            if cls._session is not None or cls._load_err:
                return
            try:
                import onnxruntime as ort
                base = getattr(sys, "_MEIPASS", None) or str(Path(__file__).parent)
                o = ort.SessionOptions()
                o.inter_op_num_threads = 1; o.intra_op_num_threads = 1
                o.enable_cpu_mem_arena = False; o.log_severity_level = 4
                cls._session = ort.InferenceSession(
                    str(Path(base) / "silero_vad_v6.onnx"),
                    providers=["CPUExecutionProvider"], sess_options=o)
            except Exception as e:
                cls._load_err = str(e) or type(e).__name__

    def __init__(self):
        self._h = np.zeros((1, 1, 128), np.float32)
        self._c = np.zeros((1, 1, 128), np.float32)
        self._ctx = np.zeros(self.CTX, np.float32)
        self._pend = np.zeros(0, np.float32)
        self._pre = deque(); self._pre_n = 0
        self.failed = False
        self.reset()
        self.paused_s = 0.0

    def reset(self):
        """Back to streaming (used on start and when the user re-enables it)."""
        self.open = True
        self.last_speech = time.time()
        self._paused_at = None
        self._pre.clear(); self._pre_n = 0

    def touch(self):
        """Deepgram produced words: that is speech, whatever the model says."""
        self.last_speech = time.time()

    def saved_seconds(self):
        extra = (time.time() - self._paused_at) if self._paused_at else 0.0
        return self.paused_s + extra

    def _speech_in(self, chunk):
        s = SpeechGate._session
        if s is None or self.failed:
            return True                      # fail-open: never hold audio
        buf = np.concatenate([self._pend, chunk.astype(np.float32, copy=False)])
        n = len(buf) // self.FRAME
        if n == 0:
            self._pend = buf
            return False
        frames = buf[:n * self.FRAME].reshape(n, self.FRAME)
        self._pend = buf[n * self.FRAME:]
        ctx = np.vstack([self._ctx[None, :], frames[:-1, -self.CTX:]])
        self._ctx = frames[-1, -self.CTX:].copy()
        x = np.ascontiguousarray(np.concatenate([ctx, frames], axis=1), np.float32)
        try:
            p, self._h, self._c = s.run(None, {"input": x, "h": self._h, "c": self._c})
        except Exception:
            self.failed = True
            return True
        return float(np.max(p)) >= self.THRESH

    def feed(self, chunk, pcm):
        """chunk: float32 16 kHz mono; pcm: the same audio as int16 bytes.
        Returns the pcm blobs to send right now (possibly none)."""
        now = time.time()
        if self._speech_in(chunk):
            self.last_speech = now
        if self.open:
            if now - self.last_speech <= self.IDLE_S:
                return [pcm]
            self.open = False; self._paused_at = now      # start holding
        elif self.last_speech >= self._paused_at:          # a voice is back
            out = list(self._pre); out.append(pcm)
            self._pre.clear(); self._pre_n = 0
            self.paused_s += now - self._paused_at; self._paused_at = None
            self.open = True
            return out
        self._pre.append(pcm); self._pre_n += len(pcm)
        cap = int(self.PREROLL_S * self.SR) * 2
        while self._pre_n > cap and len(self._pre) > 1:
            self._pre_n -= len(self._pre.popleft())
        return []


class DeepgramStream:
    """
    Sends raw audio to Deepgram, receives transcripts.
    Translation is NOT done here — the App translates each finished bubble
    in App._translate_and_push_bubble.
    """
    def __init__(self, api_key, ui_q, log):
        self.api_key = api_key; self.ui_q = ui_q; self.log = log
        self._ws = None; self._connected = False; self._running = False
        self._audio_q = queue.Queue(maxsize=500)
        self._src_name = "Auto Detect"
        self._allowed = None          # in-pair translation codes, e.g. {"en","es"}
        self._phrase_n = 0; self._cur_id = None
        self._bytes_sent = 0; self._msgs_recv = 0
        self._first_audio_t = None; self._first_resp_t = None
        self._open_evt = threading.Event()
        self._current_speaker = None
        self._last_lang = "en"      # last confidently-detected language
        self._url = None            # saved for auto-reconnect
        self._reconnecting = False

    def connect(self, src_lang_name="Auto Detect", keyterms=None, allowed=None):
        self._src_name = src_lang_name
        self._allowed = allowed
        self._running = True
        self._open_evt.clear(); self._connected = False
        url = build_dg_url(src_lang_name, keyterms=keyterms)
        self._url = url
        self.log.log(f"URL: {url}","WS")
        self._ws = websocket.WebSocketApp(
            url,
            header=[f"Authorization: Token {self.api_key}"],
            on_open=self._on_open, on_message=self._on_message,
            on_error=self._on_error, on_close=self._on_close,
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
        threading.Thread(target=self._sender, daemon=True, name="dg-send").start()
        self.log.log("Connected and sender started","WS")
        return True

    def _on_open(self, ws):
        self._connected = True; self._open_evt.set()
        self.log.log("WebSocket OPEN ✓","WS")
        try: self.ui_q.put_nowait(("ws_status","connected","Deepgram connected ✓"))
        except: pass

    def _on_message(self, ws, raw):
        self._msgs_recv += 1
        try: data = json.loads(raw)
        except: return
        t = data.get("type","")

        if t == "Metadata":
            self.log.log("Metadata OK","DG")

        elif t == "Results":
            if self._first_resp_t is None:
                self._first_resp_t = time.time()
                if self._first_audio_t:
                    lat = (self._first_resp_t - self._first_audio_t) * 1000
                    self.log.log(f"First transcript latency={lat:.0f}ms","DG")
                    try: self.ui_q.put_nowait(("latency", lat))
                    except: pass

            alts = data.get("channel",{}).get("alternatives",[{}])
            if not alts: return
            alt = alts[0]; text = alt.get("transcript","").strip()
            is_final = data.get("is_final", False)
            try:    conf = float(alt.get("confidence", 0.0))
            except (TypeError, ValueError): conf = 0.0
            if not text: return

            lang = self._lang(alt, data)
            words = alt.get("words", [])

            # ── Split a final into per-speaker runs ──────────────
            # A single Deepgram final can bundle the tail end of one
            # speaker's words with the start of the next speaker's
            # (fast turn-taking / short endpointing gap), causing the
            # trailing word to get mis-attributed to the new speaker.
            # When 2+ distinct speakers appear in one final, split by
            # word-level speaker boundary and reconstruct each run's
            # text from punctuated_word fields.
            # For single-speaker finals (the common case) always use
            # Deepgram's transcript field directly — it has correct
            # smart_format/punctuation and is the safe default.
            # Format BEFORE building runs — the runs tuple captures this
            # string, so formatting after would be silently discarded.
            text = _compound_numbers_to_digits(text)
            text = _words_to_digits(text)
            text = _format_currency(text)
            text = _format_phone(text)
            text = _join_digit_runs(text)

            spk_ids_in_final = [w.get("speaker") for w in words
                                 if w.get("speaker") is not None]
            unique_spks = list(dict.fromkeys(spk_ids_in_final))  # ordered, deduped

            runs = []  # list of (run_spk, run_words, run_text | None)
            if is_final and len(unique_spks) > 1:
                cur_spk, cur_words = None, []
                for w in words:
                    wspk = w.get("speaker")
                    if wspk is None: wspk = cur_spk
                    if cur_spk is None: cur_spk = wspk
                    if wspk != cur_spk:
                        runs.append((cur_spk, cur_words, None))
                        cur_spk, cur_words = wspk, []
                    cur_words.append(w)
                if cur_words:
                    runs.append((cur_spk, cur_words, None))
            else:
                dominant = (max(set(spk_ids_in_final), key=spk_ids_in_final.count)
                            if spk_ids_in_final else self._current_speaker)
                runs = [(dominant, words, text)]

            for i, (run_spk, run_words, run_text_override) in enumerate(runs):
                if run_text_override is not None:
                    run_text = run_text_override  # already processed above
                else:
                    # Multi-speaker split: reconstruct from word tokens
                    run_text = " ".join(
                        w.get("punctuated_word") or w.get("word","") for w in run_words
                    ).strip()
                    run_text = _join_digit_runs(_format_phone(_format_currency(
                        _words_to_digits(_compound_numbers_to_digits(run_text)))))
                if not run_text:
                    continue

                # ── Speaker tracking (metadata only — NO flush) ──
                # Diarization flaps constantly on compressed phone audio
                # (log showed 0↔1 flips within the same second), and each
                # flap was force-cutting paragraphs into fragments. The
                # real turn boundary in an interpreted call is the
                # LANGUAGE change, which still flushes in _para_add_text.
                if is_final and run_spk is not None:
                    if self._current_speaker is not None and run_spk != self._current_speaker:
                        self.log.log(f"Speaker {self._current_speaker}→{run_spk} (no flush)","DG")
                    self._current_speaker = run_spk

                spk = self._current_speaker if self._current_speaker is not None else -1

                # ── Phrase ID management ─────────────────────────
                if not is_final:
                    if self._cur_id is None:
                        self._phrase_n += 1; self._cur_id = f"ph_{self._phrase_n:05d}"
                    pid = self._cur_id
                elif i == 0:
                    pid = self._cur_id or f"ph_{self._phrase_n+1:05d}"
                    if self._cur_id is None: self._phrase_n += 1
                    self._cur_id = None
                else:
                    # A later run in the same final is a new speaker's
                    # utterance — never reuse the in-progress partial id.
                    self._phrase_n += 1
                    pid = f"ph_{self._phrase_n:05d}"

                # ── Word-level timestamps (finals only) ───────────
                # Use Deepgram's actual word start/end times for precise SRT/VTT.
                # Words carry offsets in seconds from audio stream start;
                # add _first_audio_t to convert to wall-clock time.
                ts_start = time.time()
                ts_end   = 0.0
                if is_final and run_words and self._first_audio_t is not None:
                    try:
                        ts_start = self._first_audio_t + run_words[0]["start"]
                        ts_end   = self._first_audio_t + run_words[-1]["end"]
                    except (KeyError, IndexError):
                        pass

                # ── No translation here — App handles it on two tracks ─
                p = Phrase(id=pid, text=run_text, translation="", lang=lang,
                           conf=conf, ts=ts_start, partial=not is_final,
                           speaker=spk, ts_end=ts_end)

                self.log.log(f"{'FINAL' if is_final else 'part'} [{lang}] spk={spk} {run_text[:60]}","DG")
                try: self.ui_q.put_nowait(("phrase", p))
                except: pass

            if is_final and data.get("speech_final"):
                # endpoint: the speaker just stopped (~0.5 s). Queued AFTER
                # the phrase, so the buffer already holds this text.
                try: self.ui_q.put_nowait(("flush_para", "endpoint"))
                except: pass

            try: self.ui_q.put_nowait(("ws_bytes", self._bytes_sent))
            except: pass

        elif t == "SpeechStarted":
            try: self.ui_q.put_nowait(("speech_start", None, None))
            except: pass

        elif t == "UtteranceEnd":
            # Deepgram declares utterance complete after utterance_end_ms of silence.
            # This is the primary flush trigger → guarantees full sentences in bubbles.
            self._cur_id = None
            try: self.ui_q.put_nowait(("flush_para", None))
            except: pass

        elif t == "Error":
            self.log.log(f"DG Error: {data}","ERROR")
            try: self.ui_q.put_nowait(("ws_status","error",str(data.get("message",""))))
            except: pass

    def _on_error(self, ws, err):
        self._connected = False; self._open_evt.set()
        msg = str(err)
        # A close frame (opcode=8, code 1000) after we pressed Stop is the
        # NORMAL goodbye handshake — not an error, don't alarm the user.
        if not self._running or "opcode=8" in msg:
            self.log.log(f"WS closed normally ({msg[:40]})","WS")
            return
        self.log.log(f"WS error: {msg}","ERROR")
        try: self.ui_q.put_nowait(("ws_status","error",msg[:80]))
        except: pass

    def _on_close(self, ws, code, msg):
        self._connected = False
        self.log.log(f"WS closed code={code}","WS")
        if self._running:
            try: self.ui_q.put_nowait(("ws_status","warn","Disconnected — reconnecting…"))
            except: pass
            threading.Thread(target=self._reconnect, daemon=True).start()

    def _reconnect(self):
        """Auto-reconnect with backoff after an unexpected disconnect.
        The sender thread survives disconnects (outer while _running loop),
        so once _connected flips back to True audio resumes automatically."""
        if self._reconnecting or not self._url: return
        self._reconnecting = True
        try:
            delay = 2.0
            for attempt in range(1, 6):
                if not self._running or self._connected: return
                time.sleep(delay)
                if not self._running or self._connected: return
                self.log.log(f"Reconnect attempt {attempt}/5","WS")
                self._open_evt.clear()
                self._ws = websocket.WebSocketApp(
                    self._url,
                    header=[f"Authorization: Token {self.api_key}"],
                    on_open=self._on_open, on_message=self._on_message,
                    on_error=self._on_error, on_close=self._on_close,
                )
                threading.Thread(
                    target=self._ws.run_forever,
                    kwargs={"skip_utf8_validation":True},
                    daemon=True, name="dg-ws-rc"
                ).start()
                if self._open_evt.wait(timeout=10.0) and self._connected:
                    self.log.log("Reconnected ✓","WS")
                    try: self.ui_q.put_nowait(("ws_status","connected","Reconnected ✓"))
                    except: pass
                    return
                delay = min(delay * 2, 15.0)
            self.log.log("Reconnect failed after 5 attempts","ERROR")
            try: self.ui_q.put_nowait(("ws_status","error","Connection lost — press Stop, then Start"))
            except: pass
        finally:
            self._reconnecting = False

    def _sender(self):
        last_ka = time.time()
        while self._running:
            while self._connected:
                try: pcm = self._audio_q.get(timeout=0.02)
                except queue.Empty: break
                try:
                    self._ws.send(pcm, websocket.ABNF.OPCODE_BINARY)
                    self._bytes_sent += len(pcm)
                    if self._first_audio_t is None:
                        self._first_audio_t = time.time()
                        self.log.log(f"First audio sent {len(pcm)}B","WS")
                except Exception as e:
                    self.log.log(f"Send error:{e}","ERROR"); self._connected = False; break
            if self._connected and time.time() - last_ka > 5.0:
                try: self._ws.send(json.dumps({"type":"KeepAlive"})); last_ka = time.time()
                except: pass
            time.sleep(0.01)

    def _lang(self, alt, data):
        if self._src_name != "Auto Detect":
            return LANGUAGES[self._src_name][1]
        def accept(code):
            """Map a Deepgram code to a translation code, but trust it ONLY
            when it belongs to the active language pair. Deepgram's detector
            rarely (but really) mislabels a clear English line as Hindi/etc.;
            outside the pair there is nowhere to translate it, so we drop the
            stray guess and keep the last good language instead."""
            tr = DG_TO_TRANS.get(code)
            if not tr:
                return None
            if self._allowed and tr not in self._allowed:
                return None
            return tr

        d = (data.get("channel",{}).get("detected_language","") or
             data.get("metadata",{}).get("detected_language",""))
        if d and d not in ("","auto","multi"):
            tr = accept(d.lower().split("-")[0])
            if tr:
                self._last_lang = tr
                return self._last_lang
        words = alt.get("words",[])
        if words:
            langs = [w.get("language","").lower().split("-")[0]
                     for w in words if w.get("language","")]
            # walk candidates by frequency; take the first that's in-pair, so a
            # few stray "hi" word-tags can't outvote the real English/Spanish.
            for cand, _n in Counter(langs).most_common():
                tr = accept(cand)
                if tr:
                    self._last_lang = tr
                    return self._last_lang
        # Detection missing or entirely out-of-pair → keep the last good
        # language (don't hard-default to "en": that mislabeled Spanish).
        return getattr(self, "_last_lang", "en")

    def send_pcm(self, pcm):
        try: self._audio_q.put_nowait(pcm)
        except queue.Full: pass

    def stop(self):
        self._running = False
        try:
            if self._ws and self._connected:
                self._ws.send(json.dumps({"type":"CloseStream"}))
                time.sleep(0.2); self._ws.close()
        except: pass

# ── Audio Capture ────────────────────────────────────────────────
class AudioCapture:
    def __init__(self, dev_idx, on_chunk, on_level, log, prefer_direct=False,
                 output_loopback=False):
        self.dev_idx = dev_idx; self.on_chunk = on_chunk
        self.on_level = on_level; self.log = log
        self.prefer_direct = prefer_direct  # True when user selected a VB-Cable/loopback input
        self.output_loopback = output_loopback  # True = capture what PLAYS on this output device
        self._stream = None; self._running = False
        self._pa = None; self._pa_stream = None   # pyaudiowpatch backend
        self._sr = None; self._rs = None; self._wave = deque(maxlen=200)
        self._bytes_cap = 0

    def start(self):
        import sounddevice as sd

        # ── "What you hear" mode: WASAPI loopback via pyaudiowpatch ──
        # Captures everything playing through the user's headset/speakers.
        # No VB-Cable, no Windows default-device switching needed.
        if self.output_loopback:
            import pyaudiowpatch as pyaudio
            self._pa = pyaudio.PyAudio()
            dev = self._pa.get_device_info_by_index(self.dev_idx)
            sr = int(dev["defaultSampleRate"])
            ch = max(1, int(dev["maxInputChannels"]))
            self._sr = sr; self._rs = Resampler(sr, TARGET_SR)

            def _pa_cb(in_data, frame_count, time_info, status):
                if not self._running:
                    return (None, pyaudio.paComplete)
                try:
                    audio = np.frombuffer(in_data, dtype=np.float32).reshape(-1, ch)
                    self._cb(audio, frame_count, time_info, None)
                except Exception:
                    pass
                return (None, pyaudio.paContinue)

            self._pa_stream = self._pa.open(
                format=pyaudio.paFloat32, channels=ch, rate=sr,
                input=True, input_device_index=self.dev_idx,
                frames_per_buffer=int(sr*CAPTURE_MS/1000),
                stream_callback=_pa_cb)
            self._running = True
            self._pa_stream.start_stream()
            self.log.log(f"What-you-hear loopback on '{dev['name']}' sr={sr} ch={ch}","AUDIO")
            return dev["name"]

        info = sd.query_devices(self.dev_idx)
        opened = False
        sr = find_working_sr(self.dev_idx, self.log)
        self._sr = sr; self._rs = Resampler(sr, TARGET_SR)
        ch = min(2, int(info["max_input_channels"]))
        block = int(sr * CAPTURE_MS / 1000)
        self.log.log(f"Opening [{self.dev_idx}] '{info['name']}' sr={sr} ch={ch} block={block}","AUDIO")
        # Honor the user's dropdown choice: a VB-Cable / loopback input device
        # captures the call audio directly — use it as selected.
        if not opened and self.prefer_direct and ch >= 1:
            try:
                self._stream = sd.InputStream(
                    device=self.dev_idx, channels=ch, samplerate=sr,
                    blocksize=block, dtype="float32", callback=self._cb, latency="low")
                self._stream.start(); opened = True
                self.log.log(f"Direct capture on selected device [{self.dev_idx}] '{info['name']}'","AUDIO")
            except Exception as e:
                self.log.log(f"Selected device failed: {e} — trying WASAPI loopback","AUDIO")
        if not opened:
            self._rs = Resampler(sr, TARGET_SR)
            self._stream = sd.InputStream(
                device=self.dev_idx, channels=ch, samplerate=sr,
                blocksize=block, dtype="float32", callback=self._cb, latency="low")
            self._stream.start()
            self.log.log("Direct input stream started","AUDIO")
        self._running = True
        return info["name"]

    def _cb(self, indata, frames, t, status):
        # Send CLEAN audio to Deepgram — no gating, no per-chunk AGC, no
        # pre-emphasis. Dropping quiet chunks deleted word onsets/tails and
        # chunk-level gain distorted the envelope, causing missed/wrong words.
        if not self._running: return
        if status: self.log.log(f"Stream status: {status}","WARN")
        mono = indata.mean(axis=1) if indata.ndim > 1 else indata[:,0]
        chunk = self._rs.run(mono.astype(np.float32))
        if len(chunk) < 1: return
        rms = float(np.sqrt(np.mean(chunk**2)))
        self._bytes_cap += len(chunk) * 2
        self._wave.append(rms)
        try: self.on_level(min(1.0, rms*12), rms > 0.003, self._bytes_cap)
        except: pass
        try: self.on_chunk(chunk)
        except: pass

    def stop(self):
        self._running = False
        if self._stream:
            try: self._stream.stop(); self._stream.close()
            except: pass
            self._stream = None
        if self._pa_stream:
            try: self._pa_stream.stop_stream(); self._pa_stream.close()
            except: pass
            self._pa_stream = None
        if self._pa:
            try: self._pa.terminate()
            except: pass
            self._pa = None

    @property
    def waveform(self): return list(self._wave)

# ═══════════════════════════════════════════════════════════════════
# App — Main UI
# ═══════════════════════════════════════════════════════════════════
class App:
    # ── Paragraph accumulation constants ─────────────────────────
    # UtteranceEnd (1 s) is the primary flush trigger.
    # The safety timer fires if UtteranceEnd is never sent
    # (e.g. very long continuous speech).
    PARA_SAFETY_SEC = 2.0   # pause flush if Deepgram's UtteranceEnd never came
    PARA_EXTEND_SEC = 3.0   # extra grace when it ends mid-thought (comma etc.)
    PARA_MAX_WORDS  = 90    # hard word cap → force flush

    def __init__(self):
        self.root = ctk.CTk()
        self.root.title("Sayvo — Professional Interpreter")
        self.root.geometry("1280x800"); self.root.minsize(1000,680)
        try: self.root.state("zoomed")
        except: pass
        self._apply_icon(self.root)

        # ── Core state ───────────────────────────────────────────
        self.recording = False; self.session_start = None
        self.committed = {}
        self.seg_count = 0; self.word_count = 0; self._last_meter = 0.0

        # Paragraph accumulation buffer (only text + metadata, no translation)
        self._para_text    = ""
        self._para_lang    = "en"
        self._para_conf    = 0.0
        self._para_ts      = None
        self._para_timer   = None
        self._para_speaker = -1
        self._para_lock    = threading.Lock()
        self._pending_dev  = None

        self._current_partial = ""  # latest in-flight partial text (for manual flush)
        self._gate = None           # SpeechGate while recording (smart pause)

        # Silence watchdog (warns when the audio source goes dead mid-call)
        self._last_audio_t = 0.0
        self._audio_warn   = False

        # Privacy pause — audio muted while True (card numbers, SSNs…)
        self._privacy = False

        # Two-stage safety timer state (extended grace for mid-thought pauses)
        self._safety_extended = False

        # Bubble sequence counter (thread-safe via _para_lock)
        self._bubble_count = 0

        # Smart-boundary grace timer (500 ms extension when no trailing punctuation)
        self._grace_timer = None

        # Rolling context window — last 5 recent exchanges (hybrid memory)
        self._context_window = deque(maxlen=5)

        # Periodic conversation summary (generated every ~8 bubbles)
        self._conversation_summary = ""
        self._summarizing          = False

        # Speaker name registry {speaker_id: custom_name}
        self._speaker_names = {}

        # Custom glossary {term: rule} — persisted to disk
        self._glossary = self._load_glossary()

        # Auto-save session file (crash recovery)
        self._session_file = None

        # ── Services ─────────────────────────────────────────────
        self.ui_q       = queue.Queue(maxsize=1000)
        self.log        = Logger(self.ui_q)
        self.translator = Translator()
        self.translator.log = self.log
        self.tm         = TranslationMemory(self.log)
        self.devices    = []; self._dg = None; self._cap = None

        self.log.log(f"Sayvo v{__version__} starting","INFO")
        self.log.log(f"URL preview: {build_dg_url('Auto Detect')[:100]}","INFO")

        # ── Tk variables (restored from saved settings) ──────────
        self._settings = self._load_settings()
        self.api_key_var      = tk.StringVar(value=self._load_key())
        self.anthropic_key_var= tk.StringVar(value=self._load_key("anthropic.key"))
        if self.anthropic_key_var.get():
            self.translator.set_anthropic_key(self.anthropic_key_var.get())
        self.src_lang_var = tk.StringVar(value="Auto Detect")
        self.lang_a_var   = tk.StringVar(value=self._settings.get("lang_a", "English"))
        self.lang_b_var   = tk.StringVar(value=self._settings.get("lang_b", "Spanish"))
        self.dev_var      = tk.StringVar()
        self.domain_var   = tk.StringVar(value=self._settings.get("domain", "Auto"))
        self.show_tr      = tk.BooleanVar(value=self._settings.get("show_tr", True))
        self.autoscroll   = tk.BooleanVar(value=self._settings.get("autoscroll", True))
        self.show_debug   = tk.BooleanVar(value=False)
        self.textsize_var = tk.StringVar(value=self._settings.get("textsize", "Normal"))
        self._fs = {"Normal": 0, "Large": 2, "Extra Large": 5}.get(self.textsize_var.get(), 0)
        # Split view (two columns) is a USER toggle, not auto-from-width —
        # auto-flipping on resize caused a resize->rebuild->resize freeze.
        self._twocol     = bool(self._settings.get("twocol", True))
        self.twocol_var  = tk.BooleanVar(value=self._twocol)
        self._smart_pause    = bool(self._settings.get("smart_pause", True))
        self.smart_pause_var = tk.BooleanVar(value=self._smart_pause)
        threading.Thread(target=SpeechGate.preload, daemon=True).start()
        self._narrow     = False   # render stacked when genuinely too narrow
        self._rebuilding = False   # hard reentrancy guard against rebuild loops

        self._build()
        self._load_devices()
        self._poll()

        # Crash protection: log every unhandled exception to disk and keep
        # the app alive — a stray error must never kill a live call.
        self._crash_dialog_shown = False
        self.root.report_callback_exception = self._on_crash
        sys.excepthook = lambda et, ev, tb: self._on_crash(et, ev, tb)

        # Global hotkey: Ctrl+Shift+F flushes the live buffer even when the
        # app is not the focused window (useful mid-call).
        try:
            import keyboard
            keyboard.add_hotkey("ctrl+shift+f",
                lambda: self.root.after(0, self._manual_flush))
            self.log.log("Global hotkey Ctrl+Shift+F active","INFO")
        except Exception as e:
            self.log.log(f"Global hotkey unavailable: {e}","WARN")

        # Restore collapsed sidebar from last session
        if self._settings.get("sidebar_hidden"):
            self._toggle_sidebar()

        # First-run setup wizard (reappears until completed or skipped forever)
        if not self._settings.get("wizard_done"):
            self.root.after(400, self._show_wizard)

        # In-app hotkeys (work whenever the Sayvo window is focused)
        self.root.bind("<Control-p>", lambda e: self._toggle_privacy())
        self.root.bind("<Control-o>", lambda e: self._toggle_overlay())
        self.root.bind("<Control-f>", lambda e: self.search_entry.focus_set())

        self.root.protocol("WM_DELETE_WINDOW", self._quit)
        self.root.mainloop()

    # ── Language pair helpers ────────────────────────────────────
    def _pair_codes(self):
        """Translation codes for the user's Language A / Language B pair."""
        a = LANGUAGES.get(self.lang_a_var.get(), ("en-US", "en"))[1]
        b = LANGUAGES.get(self.lang_b_var.get(), ("es", "es"))[1]
        return a, b

    def _target_for(self, src):
        """Which language to translate INTO for a detected source language."""
        a, b = self._pair_codes()
        if src == a: return b
        if src == b: return a
        return a   # unexpected language → translate into the user's Language A

    def _on_lang_pair(self, *_):
        if self.lang_a_var.get() == self.lang_b_var.get():
            self._det_lbl.configure(
                text="⚠ Pick two DIFFERENT languages — translation is off",
                text_color=YELLOW)
            return
        self._det_lbl.configure(
            text=f"Speak in {self.lang_a_var.get()} or {self.lang_b_var.get()}",
            text_color=DIM)
        self._save_settings()
        self._rebuild()   # recolor existing bubbles for the new pair

    # ── Window icon ──────────────────────────────────────────────
    @staticmethod
    def _icon_path():
        base = getattr(sys, "_MEIPASS", None) or str(Path(__file__).parent)
        p = Path(base) / "sayvo_icon.ico"
        return str(p) if p.exists() else None

    def _apply_icon(self, win):
        """Set the Sayvo icon on a window. CTkToplevel resets the icon
        ~200ms after creation, so apply again after a delay."""
        p = self._icon_path()
        if not p: return
        def _set():
            try: win.iconbitmap(p)
            except Exception: pass
        _set()
        try: win.after(300, _set)
        except Exception: pass

    # ── Key helpers ──────────────────────────────────────────────
    def _keypath(self, name="deepgram.key"): return Path.home()/".interpro"/name
    def _load_key(self, name="deepgram.key"):
        try: return self._keypath(name).read_text().strip()
        except: return ""
    def _save_key_file(self, k, name="deepgram.key"):
        p = self._keypath(name); p.parent.mkdir(parents=True, exist_ok=True); p.write_text(k.strip())

    # ── Settings persistence ─────────────────────────────────────
    def _settings_path(self): return Path.home()/".interpro"/"settings.json"
    def _load_settings(self) -> dict:
        try: return json.loads(self._settings_path().read_text(encoding="utf-8"))
        except: return {}
    def _save_settings(self, *_):
        """Persist UI preferences. Accepts (and ignores) widget-command args."""
        try:
            i = self.dev_combo.current()
            dev_name = (self.devices[i]["name"]
                        if 0 <= i < len(self.devices) else "")
            self._settings.update({
                "device_name": dev_name,
                "lang_a":      self.lang_a_var.get(),
                "lang_b":      self.lang_b_var.get(),
                "textsize":    self.textsize_var.get(),
                "domain":      self.domain_var.get(),
                "show_tr":     bool(self.show_tr.get()),
                "autoscroll":  bool(self.autoscroll.get()),
            })
            p = self._settings_path()
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(self._settings, indent=2), encoding="utf-8")
        except Exception:
            pass

    # ── Glossary helpers ─────────────────────────────────────────
    def _glossary_path(self): return Path.home()/".interpro"/"glossary.json"
    def _load_glossary(self) -> dict:
        try: return json.loads(self._glossary_path().read_text(encoding="utf-8"))
        except: return {}
    def _save_glossary(self):
        p = self._glossary_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(self._glossary, indent=2, ensure_ascii=False),
                     encoding="utf-8")

    # ══════════════════════════════════════════════════════════════
    # UI Build
    # ══════════════════════════════════════════════════════════════
    def _build(self):
        self.root.title("Sayvo")

        # ── Header ──────────────────────────────────────────────
        hdr = ctk.CTkFrame(self.root, fg_color=BG2, height=54, corner_radius=0)
        hdr.pack(fill="x"); hdr.pack_propagate(False)
        tk.Frame(self.root, bg=BORDER, height=1).pack(fill="x")

        ctk.CTkButton(hdr, text="☰", width=34, height=28,
            fg_color="transparent", hover_color=BG3, text_color=DIM,
            font=(F,13), corner_radius=8, command=self._toggle_sidebar
        ).pack(side="left", padx=(12,0))
        ctk.CTkLabel(hdr, text="Sayvo", fg_color="transparent",
            text_color=TEXT, font=(F,15,"bold")).pack(side="left", padx=(8,0))
        badge = ctk.CTkFrame(hdr, fg_color=ACCENT, corner_radius=4)
        badge.pack(side="left", padx=9, pady=17)
        ctk.CTkLabel(badge, text="PRO", fg_color="transparent",
            text_color="white", font=(F,7,"bold")).pack(padx=7, pady=2)
        self.eng_lbl = ctk.CTkLabel(hdr, text="", fg_color="transparent",
            text_color=DIM, font=(F,9)); self.eng_lbl.pack(side="left", padx=10)
        # Mini record button — Start/Stop stays reachable with sidebar hidden
        self.hdr_rec_btn = ctk.CTkButton(hdr, text="⏺", width=36, height=30,
            fg_color=ACCENT, hover_color=ACCENT_HOV, text_color="white",
            font=(F,12,"bold"), corner_radius=8, command=self._toggle)
        self.hdr_rec_btn.pack(side="left", padx=(2,0))
        ctk.CTkButton(hdr, text="🗑", width=36, height=30,
            fg_color=BG3, hover_color=BG4, text_color=DIM,
            font=(F,11), corner_radius=8, command=self._clear
        ).pack(side="left", padx=(8,0))

        ctk.CTkCheckBox(hdr, text="Debug", variable=self.show_debug,
            fg_color=ACCENT, hover_color=BG4, text_color=DIM, font=(F,9),
            command=self._toggle_debug, corner_radius=4, border_color=BORDER,
            checkmark_color="white", checkbox_width=16, checkbox_height=16
        ).pack(side="right", padx=(0,20))
        ctk.CTkButton(hdr, text="⚙", width=38, height=30,
            fg_color=BG3, hover_color=BG4, text_color=TEXT, font=(F,12),
            corner_radius=8, command=self._open_settings
        ).pack(side="right", padx=(0,10))
        ctk.CTkButton(hdr, text="📝  Notes", width=84, height=30,
            fg_color=BG3, hover_color=BG4, text_color=TEXT, font=(F,9),
            corner_radius=8, command=self._toggle_notes
        ).pack(side="right", padx=(0,10))
        ctk.CTkButton(hdr, text="📌  Facts", width=84, height=30,
            fg_color=BG3, hover_color=BG4, text_color=TEXT, font=(F,9),
            corner_radius=8, command=self._toggle_facts
        ).pack(side="right", padx=(0,10))
        ctk.CTkButton(hdr, text="🗖  Overlay", width=90, height=30,
            fg_color=BG3, hover_color=BG4, text_color=TEXT, font=(F,9),
            corner_radius=8, command=self._toggle_overlay
        ).pack(side="right", padx=(0,10))
        pill = ctk.CTkFrame(hdr, fg_color=BG3, corner_radius=999,
                            border_width=1, border_color=BORDER)
        pill.pack(side="right", padx=6, pady=12)
        self.hdr_dot = ctk.CTkLabel(pill, text="●", fg_color="transparent",
            text_color=DIM, font=(F,9))
        self.hdr_dot.pack(side="left", padx=(12,4), pady=3)
        self.hdr_status = ctk.CTkLabel(pill, text="Ready", fg_color="transparent",
            text_color=DIM, font=(F,9))
        self.hdr_status.pack(side="left", padx=(0,12), pady=3)

        # ── Body ────────────────────────────────────────────────
        body = ctk.CTkFrame(self.root, fg_color=BG, corner_radius=0)
        body.pack(fill="both", expand=True)

        self.sb_frame = ctk.CTkFrame(body, fg_color=BG2, width=264, corner_radius=0)
        self.sb_frame.pack(side="left", fill="y"); self.sb_frame.pack_propagate(False)
        self._sb_border = tk.Frame(body, bg=BORDER, width=1)
        self._sb_border.pack(side="left", fill="y")
        self._build_sb(self.sb_frame)

        self.mid_frame = ctk.CTkFrame(body, fg_color=BG, corner_radius=0)
        self.mid_frame.pack(side="left", fill="both", expand=True)
        self._build_mid(self.mid_frame)

        self.dbg_frame = ctk.CTkFrame(body, fg_color=BG2, width=340, corner_radius=0)
        self.dbg_frame.pack_propagate(False)
        self._build_dbg(self.dbg_frame)

        self._facts = {}          # value -> icon (insertion-ordered)
        self._facts_visible = False
        self.facts_frame = ctk.CTkFrame(body, fg_color=BG2, width=260, corner_radius=0)
        self.facts_frame.pack_propagate(False)
        self._build_facts(self.facts_frame)

        self._notes_visible = False
        self.notes_frame = ctk.CTkFrame(body, fg_color=BG2, width=290, corner_radius=0)
        self.notes_frame.pack_propagate(False)
        self._build_notes(self.notes_frame)
        if self.show_debug.get():
            self.dbg_frame.pack(side="right", fill="y")

    def _build_sb(self, sb):
        px = dict(padx=18)

        # ── Start / Stop button (fixed at top) ────────────────────
        rw = ctk.CTkFrame(sb, fg_color="transparent")
        rw.pack(fill="x", pady=(14,0), **px)
        self.rec_btn = ctk.CTkButton(rw, text="⏺  Start Listening",
            fg_color=ACCENT, hover_color=ACCENT_HOV, text_color="white",
            font=(F,12,"bold"), height=52, corner_radius=12, command=self._toggle)
        self.rec_btn.pack(fill="x")
        self.rec_lbl = ctk.CTkLabel(rw, text="Ready", fg_color="transparent",
            text_color=DIM, font=(F,8)); self.rec_lbl.pack(pady=(6,0))

        # ── Bottom controls (pack BEFORE scroll area so they're always visible) ──
        bot = ctk.CTkFrame(sb, fg_color="transparent")
        bot.pack(side="bottom", fill="x", padx=18, pady=10)
        opts = ctk.CTkFrame(bot, fg_color="transparent"); opts.pack(fill="x", pady=(0,8))
        ctk.CTkCheckBox(opts, text="Show translation", variable=self.show_tr,
            fg_color=ACCENT, hover_color=BG4, text_color=DIM, font=(F,9),
            command=lambda: (self._rebuild(), self._save_settings()),
            corner_radius=4, border_color=BORDER,
            checkmark_color="white", checkbox_width=16, checkbox_height=16
        ).pack(anchor="w", pady=2)
        ctk.CTkCheckBox(opts, text="Auto-scroll", variable=self.autoscroll,
            fg_color=ACCENT, hover_color=BG4, text_color=DIM, font=(F,9),
            command=self._save_settings,
            corner_radius=4, border_color=BORDER, checkmark_color="white",
            checkbox_width=16, checkbox_height=16
        ).pack(anchor="w", pady=2)
        ctk.CTkCheckBox(opts, text="Split view (side-by-side)", variable=self.twocol_var,
            fg_color=ACCENT, hover_color=BG4, text_color=DIM, font=(F,9),
            command=self._toggle_view,
            corner_radius=4, border_color=BORDER, checkmark_color="white",
            checkbox_width=16, checkbox_height=16
        ).pack(anchor="w", pady=2)
        ctk.CTkCheckBox(opts, text="Pause billing on silence / hold music",
            variable=self.smart_pause_var,
            fg_color=ACCENT, hover_color=BG4, text_color=DIM, font=(F,9),
            command=self._toggle_smart_pause,
            corner_radius=4, border_color=BORDER, checkmark_color="white",
            checkbox_width=16, checkbox_height=16
        ).pack(anchor="w", pady=2)
        ctk.CTkButton(bot, text="📄  Call Summary", fg_color=BG3,
            hover_color=BG4, text_color=GREEN, font=(F,10), height=38,
            corner_radius=10, anchor="w",
            command=self._call_summary).pack(fill="x", pady=(0,6))
        ctk.CTkButton(bot, text="📂  Session History", fg_color=BG3,
            hover_color=BG4, text_color=TEXT, font=(F,10), height=38,
            corner_radius=10, anchor="w",
            command=self._open_sessions).pack(fill="x", pady=(0,6))
        ctk.CTkButton(bot, text="📖  Glossary", fg_color=BG3,
            hover_color=BG4, text_color=PURPLE, font=(F,10), height=38,
            corner_radius=10, anchor="w",
            command=self._open_glossary).pack(fill="x", pady=(0,6))
        ctk.CTkButton(bot, text="⬇  Export Transcript", fg_color=BG3,
            hover_color=BG4, text_color=TEXT, font=(F,10), height=38,
            corner_radius=10, anchor="w",
            command=self._export).pack(fill="x", pady=(0,6))
        ctk.CTkButton(bot, text="🗑  Clear", fg_color="transparent",
            hover_color=BG3, text_color=DIM, font=(F,10), height=38,
            corner_radius=10, anchor="w",
            command=self._clear).pack(fill="x")

        # ── Scrollable area for all settings ──────────────────────
        sc = ctk.CTkScrollableFrame(sb, fg_color="transparent",
            scrollbar_button_color=BG3, scrollbar_button_hover_color=BG4)
        sc.pack(fill="both", expand=True)

        self._div(sc)

        # API keys moved to the ⚙ Settings dialog — touched once, not daily.

        # ── Device ────────────────────────────────────────────────
        self._sec(sc, "AUDIO SOURCE")
        dw = ctk.CTkFrame(sc, fg_color="transparent"); dw.pack(fill="x", **px)
        style = ttk.Style(); style.theme_use("clam")
        style.configure("IP.TCombobox", fieldbackground=BG3, background=BG3,
            foreground=TEXT, bordercolor=BORDER, arrowcolor=DIM,
            selectbackground=BG4, selectforeground=TEXT, padding=(8,6))
        style.map("IP.TCombobox",
            fieldbackground=[("readonly",BG3)], foreground=[("readonly",TEXT)])
        self.dev_combo = ttk.Combobox(dw, textvariable=self.dev_var, state="readonly",
            style="IP.TCombobox", font=(F,9))
        self.dev_combo.pack(fill="x")
        self.dev_combo.bind("<<ComboboxSelected>>",
            lambda e: (self.dev_combo.selection_clear(), self._on_dev()))
        self.dev_lbl = ctk.CTkLabel(dw, text="Scanning…", fg_color="transparent",
            text_color=GREEN, font=(F,8), wraplength=212, anchor="w", justify="left")
        self.dev_lbl.pack(anchor="w", pady=(5,0))
        self._div(sc)

        # ── Language pair ─────────────────────────────────────────
        self._sec(sc, "LANGUAGE PAIR")
        lm = ctk.CTkFrame(sc, fg_color="transparent"); lm.pack(fill="x", **px)
        self.lang_a_menu = ctk.CTkOptionMenu(lm, variable=self.lang_a_var,
            values=LANG_NAMES_T, command=self._on_lang_pair,
            fg_color=BG3, button_color=BG4, button_hover_color=BG3,
            text_color=ACCENT, font=(F,10,"bold"), corner_radius=8,
            dynamic_resizing=False)
        self.lang_a_menu.pack(fill="x")
        ctk.CTkLabel(lm, text="↕", fg_color="transparent",
            text_color=DIM, font=(F,11,"bold")).pack(pady=1)
        self.lang_b_menu = ctk.CTkOptionMenu(lm, variable=self.lang_b_var,
            values=LANG_NAMES_T, command=self._on_lang_pair,
            fg_color=BG3, button_color=BG4, button_hover_color=BG3,
            text_color=PURPLE, font=(F,10,"bold"), corner_radius=8,
            dynamic_resizing=False)
        self.lang_b_menu.pack(fill="x")
        self._det_lbl = ctk.CTkLabel(lm,
            text=f"Speak in {self.lang_a_var.get()} or {self.lang_b_var.get()}",
            fg_color="transparent", text_color=DIM, font=(F,8),
            wraplength=200, anchor="w", justify="left")
        self._det_lbl.pack(anchor="w", pady=(5,0))
        self._div(sc)

        # ── Domain mode ───────────────────────────────────────────
        self._sec(sc, "DOMAIN MODE")
        dmd = ctk.CTkFrame(sc, fg_color="transparent"); dmd.pack(fill="x", **px)
        self.domain_menu = ctk.CTkOptionMenu(
            dmd, variable=self.domain_var, command=self._save_settings,
            values=["Auto","Medical","Legal","Business","Customer Support","Casual"],
            fg_color=BG3, button_color=BG4, button_hover_color=BG3,
            text_color=TEXT, font=(F,9), corner_radius=8, dynamic_resizing=False)
        self.domain_menu.pack(fill="x")
        ctk.CTkLabel(dmd, text="Shapes Claude's translation vocabulary & tone",
            fg_color="transparent", text_color=DIM, font=(F,8)).pack(anchor="w", pady=(5,0))
        self._div(sc)

        # ── Text size ─────────────────────────────────────────────
        self._sec(sc, "TEXT SIZE")
        tsd = ctk.CTkFrame(sc, fg_color="transparent"); tsd.pack(fill="x", **px)
        ctk.CTkOptionMenu(tsd, variable=self.textsize_var,
            values=["Normal", "Large", "Extra Large"],
            command=self._on_textsize,
            fg_color=BG3, button_color=BG4, button_hover_color=BG3,
            text_color=TEXT, font=(F,9), corner_radius=8,
            dynamic_resizing=False).pack(fill="x")
        self._div(sc)

        # ── Input level ───────────────────────────────────────────
        self._sec(sc, "INPUT LEVEL")
        mw = ctk.CTkFrame(sc, fg_color="transparent"); mw.pack(fill="x", **px)
        self.meter = ctk.CTkProgressBar(mw, height=7, corner_radius=4,
            fg_color=BG4, progress_color=GREEN)
        self.meter.set(0); self.meter.pack(fill="x")
        self.meter_lbl = ctk.CTkLabel(mw, text="Standby", fg_color="transparent",
            text_color=DIM, font=(F,8)); self.meter_lbl.pack(anchor="w", pady=(5,0))
        self.bytes_lbl = ctk.CTkLabel(mw, text="", fg_color="transparent",
            text_color=DIM, font=(F,8)); self.bytes_lbl.pack(anchor="w")
        self._div(sc)

        # ── Session stats ─────────────────────────────────────────
        self._sec(sc, "SESSION")
        sw = ctk.CTkFrame(sc, fg_color="transparent"); sw.pack(fill="x", **px)
        sw.columnconfigure(0,weight=1); sw.columnconfigure(1,weight=1)
        self.sv_time  = self._stat(sw, "0:00", "Duration",   0, 0, size=15)
        self.sv_words = self._stat(sw, "0",    "Words",      0, 1, size=15)
        self.sv_segs  = self._stat(sw, "0",    "Paragraphs", 1, 0, size=15)
        self.sv_lang  = self._stat(sw, "—",    "Language",   1, 1, size=15)
        self.sv_month = self._stat(sw, "0.0h", "This month", 2, 0, size=15)
        self.sv_cost  = self._stat(sw, "~$0.00", "API cost/mo", 2, 1, size=15)
        self.root.after(600, self._usage_refresh)

        # pipe_lbl kept as a hidden label so all status-update calls still work;
        # status text is shown in the header (hdr_status) which is always visible.
        self.pipe_lbl = ctk.CTkLabel(sc, text="", fg_color="transparent",
            text_color=DIM, font=(F,9))   # not packed — intentionally invisible

    def _build_mid(self, mid):
        # ── LIVE zone (top) — floating rounded card ──────────────
        live = ctk.CTkFrame(mid, fg_color=BG3, corner_radius=18,
                            border_width=1, border_color=BORDER)
        live.pack(fill="x", side="top", padx=16, pady=(14, 8))

        # ── Footer: confidence bar (bottom, packed before feed) ──
        tk.Frame(mid, bg="#141a2b", height=1).pack(side="bottom", fill="x")
        cs = ctk.CTkFrame(mid, fg_color=BG, corner_radius=0, height=32)
        cs.pack(side="bottom", fill="x"); cs.pack_propagate(False)

        # ── Transcript search ─────────────────────────────────────
        srch = ctk.CTkFrame(mid, fg_color="transparent")
        srch.pack(fill="x", side="top", padx=14, pady=(2,0))
        # NOTE: no textvariable — CTkEntry hides placeholder_text when one
        # is attached, which made this render as an unlabeled mystery bar.
        self.search_entry = ctk.CTkEntry(srch,
            placeholder_text="🔍  Search transcript…",
            fg_color=BG2, text_color=TEXT, border_width=1,
            border_color=BORDER, font=(F,10),
            corner_radius=10, height=34)
        self.search_entry.pack(side="left", fill="x", expand=True)
        self._search_job = None
        self.search_entry.bind("<KeyRelease>", self._on_search_key)
        def _clear_search():
            self.search_entry.delete(0, "end")
            self._rebuild()
        ctk.CTkButton(srch, text="✕", width=34, height=34,
            fg_color=BG2, hover_color=BG4, text_color=DIM, font=(F,10),
            corner_radius=10, command=_clear_search).pack(side="left", padx=(8,0))

        # ── Bubble feed (expanding center) ───────────────────────
        self.feed = ctk.CTkScrollableFrame(mid, fg_color=BG, corner_radius=0,
            scrollbar_button_color=BG3, scrollbar_button_hover_color=BG4)
        self.feed.pack(fill="both", expand=True, pady=(6,0))
        self._empty_lbl = None
        self.root.after(200, self._update_empty_state)

        # ── Responsive text wrapping ──────────────────────────────
        # Bubbles/live text re-wrap to the window width (split-screen!)
        self._wrap = 850
        self._col_wrap = 380
        self._resize_job = None
        self.root.bind("<Configure>", self._on_window_resize)
        self.root.after(900, self._apply_wrap)

        # Column-header strip (shown only in two-column mode)
        self.col_hdr = ctk.CTkFrame(mid, fg_color="transparent")
        ch = ctk.CTkFrame(self.col_hdr, fg_color="transparent")
        ch.pack(fill="x")
        ch.columnconfigure(0, weight=1, uniform="ch")
        ch.columnconfigure(1, weight=1, uniform="ch")
        ctk.CTkLabel(ch, text="ORIGINAL", fg_color="transparent",
            text_color=DIM, font=(F,8,"bold")).grid(row=0, column=0, sticky="w")
        ctk.CTkLabel(ch, text="TRANSLATION", fg_color="transparent",
            text_color=ACCENT, font=(F,8,"bold")).grid(row=0, column=1, sticky="w")

        # ── Smooth scrolling engine ───────────────────────────────
        # Disable CTk's chunky per-notch jumps on the feed canvas and
        # replace them with eased animation + middle-click autoscroll.
        self._scroll_target = None
        self._scroll_job    = None
        self._pan_active    = False
        self._pan_anchor    = 0
        self.feed._parent_canvas.yview_scroll = lambda *a, **k: None
        self.root.bind_all("<MouseWheel>",      self._feed_wheel,     add="+")
        self.root.bind_all("<ButtonPress-2>",   self._feed_pan_start, add="+")
        self.root.bind_all("<ButtonRelease-2>", self._feed_pan_stop,  add="+")

        # ── LIVE zone content ─────────────────────────────────────
        top_row = ctk.CTkFrame(live, fg_color="transparent")
        top_row.pack(fill="x", padx=24, pady=(14,0))
        self.live_dot = ctk.CTkLabel(top_row, text="●", fg_color="transparent",
            text_color=DIM, font=(F,9)); self.live_dot.pack(side="left")
        ctk.CTkLabel(top_row, text="  LIVE", fg_color="transparent",
            text_color=DIM, font=(F,9,"bold")).pack(side="left")
        self.lat_lbl = ctk.CTkLabel(top_row, text="", fg_color="transparent",
            text_color=DIM, font=(FM,8)); self.lat_lbl.pack(side="right")
        ctk.CTkButton(top_row, text="⬇  Flush", width=78, height=26,
            fg_color=ACCENT, hover_color=ACCENT_HOV, text_color="white",
            font=(F,9,"bold"), corner_radius=8,
            command=self._manual_flush).pack(side="right", padx=(0,12))
        self.privacy_btn = ctk.CTkButton(top_row, text="⏸  Privacy", width=84,
            height=26, fg_color=BG4, hover_color=BG3, text_color=DIM,
            font=(F,9,"bold"), corner_radius=8,
            command=self._toggle_privacy)
        self.privacy_btn.pack(side="right", padx=(0,8))

        # Confirmed buffer — finals locked in (white)
        self.live_confirmed = ctk.CTkLabel(live, text="",
            fg_color="transparent", text_color=TEXT, font=(F,12),
            anchor="w", justify="left", wraplength=900)
        self.live_confirmed.pack(fill="x", padx=24, pady=(8,0))

        # Current partial — words Deepgram is still deciding (gray + cursor)
        self.live_partial = ctk.CTkLabel(live, text="Waiting for speech…",
            fg_color="transparent", text_color=DIM, font=(F,12),
            anchor="w", justify="left", wraplength=900)
        self.live_partial.pack(fill="x", padx=24, pady=(0,18))

        # ── Footer content ─────────────────────────────────────────
        ctk.CTkLabel(cs, text="Confidence", fg_color="transparent",
            text_color=DIM, font=(F,8)).pack(side="left", padx=(22,8))
        self.conf_bar = ctk.CTkProgressBar(cs, width=100, height=3,
            corner_radius=1, fg_color=BG4, progress_color=ACCENT)
        self.conf_bar.set(0); self.conf_bar.pack(side="left")
        self.conf_lbl = ctk.CTkLabel(cs, text="—", fg_color="transparent",
            text_color=DIM, font=(F,8)); self.conf_lbl.pack(side="left", padx=6)

    def _build_facts(self, frm):
        tk.Frame(frm, bg=BORDER, width=1).pack(side="left", fill="y")
        inner = ctk.CTkFrame(frm, fg_color=BG2, corner_radius=0)
        inner.pack(side="left", fill="both", expand=True)
        top = ctk.CTkFrame(inner, fg_color="transparent")
        top.pack(fill="x", padx=14, pady=(16,8))
        ctk.CTkLabel(top, text="📌 Key Facts", fg_color="transparent",
            text_color=TEXT, font=(F,12,"bold")).pack(side="left")
        ctk.CTkButton(top, text="Clear", width=48, height=24,
            fg_color="transparent", hover_color=BG3, text_color=DIM,
            font=(F,8), corner_radius=6,
            command=self._clear_facts).pack(side="right")
        ctk.CTkLabel(inner,
            text="Numbers, amounts, emails & dates\nfrom the call — click to copy.",
            fg_color="transparent", text_color=DIM, font=(F,8),
            justify="left").pack(anchor="w", padx=14, pady=(0,6))
        self.facts_list = ctk.CTkScrollableFrame(inner, fg_color="transparent",
            scrollbar_button_color=BG3, scrollbar_button_hover_color=BG4)
        self.facts_list.pack(fill="both", expand=True, padx=8, pady=(0,10))

    def _toggle_sidebar(self):
        """Collapse/restore the left sidebar — for split-screen / vertical
        layouts where only the conversation matters."""
        self._sb_hidden = not getattr(self, "_sb_hidden", False)
        if self._sb_hidden:
            self.sb_frame.pack_forget()
            self._sb_border.pack_forget()
        else:
            self._sb_border.pack(side="left", fill="y",
                                 before=self.mid_frame)
            self.sb_frame.pack(side="left", fill="y",
                               before=self._sb_border)
        self._settings["sidebar_hidden"] = self._sb_hidden
        self._save_settings()

    def _toggle_facts(self):
        self._facts_visible = not self._facts_visible
        if self._facts_visible:
            self.facts_frame.pack(side="right", fill="y")
        else:
            self.facts_frame.pack_forget()

    def _add_facts(self, pairs):
        # Collect facts silently — the panel opens only when the user clicks
        # 📌 Facts (auto-opening mid-call caused a layout hitch).
        for icon, val in pairs:
            if val in self._facts: continue
            self._facts[val] = icon
            ctk.CTkButton(self.facts_list, text=f"{icon}  {val}",
                fg_color=BG3, hover_color=BG4, text_color=TEXT,
                font=(FM,10), height=30, corner_radius=8, anchor="w",
                command=lambda v=val: self._copy_fact(v)
                ).pack(fill="x", pady=2, padx=2)

    def _copy_fact(self, val):
        try:
            self.root.clipboard_clear(); self.root.clipboard_append(val)
            self.hdr_status.configure(text=f"Copied: {val}", text_color=GREEN)
        except Exception: pass

    def _clear_facts(self):
        self._facts.clear()
        for w in self.facts_list.winfo_children(): w.destroy()

    def _build_notes(self, frm):
        tk.Frame(frm, bg=BORDER, width=1).pack(side="left", fill="y")
        inner = ctk.CTkFrame(frm, fg_color=BG2, corner_radius=0)
        inner.pack(side="left", fill="both", expand=True)
        top = ctk.CTkFrame(inner, fg_color="transparent")
        top.pack(fill="x", padx=14, pady=(16,6))
        ctk.CTkLabel(top, text="📝 Notes", fg_color="transparent",
            text_color=TEXT, font=(F,12,"bold")).pack(side="left")
        ctk.CTkButton(top, text="Clear", width=48, height=24,
            fg_color="transparent", hover_color=BG3, text_color=DIM,
            font=(F,8), corner_radius=6,
            command=self._clear_notes).pack(side="right")

        ctk.CTkLabel(inner, text="QUICK FIELDS", fg_color="transparent",
            text_color=DIM, font=(F,8,"bold")).pack(anchor="w", padx=14, pady=(6,4))
        def qf(icon, ph):
            r = ctk.CTkFrame(inner, fg_color="transparent")
            r.pack(fill="x", padx=12, pady=2)
            ctk.CTkLabel(r, text=icon, fg_color="transparent",
                text_color=DIM, font=(F,10), width=18).pack(side="left")
            e = ctk.CTkEntry(r, placeholder_text=ph,
                fg_color=BG3, text_color=TEXT, border_width=0, font=(F,10),
                corner_radius=8, height=30)
            e.pack(side="left", fill="x", expand=True, padx=(4,0))
            return e
        self.qf_claim = qf("#",  "Claim / reference #")
        self.qf_alert = qf("⚠",  "Allergy / alert")
        self.qf_appt  = qf("🕐", "Callback / appt time")

        ctk.CTkLabel(inner, text="FREE NOTES", fg_color="transparent",
            text_color=DIM, font=(F,8,"bold")).pack(anchor="w", padx=14, pady=(12,4))
        self.notes_box = ctk.CTkTextbox(inner, fg_color=BG3, text_color=TEXT,
            font=(F,11), corner_radius=10, wrap="word")
        self.notes_box.pack(fill="both", expand=True, padx=12, pady=(0,8))
        ctk.CTkLabel(inner,
            text="Saved with the session & included in Export.",
            fg_color="transparent", text_color=DIM, font=(F,8)
            ).pack(anchor="w", padx=14, pady=(0,10))

    def _toggle_notes(self):
        self._notes_visible = not self._notes_visible
        if self._notes_visible:
            self.notes_frame.pack(side="right", fill="y")
        else:
            self.notes_frame.pack_forget()

    def _clear_notes(self):
        try:
            for e in (self.qf_claim, self.qf_alert, self.qf_appt):
                e.delete(0, "end")
            self.notes_box.delete("1.0", "end")
        except Exception: pass

    def _autofill_notes(self, pairs):
        # Detected facts flow into the Quick Fields — but only into EMPTY
        # fields, never overwriting what the user typed.
        try:
            for icon, val in pairs:
                if icon == "#" and not self.qf_claim.get().strip():
                    self.qf_claim.insert(0, val)
                elif icon == "📅" and not self.qf_appt.get().strip():
                    self.qf_appt.insert(0, val)
        except Exception:
            pass

    def _notes_snapshot(self):
        # Returns a list of non-empty note lines, or [] if nothing written.
        out = []
        try:
            if self.qf_claim.get().strip(): out.append("Claim / Ref #: " + self.qf_claim.get().strip())
            if self.qf_alert.get().strip(): out.append("Allergy / Alert: " + self.qf_alert.get().strip())
            if self.qf_appt.get().strip():  out.append("Callback / Appt: " + self.qf_appt.get().strip())
            free = self.notes_box.get("1.0", "end").strip()
            if free: out.append("Notes:\n" + free)
        except Exception:
            pass
        return out

    def _build_dbg(self, frm):
        tk.Frame(frm, bg=BORDER, width=1).pack(side="left", fill="y")
        inner = ctk.CTkFrame(frm, fg_color=BG2, corner_radius=0)
        inner.pack(side="left", fill="both", expand=True)
        ctk.CTkLabel(inner, text="Debug", fg_color="transparent",
            text_color=DIM, font=(F,11,"bold")).pack(anchor="w", padx=16, pady=(16,10))
        sg = ctk.CTkFrame(inner, fg_color="transparent")
        sg.pack(fill="x", padx=16, pady=(0,10))
        self.d_cap  = self._dr(sg, "Captured",  "—", 0)
        self.d_sent = self._dr(sg, "Sent",      "—", 1)
        self.d_msgs = self._dr(sg, "Responses", "0", 2)
        self.d_ws   = self._dr(sg, "WebSocket", "—", 3)
        self.wave_cv = tk.Canvas(inner, bg=BG, height=52, highlightthickness=0)
        self.wave_cv.pack(fill="x", padx=16, pady=(0,10))
        lf = tk.Frame(inner, bg=BG)
        lf.pack(fill="both", expand=True, padx=16, pady=(0,16))
        ls = tk.Scrollbar(lf, bg=BG3, troughcolor=BG2, width=4)
        ls.pack(side="right", fill="y")
        mono = next((f for f in ["JetBrains Mono","Consolas","Courier New"]
                     if f in tkfont.families()), FM)
        self.log_txt = tk.Text(lf, bg=BG, fg=TEXT, font=(mono,8), relief="flat", bd=0,
            padx=8, pady=4, wrap="word", yscrollcommand=ls.set, state="disabled")
        self.log_txt.pack(side="left", fill="both", expand=True)
        ls.config(command=self.log_txt.yview)
        for tag, col in [("INFO","#6b7fa8"),("AUDIO",ACCENT),("WS",PURPLE),
                         ("DG",GREEN),("ERROR",RED),("WARN",YELLOW)]:
            self.log_txt.tag_config(tag, foreground=col)

    # ── UI helpers ───────────────────────────────────────────────
    def _dr(self, p, label, val, row):
        ctk.CTkLabel(p, text=label, fg_color="transparent",
            text_color=DIM, font=(F,8)).grid(row=row, column=0, sticky="w", pady=2)
        lbl = ctk.CTkLabel(p, text=val, fg_color="transparent",
            text_color=TEXT, font=(F,8,"bold"))
        lbl.grid(row=row, column=1, sticky="e", pady=2)
        p.columnconfigure(1, weight=1); return lbl

    def _toggle_debug(self):
        if self.show_debug.get(): self.dbg_frame.pack(side="right", fill="y")
        else:                     self.dbg_frame.pack_forget()

    def _div(self, p):
        tk.Frame(p, bg=BORDER, height=1).pack(fill="x", padx=18, pady=8)

    def _sec(self, p, t):
        ctk.CTkLabel(p, text=t, fg_color="transparent",
            text_color=DIM, font=(F,8,"bold")).pack(anchor="w", padx=18, pady=(0,4))

    def _stat(self, p, v, k, r, c, size=18):
        f = ctk.CTkFrame(p, fg_color="transparent")
        f.grid(row=r, column=c, sticky="w", padx=(0,12), pady=2)
        lbl = ctk.CTkLabel(f, text=v, fg_color="transparent",
            text_color=TEXT, font=(F,size,"bold")); lbl.pack(anchor="w")
        ctk.CTkLabel(f, text=k, fg_color="transparent",
            text_color=DIM, font=(F,8)).pack(anchor="w")
        return lbl

    # ── Scroll helper ────────────────────────────────────────────
    # ── Smooth scrolling ─────────────────────────────────────────
    def _inside_feed(self, xr, yr):
        try:
            w = self.root.winfo_containing(xr, yr)
        except Exception:
            return False
        while w is not None:
            if w is self.feed or w is self.feed._parent_canvas:
                return True
            w = getattr(w, "master", None)
        return False

    def _feed_wheel(self, event):
        """Eased wheel scrolling: each notch nudges a target position and an
        animation glides the view toward it instead of jumping."""
        if not self._inside_feed(event.x_root, event.y_root):
            return
        cv = self.feed._parent_canvas
        try:
            bbox = cv.bbox("all")
            content_h = max((bbox[3] - bbox[1]) if bbox else 1, 1)
            if content_h <= cv.winfo_height():
                return
            notches = -event.delta / 120.0
            step = 110.0 / content_h            # ≈110 px per wheel notch
            base = (self._scroll_target if self._scroll_job
                    else cv.yview()[0])
            self._scroll_target = min(max(base + notches * step, 0.0), 1.0)
            if not self._scroll_job:
                self._smooth_step()
        except Exception:
            pass

    def _smooth_step(self):
        cv = self.feed._parent_canvas
        try:
            cur, tgt = cv.yview()[0], self._scroll_target
            diff = tgt - cur
            if abs(diff) < 0.0004:
                cv.yview_moveto(tgt)
                self._scroll_job = None
                return
            cv.yview_moveto(cur + diff * 0.30)   # exponential ease-out
            self._scroll_job = self.root.after(14, self._smooth_step)
        except Exception:
            self._scroll_job = None

    def _feed_pan_start(self, event):
        """Middle-click autoscroll: hold the wheel button and drag —
        the feed glides at a speed proportional to the drag distance."""
        if not self._inside_feed(event.x_root, event.y_root):
            return
        self._pan_active = True
        self._pan_anchor = event.y_root
        self._pan_loop()

    def _feed_pan_stop(self, event=None):
        self._pan_active = False

    def _pan_loop(self):
        if not self._pan_active:
            return
        cv = self.feed._parent_canvas
        try:
            dy = self.root.winfo_pointery() - self._pan_anchor
            bbox = cv.bbox("all")
            content_h = max((bbox[3] - bbox[1]) if bbox else 1, 1)
            cur = cv.yview()[0]
            cv.yview_moveto(min(max(cur + (dy * 0.35) / content_h, 0.0), 1.0))
        except Exception:
            pass
        self.root.after(16, self._pan_loop)

    def _reset_feed_view(self):
        """After clearing, the canvas briefly remembers the OLD content
        height — the view parks in empty blackness and new bubbles appear
        far above it. Recompute the real region and snap to the top; run
        again after layout settles to catch the late shrink."""
        try:
            if self._scroll_job:
                self.root.after_cancel(self._scroll_job)
                self._scroll_job = None
            self._scroll_target = None
            self.root.update_idletasks()
            cv = self.feed._parent_canvas
            bbox = cv.bbox("all") or (0, 0, 0, 1)
            cv.configure(scrollregion=bbox)
            cv.yview_moveto(0.0)
        except Exception:
            pass

    def _scroll_feed_to_bottom(self):
        self.root.update_idletasks()
        try:
            if self._scroll_job:                 # cancel any glide in flight
                self.root.after_cancel(self._scroll_job)
                self._scroll_job = None
            self._scroll_target = None
            canvas = self.feed._parent_canvas
            canvas.configure(scrollregion=canvas.bbox("all"))
            canvas.yview_moveto(1.0)
        except: pass

    # ── Device ───────────────────────────────────────────────────
    def _load_devices(self):
        try: self.devices = list_input_devices()
        except Exception as e: self.log.log(f"Device scan error: {e}","ERROR"); return
        names = []; best = None
        for i, d in enumerate(self.devices):
            if d.get("is_output_loopback"):
                names.append(f"🔊 {d['name']} — what you hear")
            else:
                tag = " VB-Cable" if d["is_vbcable"] else " Loopback" if d["is_loopback"] else ""
                names.append(f"[{d['index']}] {d['name']}{tag}")
            if best is None and d["is_vbcable"]:
                best = i
        if best is None:   # no VB-Cable → prefer a what-you-hear device over a mic
            best = next((i for i, d in enumerate(self.devices)
                         if d.get("is_output_loopback")), 0)
        # Restore the device used last session (overrides auto-pick)
        saved = self._settings.get("device_name", "")
        if saved:
            for j, d in enumerate(self.devices):
                if d["name"] == saved:
                    best = j; break
        if names:
            self.dev_combo["values"] = names; self.dev_combo.current(best); self._on_dev()
        else:
            self.dev_combo["values"] = ["No input devices"]
            self.dev_lbl.configure(text="None found", text_color=RED)

    def _on_dev(self, _=None):
        self._save_settings()
        i = self.dev_combo.current()
        if 0 <= i < len(self.devices):
            d = self.devices[i]
            if d.get("is_output_loopback"):
                self.dev_lbl.configure(
                    text=f"{d['name']} — captures everything playing on this device (not your mic)",
                    text_color=GREEN)
                return
            tag = ("VB-Cable OK" if d["is_vbcable"] else
                   "Loopback OK" if d["is_loopback"] else "Warning: not a loopback")
            col = ACCENT if (d["is_vbcable"] or d["is_loopback"]) else YELLOW
            self.dev_lbl.configure(text=f"{d['name']} — {tag}", text_color=col)

    def _save_key(self):
        k = self.api_key_var.get().strip()
        if k: self._save_key_file(k); self.hdr_status.configure(text="Key saved", text_color=GREEN)
        else: self.hdr_status.configure(text="Enter key first", text_color=YELLOW)

    def _save_anthropic_key(self):
        k = self.anthropic_key_var.get().strip()
        if k:
            self._save_key_file(k, "anthropic.key")
            self.translator.set_anthropic_key(k)
            self.hdr_status.configure(text="Anthropic key saved", text_color=GREEN)
        else:
            self.hdr_status.configure(text="Enter Anthropic key first", text_color=YELLOW)

    # ── Record ───────────────────────────────────────────────────
    def _toggle(self):
        if getattr(self, "_connecting", False):
            return              # ignore clicks while a connection is in flight
        if self.recording: self._stop()
        else:              self._start()

    def _start(self):
        api_key = self.api_key_var.get().strip()
        if not api_key:
            messagebox.showerror("No API Key",
                "Enter your Deepgram API key first.\nconsole.deepgram.com"); return
        if not self.translator._claude:
            messagebox.showwarning("No Claude key",
                "Translations need your Claude (Anthropic) API key.\n"
                "Add it in ⚙ Settings. Until then Sayvo will only transcribe.")
        idx = self.dev_combo.current()
        if idx < 0 or idx >= len(self.devices):
            messagebox.showerror("No Device","Select an audio input device."); return
        dev = self.devices[idx]
        src = self.src_lang_var.get()

        self._connecting = True
        self.rec_btn.configure(state="disabled", text="Connecting…")
        self.pipe_lbl.configure(text="Connecting to Deepgram…", text_color=YELLOW)
        self.hdr_status.configure(text="Connecting…", text_color=YELLOW)

        self._dg = DeepgramStream(api_key, self.ui_q, self.log)
        self._pending_dev = dev
        keyterms = list(self._glossary.keys()) if self._glossary else None
        allowed  = set(self._pair_codes())      # only trust in-pair auto-detect
        threading.Thread(target=self._do_connect,
                         args=(self._dg, src, keyterms, allowed), daemon=True).start()

    def _do_connect(self, dg, src, keyterms=None, allowed=None):
        ok = dg.connect(src_lang_name=src, keyterms=keyterms, allowed=allowed)
        try: self.ui_q.put_nowait(("connect_result", ok))
        except: pass

    def _resolve_device(self, dev):
        """Re-enumerate audio devices and match the selection by NAME.
        Device indices go stale whenever a device is added/removed/disabled
        (e.g. disabling VB-Cable renumbers everything → Errno -9996)."""
        try:
            fresh = list_input_devices()
        except Exception:
            return dev
        for d in fresh:
            if (d["name"] == dev["name"] and
                    d.get("is_output_loopback") == dev.get("is_output_loopback")):
                if d["index"] != dev["index"]:
                    self.log.log(f"Device '{d['name']}' index moved "
                                 f"{dev['index']}→{d['index']} — re-resolved","AUDIO")
                return d
        return dev

    def _finish_start(self):
        self._connecting = False
        dev = self._resolve_device(self._pending_dev)
        self._cap = AudioCapture(dev["index"], self._on_chunk, self._on_level, self.log,
                                 prefer_direct=dev.get("is_loopback", False),
                                 output_loopback=dev.get("is_output_loopback", False))
        try:
            dev_name = self._cap.start()
        except Exception as e:
            self.log.log(f"Audio start failed: {e}","ERROR")
            self._dg.stop()
            self.rec_btn.configure(state="normal", text="⏺  Start Listening",
                fg_color=ACCENT, hover_color=ACCENT_HOV, text_color="white")
            self.pipe_lbl.configure(text="Audio error — check Debug log", text_color=RED)
            self.hdr_status.configure(text="Audio error", text_color=RED)
            messagebox.showerror("Audio Error",
                f"{e}\n\nThe audio device may have been disconnected, disabled, "
                "or changed.\nI've rescanned your devices — reselect your source "
                "and press Start again.")
            self._load_devices()   # refresh the dropdown with current indices
            return

        self.recording = True; self.session_start = time.time()
        self._last_audio_t = time.time(); self._audio_warn = False
        self._gate = SpeechGate()
        if SpeechGate._session is not None:
            self.log.log("Smart pause ready (silence/hold music not billed)", "INFO")
        elif SpeechGate._load_err:
            self.log.log(f"Smart pause unavailable ({SpeechGate._load_err}), streaming everything", "WARN")
        # Open auto-save session cache (crash recovery — never lose a conversation)
        try:
            sp = Path.home() / ".interpro" / "sessions"
            sp.mkdir(parents=True, exist_ok=True)
            fname = sp / f"session_{time.strftime('%Y%m%d_%H%M%S')}.jsonl"
            self._session_file = open(fname, "w", encoding="utf-8")
            self.log.log(f"Session cache: {fname.name}","INFO")
        except Exception as e:
            self.log.log(f"Session cache unavailable: {e}","WARN")
        self.rec_btn.configure(state="normal", text="⏹  Stop Recording",
            fg_color=RED, hover_color=RED_HOV, text_color="white")
        self.rec_lbl.configure(text=f"● {dev_name}", text_color=ACCENT)
        self.live_dot.configure(text_color=ACCENT)
        self.live_confirmed.configure(text="")
        self.live_partial.configure(text="Listening…", text_color=DIM)
        self.eng_lbl.configure(text="Deepgram Nova-3", text_color=DIM)
        self.pipe_lbl.configure(text="✓ Streaming to Deepgram", text_color=GREEN)
        self.hdr_status.configure(text="Recording", text_color=GREEN)

    def _connect_failed(self):
        self._connecting = False
        self.rec_btn.configure(state="normal", text="⏺  Start Listening",
            fg_color=ACCENT, hover_color=ACCENT_HOV, text_color="white")
        self.pipe_lbl.configure(text="Connection failed — check Debug log", text_color=RED)
        self.hdr_status.configure(text="Connection failed", text_color=RED)
        messagebox.showerror("Connection Failed",
            "Could not connect to Deepgram.\n\nSee the Debug panel for the exact error.")

    def _stop(self):
        saved = self._gate.saved_seconds() if self._gate else 0.0
        self._gate = None
        if saved >= 1:
            self.log.log(f"Smart pause: {saved/60:.1f} min of silence/hold not billed", "INFO")
        if self.recording and self.session_start:
            m = self._usage_month()
            m["sec"] = m.get("sec", 0) + int(time.time() - self.session_start)
            m["saved_sec"] = m.get("saved_sec", 0) + int(saved)
            self._save_settings(); self._usage_refresh()
        self.recording = False
        if self._cap: self._cap.stop(); self._cap = None
        if self._dg:  self._dg.stop();  self._dg  = None
        # Cancel any pending grace timer before final flush
        if self._grace_timer:
            self.root.after_cancel(self._grace_timer); self._grace_timer = None
        self._para_flush()   # flush remaining buffer → Claude thread takes care of it
        # Persist notes alongside the session cache
        try:
            notes = self._notes_snapshot()
            if notes and self._session_file:
                np = Path(self._session_file.name).with_suffix("")
                Path(str(np) + "_notes.txt").write_text(
                    "\n".join(notes), encoding="utf-8")
        except Exception:
            pass
        # Close session cache
        if self._session_file:
            try: self._session_file.close()
            except: pass
            self._session_file = None
        self.rec_btn.configure(text="⏺  Start Listening", fg_color=ACCENT,
            hover_color=ACCENT_HOV, text_color="white")
        self.rec_lbl.configure(text="Ready", text_color=DIM)
        self.live_dot.configure(text_color=MUT)
        self.live_confirmed.configure(text="")
        self.live_partial.configure(text="Waiting for speech…", text_color=TEXT)
        self._overlay_speech("")
        self._set_meter(0, False)
        self._det_lbl.configure(
            text=f"Speak in {self.lang_a_var.get()} or {self.lang_b_var.get()}",
            text_color=DIM)
        self.pipe_lbl.configure(
            text=(f"Idle · {saved/60:.0f} min of silence/hold not billed this call"
                  if saved >= 60 else "Idle"), text_color=DIM)
        self.hdr_status.configure(text="Ready", text_color=DIM)

    # ── Audio callbacks ──────────────────────────────────────────
    def _on_chunk(self, chunk):
        if self._privacy: return        # privacy pause — nothing leaves the app
        dg = self._dg
        if not dg: return
        pcm = (chunk * 32767).astype(np.int16).tobytes()
        gate = self._gate
        if gate is None or not self._smart_pause:
            dg.send_pcm(pcm); return
        was = gate.open
        for blob in gate.feed(chunk, pcm):
            dg.send_pcm(blob)
        if gate.open != was:
            try: self.ui_q.put_nowait(("gate", gate.open))
            except: pass

    def _toggle_smart_pause(self):
        self._smart_pause = bool(self.smart_pause_var.get())
        self._settings["smart_pause"] = self._smart_pause
        self._save_settings()
        if self._gate:
            self._gate.reset()            # resume streaming right away
        if self.recording:
            self._show_gate(True)

    def _show_gate(self, is_open):
        if is_open:
            self.pipe_lbl.configure(text="✓ Streaming to Deepgram", text_color=GREEN)
        else:
            self.pipe_lbl.configure(text="⏸ No one talking: audio paused (not billed)",
                                    text_color=DIM)

    def _on_level(self, lvl, spk, bcap):
        try: self.ui_q.put_nowait(("level", lvl, spk, bcap))
        except: pass

    # ══════════════════════════════════════════════════════════════
    # Poll loop  (runs on main thread every 33 ms)
    # ══════════════════════════════════════════════════════════════
    def _poll(self):
        try:
            while True:
                item = self.ui_q.get_nowait(); k = item[0]
                if k == "level":
                    _, lvl, spk, bcap = item
                    if lvl > 0.01:
                        self._last_audio_t = time.time()
                        if self._audio_warn:
                            self._audio_warn = False
                            self.hdr_status.configure(text="Recording", text_color=GREEN)
                    self._set_meter(lvl, spk)
                    now = time.time()
                    if now - getattr(self, "_lbl_t", 0) > 0.4:   # throttle label churn
                        self._lbl_t = now
                        self.bytes_lbl.configure(text=f"{bcap/1024:.1f} KB captured")
                        self.d_cap.configure(text=f"{bcap/1024:.1f} KB")
                        if self._dg:
                            self.d_sent.configure(text=f"{self._dg._bytes_sent/1024:.1f} KB")
                            self.d_msgs.configure(text=str(self._dg._msgs_recv))
                elif k == "connect_result":
                    _, ok = item
                    if ok: self._finish_start()
                    else:  self._connect_failed()
                elif k == "phrase":
                    if self._gate and getattr(item[1], "text", "").strip():
                        self._gate.touch()
                    self._handle(item[1])
                elif k == "gate":
                    if self.recording and self._smart_pause:
                        self._show_gate(item[1])
                elif k == "flush_para":
                    if len(item) > 1 and item[1] == "immediate":
                        self._para_flush()   # speaker change — no grace delay
                    elif len(item) > 1 and item[1] == "endpoint":
                        self._maybe_flush(endpoint=True)   # ~0.5 s pause
                    else:
                        self._maybe_flush()  # UtteranceEnd (~1 s) — smart boundary
                elif k == "ws_status":
                    _, st, msg = item
                    c = {"connected":GREEN,"warn":YELLOW,"error":RED}.get(st, DIM)
                    self.pipe_lbl.configure(text=msg, text_color=c)
                    self.hdr_status.configure(text=msg, text_color=c)
                    self.d_ws.configure(text=msg, text_color=c)
                elif k == "ws_bytes":
                    if self._dg:
                        self.d_sent.configure(text=f"{self._dg._bytes_sent/1024:.1f} KB")
                elif k == "latency":
                    self.lat_lbl.configure(text=f"{int(item[1])}ms")
                elif k == "log":
                    _, level, line = item; self._add_log(line, level)
                elif k == "speech_start":
                    self.live_dot.configure(text_color=ACCENT)
        except queue.Empty:
            pass

        # Breathing LIVE dot while recording (~0.8s cycle) + pill dot sync
        self._pulse = getattr(self, "_pulse", 0) + 1
        if self._pulse % 12 == 0:
            try:
                if self.recording and not self._privacy:
                    cur = self.live_dot.cget("text_color")
                    self.live_dot.configure(
                        text_color="#3a4c8f" if cur == ACCENT else ACCENT)
                self.hdr_dot.configure(
                    text_color=self.hdr_status.cget("text_color"))
            except Exception: pass

        if getattr(self, "_hdr_rec_state", None) != self.recording:
            self._hdr_rec_state = self.recording
            try:
                if self.recording:
                    self.hdr_rec_btn.configure(text="⏹", fg_color=RED,
                                               hover_color=RED_HOV)
                else:
                    self.hdr_rec_btn.configure(text="⏺", fg_color=ACCENT,
                                               hover_color=ACCENT_HOV)
            except Exception: pass

        if self.recording and self.session_start:
            e = int(time.time() - self.session_start)
            self.sv_time.configure(text=f"{e//60}:{e%60:02d}")
            # Silence watchdog: source died (site stream dropped, headset
            # power-save, device re-created) → tell the user immediately
            if (not self._audio_warn and self._last_audio_t
                    and not self._privacy
                    and time.time() - self._last_audio_t > 45):
                self._audio_warn = True
                self.hdr_status.configure(
                    text="⚠ No audio for 45s — refresh the site or press Stop → Start",
                    text_color=YELLOW)
                self.log.log("Silence watchdog: no audio for 45s","WARN")
        if self.show_debug.get() and self._cap:
            self._draw_wave()

        # Adaptive: quick while recording, relaxed when idle → low CPU on
        # weak machines without any visible lag during a call.
        self.root.after(45 if self.recording else 120, self._poll)

    def _set_meter(self, lvl, spk):
        now = time.time()
        if now - self._last_meter < 0.05: return
        self._last_meter = now
        pct = min(1.0, lvl * 4.0)
        color = GREEN if pct < 0.55 else YELLOW if pct < 0.82 else RED
        self.meter.set(pct); self.meter.configure(progress_color=color)
        self.meter_lbl.configure(
            text="● SPEECH DETECTED" if spk else "○ Silence",
            text_color=ACCENT if spk else DIM)

    def _draw_wave(self):
        w = self.wave_cv.winfo_width(); h = self.wave_cv.winfo_height()
        if w < 10 or h < 10: return
        self.wave_cv.delete("all")
        data = self._cap.waveform if self._cap else []
        if not data: return
        mx = max(max(data), 0.01)
        for i, v in enumerate(data):
            x = i*w/len(data); bh = (v/mx)*(h-4)
            self.wave_cv.create_line(x, h, x, h-bh,
                fill=GREEN if v<0.05 else YELLOW if v<0.15 else RED, width=1)

    def _add_log(self, line, level):
        self.log_txt.config(state="normal")
        self.log_txt.insert("end", line+"\n", level)
        lc = int(self.log_txt.index("end-1c").split(".")[0])
        if lc > 400: self.log_txt.delete("1.0", f"{lc-400}.0")
        self.log_txt.see("end"); self.log_txt.config(state="disabled")

    # ══════════════════════════════════════════════════════════════
    # TRACK 1 — LIVE zone  (instant, word-by-word)
    # ══════════════════════════════════════════════════════════════
    def _handle(self, p: Phrase):
        """Route incoming Deepgram phrase to the right track.

        PARTIAL  → confirmed buffer (white) + current partial (gray ▋)
        FINAL    → commit text to paragraph buffer; confirmed label updates, partial clears
        """
        # ── Confidence / language indicator ─────────────────────
        self.conf_bar.set(p.conf)
        self.conf_lbl.configure(text=f"{int(p.conf*100)}%")
        self.sv_lang.configure(text=p.lang.upper())
        _det = TRANS_NAME.get(p.lang, p.lang.upper())
        _tgt = TRANS_NAME.get(self._target_for(p.lang), "?")
        _arrow = f"{_det} → {_tgt}"
        self._det_lbl.configure(
            text=f"● {_det} detected  —  {_arrow}",
            text_color=GREEN if not p.partial else DIM)

        if p.partial:
            # ── Ghost Edit: confirmed buffer (white) + current partial (gray) ──
            buf = self._para_text.strip()
            live_text = (buf + " " + p.text).strip() if buf else p.text
            self._current_partial = p.text  # track for manual flush
            # Confirmed finals stay white; only the in-flight partial is gray
            self.live_confirmed.configure(text=buf)
            self.live_partial.configure(text=p.text + "  ▋", text_color=DIM)

            self._overlay_speech(live_text)
        else:
            # ── Final: split into sentences, add each separately ──
            # This prevents multi-sentence Deepgram finals from mixing
            # languages or piling up in the live bar as one blob.
            from dataclasses import replace as dc_replace
            self._current_partial = ""  # final arrived — clear the partial tracker
            for sent in _split_sentences(p.text):
                mini = dc_replace(p, text=sent)
                self._para_add_text(mini)
                buf = self._para_text.strip()
                # Paragraph grouping: keep accumulating sentences while the
                # speaker is still talking. Only cut mid-speech when the
                # paragraph is genuinely long (6 sentences / ~70 words) —
                # end-of-thought comes from a real pause (safety timer).
                if buf and buf[-1] in '.!?…':
                    n_sents = len(re.findall(r'[.!?…]', buf))
                    if n_sents >= 6 or len(buf.split()) >= 70:
                        self._para_flush()
            buf = self._para_text.strip()
            # Show whatever remains in the buffer (incomplete sentence)
            self.live_confirmed.configure(text=buf if buf else "")
            self.live_partial.configure(text="")
            self._overlay_speech(buf)

    # ══════════════════════════════════════════════════════════════
    # Paragraph buffer (accumulates finals until flush trigger)
    # ══════════════════════════════════════════════════════════════
    def _para_add_text(self, p: Phrase):
        """Append a final phrase's text to the accumulation buffer."""
        new_text = p.text.strip()
        if not new_text: return

        # Language-change flush: if the incoming text is a different language
        # from what's already in the buffer, flush the buffer immediately so
        # English and Spanish never end up in the same bubble.
        if (self._para_text.strip() and
                self._para_lang and p.lang and
                p.lang != self._para_lang):
            if self._grace_timer:
                self.root.after_cancel(self._grace_timer)
                self._grace_timer = None
            self._para_flush()
            # Buffer is now empty — fall through to start fresh paragraph

        if self._para_text:
            buf_lower = self._para_text.lower().strip()
            new_lower = new_text.lower()
            # Skip if it's a duplicate or strict subset
            if (new_lower == buf_lower or
                new_lower in buf_lower or
                buf_lower.endswith(new_lower)):
                return
            # If new text is a superset that starts with our buffer, replace
            if new_lower.startswith(buf_lower[:min(30, len(buf_lower))]):
                self._para_text = new_text
            else:
                self._para_text += " " + new_text
        else:
            self._para_text = new_text

        self._para_lang    = p.lang
        self._para_conf    = max(self._para_conf, p.conf)
        self._para_speaker = p.speaker
        if self._para_ts is None: self._para_ts = p.ts

        # Cancel and restart safety timer after each new sentence
        self._safety_extended = False   # fresh speech → fresh grace state
        if self._para_timer:
            self._para_timer.cancel(); self._para_timer = None

        words = len(self._para_text.split())
        if words >= self.PARA_MAX_WORDS:
            self._para_flush()
        else:
            self._para_timer = threading.Timer(self.PARA_SAFETY_SEC, self._sched_flush)
            self._para_timer.daemon = True
            self._para_timer.start()

    def _sched_flush(self):
        """Safety timer fired — the speaker has paused.

        Two-stage: a complete sentence flushes now. Text ending mid-thought
        ("housing allowance,") gets ONE extended grace window — slow script
        reading pauses 3-4 s between items and those belong together. After
        the extension it flushes no matter what, so nothing ever lingers."""
        text = self._para_text.strip()
        if not text:
            return
        if text[-1] in '.!?…' or self._safety_extended:
            self._safety_extended = False
            self.root.after(0, self._para_flush)
        else:
            self._safety_extended = True
            self._para_timer = threading.Timer(self.PARA_EXTEND_SEC, self._sched_flush)
            self._para_timer.daemon = True
            self._para_timer.start()

    def _manual_flush(self):
        """Manual flush button — sends whatever is live right now to a bubble.
        If the buffer has finalized text, flush that. Otherwise grab the current
        in-flight partial (not yet finalized by Deepgram) and force it through."""
        if self._para_text.strip():
            self._para_flush()
        elif self._current_partial.strip():
            # Force the in-flight partial into the buffer as if it were a final
            partial = self._current_partial.strip()
            with self._para_lock:
                self._para_text = partial
                if self._para_ts is None:
                    self._para_ts = time.time()
            self._current_partial = ""
            self.live_partial.configure(text="")
            self._para_flush()

    # ══════════════════════════════════════════════════════════════
    # TRACK 2 — Smart flush → Claude → Bubble
    # ══════════════════════════════════════════════════════════════
    def _maybe_flush(self, endpoint=False):
        """Smart flush on a pause in speech.

        endpoint=True  : Deepgram speech_final, ~0.5 s of silence
        endpoint=False : Deepgram UtteranceEnd, ~1 s of silence

        Rules (v44 — user wanted short questions/answers to land fast):
        1. A question ("...?") flushes at the first 0.5 s pause: the other
           person is now waiting for an answer.
        2. Any complete sentence (. ! ? …) flushes after 1 s of silence.
           Breaths shorter than that never reach here, so continuous talk
           still groups into one paragraph.
        3. >= 60 words even without punctuation → runaway → flush.
        4. Otherwise (mid-thought, e.g. ends on a comma) → keep building;
           the safety timer flushes it (2 s, +3 s grace).
        """
        if self._grace_timer:
            self.root.after_cancel(self._grace_timer)
            self._grace_timer = None
        with self._para_lock:
            text = self._para_text.strip()
        if not text:
            return
        if endpoint:
            if text[-1] == '?':
                self._para_flush()
            return
        if text[-1] in '.!?…' or len(text.split()) >= 60:
            self._para_flush()
        # else: mid-thought, keep building the paragraph

    def _grace_flush(self):
        """Fires 350 ms after a mid-length non-punctuated UtteranceEnd."""
        self._grace_timer = None
        self._para_flush()

    def _para_flush(self):
        """Extract the accumulated paragraph, create the bubble slot immediately
        (preserving display order), then hand translation off to a background thread.
        Called on: UtteranceEnd, speaker change, word cap, stop recording.
        """
        with self._para_lock:
            if not self._para_text.strip(): return
            if self._para_timer:
                self._para_timer.cancel(); self._para_timer = None
            text    = self._para_text.strip()
            lang    = self._para_lang
            conf    = self._para_conf
            ts      = self._para_ts or time.time()
            speaker = self._para_speaker
            self._para_text    = ""
            self._para_conf    = 0.0
            self._para_ts      = None
            self._para_speaker = -1
            # Allocate a sequential id in flush order — this guarantees
            # committed dict (and therefore _rebuild) stays in the right order
            self._bubble_count += 1
            pid = f"para_{self._bubble_count:04d}"
        self._current_partial = ""

        # Re-apply formatters on the full buffer — numbers often arrive split
        # across separate finals ("doscientos cincuenta" | "y tres")
        text = _compound_numbers_to_digits(text)
        text = _words_to_digits(text)
        text = _format_currency(text)
        text = _format_phone(text)
        text = _join_digit_runs(text)

        # Clear the LIVE workspace instantly
        self.live_confirmed.configure(text="")
        self.live_partial.configure(text="", text_color=DIM)
        self._overlay_speech("")

        # ── Create the bubble slot RIGHT NOW on the main thread ──
        # This locks in display order regardless of when Claude finishes.
        p = Phrase(id=pid, text=text, translation="",
                   lang=lang, conf=conf, ts=ts, partial=False, speaker=speaker)
        self.committed[pid] = p

        self.seg_count  += 1
        self.word_count += len(text.split())
        self.sv_segs.configure(text=str(self.seg_count))
        self.sv_words.configure(text=str(self.word_count))

        self._update_empty_state()           # remove the empty-feed hint
        tr_lbl, eng_lbl = self._row_slot(p)  # bubble visible immediately
        # Tk canvases break (black, unclickable) past ~32k px of content.
        # Keep only the newest ~120 bubbles ON SCREEN — the full transcript
        # stays in committed / session file / exports / summary.
        kids = self.feed.winfo_children()
        if len(kids) > 80:
            for w in kids[:len(kids) - 80]:
                try: w.destroy()
                except Exception: pass
        if self.autoscroll.get():
            self._scroll_feed_to_bottom()

        # Refresh the conversation summary every 20 bubbles (was 8 —
        # 24 summaries per session was rebilling ~2k tokens each time)
        if self.seg_count % 20 == 0:
            self._maybe_summarize()

        # Fire off Claude translation in background — fills tr_lbl when done
        threading.Thread(
            target=self._translate_and_push_bubble,
            args=(text, lang, conf, ts, speaker, pid, tr_lbl, eng_lbl),
            daemon=True
        ).start()

    def _bubble_card(self, p, live=False):
        # One renderer for both the live reserved-slot (live=True, returns a
        # placeholder tr_lbl the worker fills) and static rebuilds (live=False,
        # translation + fact-chips drawn immediately). Responsive: two columns
        # (original | translation) when wide, stacked when narrow/vertical.
        ts_str    = time.strftime("%H:%M", time.localtime(p.ts))
        code_a, code_b = self._pair_codes()
        lang_full = TRANS_NAME.get(p.lang, p.lang.upper() if p.lang else "Unknown")
        lang_col  = ACCENT if p.lang == code_a else (PURPLE if p.lang == code_b else DIM)
        bg_col    = BUBBLE_EN if p.lang == code_a else (BUBBLE_ES if p.lang == code_b else BUBBLE_OTH)
        show_tr   = self.show_tr.get()
        two       = getattr(self, "_twocol", False) and show_tr and not getattr(self, "_narrow", False)

        bubble = ctk.CTkFrame(self.feed, fg_color=bg_col, corner_radius=16,
                              border_width=1, border_color="#1e2742")
        bubble.pack(fill="x", padx=22, pady=(0, 12))
        ctk.CTkFrame(bubble, fg_color=lang_col, width=4, height=10, corner_radius=2
            ).pack(side="left", fill="y", padx=(12,0), pady=12)
        body = ctk.CTkFrame(bubble, fg_color="transparent")
        body.pack(side="left", fill="both", expand=True)

        hdr = ctk.CTkFrame(body, fg_color="transparent")
        hdr.pack(fill="x", padx=14, pady=(12, 4))
        ctk.CTkLabel(hdr, text=f"● {lang_full}", fg_color="transparent",
            text_color=lang_col, font=(F,9,"bold")).pack(side="left", padx=(0,6))
        ctk.CTkLabel(hdr, text=ts_str, fg_color="transparent",
            text_color=DIM, font=(FM,8)).pack(side="left")
        cbtn = ctk.CTkButton(hdr, text="⧉", width=26, height=22,
            fg_color="transparent", hover_color=BG4, text_color=DIM,
            font=(F,10), corner_radius=6,
            command=lambda pid=p.id: self._copy_bubble(pid, "translation"))
        cbtn.pack(side="right")
        cbtn.bind("<Button-3>", lambda e, pid=p.id: self._copy_bubble(pid, "original"))
        eng_lbl = ctk.CTkLabel(hdr, text="", fg_color="transparent",
            text_color=DIM, font=(F,8))
        eng_lbl.pack(side="right", padx=(0,4))

        # Translation OFF → original only, full width
        if not show_tr:
            ctk.CTkLabel(body, text=p.text, fg_color="transparent",
                text_color="#b8c4e0", font=(F,12+self._fs), anchor="w",
                justify="left", wraplength=self._wrap
                ).pack(fill="x", padx=14, pady=(0,10))
            return bubble, None, eng_lbl

        tr_text  = "⟳  Translating…" if live else (p.translation or "")
        tr_color = DIM if (live or not p.translation) else TEXT

        if two:
            content = ctk.CTkFrame(body, fg_color="transparent")
            content.pack(fill="x", padx=14, pady=(0,12))
            content.columnconfigure(0, weight=1, uniform="cc")
            content.columnconfigure(2, weight=1, uniform="cc")
            ctk.CTkLabel(content, text=p.text, fg_color="transparent",
                text_color="#93a3c8", font=(F,12+self._fs), anchor="nw",
                justify="left", wraplength=self._col_wrap
                ).grid(row=0, column=0, sticky="new", padx=(0,12))
            tk.Frame(content, bg="#232c48", width=1).grid(row=0, column=1, sticky="ns")
            tr_col = ctk.CTkFrame(content, fg_color="transparent")
            tr_col.grid(row=0, column=2, sticky="new", padx=(12,0))
            tr_lbl = ctk.CTkLabel(tr_col, text=tr_text, fg_color="transparent",
                text_color=tr_color, font=(F,14+self._fs,"bold"), anchor="nw",
                justify="left", wraplength=self._col_wrap)
            tr_lbl.pack(fill="x", anchor="w")
        else:
            ctk.CTkLabel(body, text=p.text, fg_color="transparent",
                text_color="#93a3c8", font=(F,12+self._fs), anchor="w",
                justify="left", wraplength=self._wrap
                ).pack(fill="x", padx=14, pady=(0,6))
            tr_lbl = ctk.CTkLabel(body, text=tr_text, fg_color="transparent",
                text_color=tr_color, font=(F,15+self._fs,"bold"), anchor="w",
                justify="left", wraplength=self._wrap)
            tr_lbl.pack(fill="x", padx=14, pady=(0,14))

        # Static render → draw fact-chips now (live path adds them on fill)
        if not live and p.translation:
            pairs = _extract_facts(p.text + " " + p.translation)
            if pairs:
                row = ctk.CTkFrame(tr_lbl.master, fg_color="transparent")
                row.pack(fill="x", pady=(0,10), padx=(0 if two else 14))
                for icon, val in pairs[:4]:
                    ctk.CTkButton(row, text=f"{icon} {val}", fg_color=BG4,
                        hover_color=BG3, text_color=ACCENT, font=(FM,9),
                        height=24, corner_radius=6,
                        command=lambda v=val: self._copy_fact(v)
                        ).pack(side="left", padx=(0,6))
        return bubble, tr_lbl, eng_lbl

    def _row_slot(self, p: Phrase):
        _, tr_lbl, eng_lbl = self._bubble_card(p, live=True)
        return tr_lbl, eng_lbl

    def _translate_and_push_bubble(self, text: str, lang: str, conf: float,
                                    ts: float, speaker: int, pid: str, tr_lbl, eng_lbl=None):
        """Background thread: clean text, run Claude, then update the bubble
        slot in-place via root.after so the main thread handles the widget write."""
        domain = self.domain_var.get()
        text   = App._clean_text(text, domain)
        if not text:
            if tr_lbl is not None:
                self.root.after(0, lambda: tr_lbl.configure(text="—", text_color=DIM))
            return

        src = DG_TO_TRANS.get(lang, "en")
        tgt = self._target_for(src)

        # ── Translation memory: exact repeats are free & instant ──
        cached = self.tm.lookup(text, src, tgt, domain)
        if cached:
            tr, engine = cached, "cached"
        else:
            tr, engine = self.translator.claude_translate(
                text, src, tgt, conf,
                context=list(self._context_window), domain=domain,
                summary=self._conversation_summary, glossary=dict(self._glossary))
        if engine == "claude":
            try: self._usage_month()["claude"] += 1
            except Exception: pass
        if tr:
            tr = tr.rstrip()
            if tr[-1] in ',;:':                 # "Seguridad Social,." fix
                tr = tr[:-1].rstrip() + '.'
            elif tr[-1] not in '.!?…':
                tr += '.'
        if not tr: tr = "—"

        # Remember every paid Claude translation — repeats cost $0 forever
        if engine == "claude":
            self.tm.store(text, src, tgt, tr, domain)

        self.root.after(0, lambda: self._fill_bubble(pid, text, tr, lang, speaker,
                                                     tr_lbl, eng_lbl, engine))

    def _fill_bubble(self, pid: str, text: str, tr: str,
                     lang: str, speaker: int, tr_lbl, eng_lbl=None, engine="none"):
        """Main-thread callback: fill a previously reserved bubble slot with
        the completed Claude translation."""
        # Update stored Phrase
        if pid in self.committed:
            self.committed[pid].translation = tr
            self.committed[pid].text = text   # cleaned version

        # Update the translation label in-place
        if tr_lbl is not None:
            try:
                tr_lbl.configure(text=tr, text_color=TEXT)
            except Exception:
                # Widget destroyed (resize rebuild / 120-cap prune / clear).
                # The translation IS stored in committed — re-render soon so
                # it appears instead of silently vanishing.
                if pid in self.committed:
                    if getattr(self, "_refill_job", None):
                        try: self.root.after_cancel(self._refill_job)
                        except Exception: pass
                    self._refill_job = self.root.after(120, self._refill_done)

        self._overlay_last(tr)

        # Engine chip: Claude = purple check, memory hit = instant
        if eng_lbl is not None:
            try:
                if engine == "claude":
                    eng_lbl.configure(text="Claude ✓", text_color=PURPLE)
                elif engine == "cached":
                    eng_lbl.configure(text="⚡ Instant", text_color=GREEN)
            except Exception:
                pass

        # Extract key facts (phones, money, emails, dates, account numbers)
        pairs = _extract_facts(text + " " + (tr or ""))
        if pairs:
            self._add_facts(pairs)
            self._autofill_notes(pairs)
            # small copy-chips inside the bubble too (max 4)
            if tr_lbl is not None:
                try:
                    row = ctk.CTkFrame(tr_lbl.master, fg_color="transparent")
                    row.pack(fill="x", padx=14, pady=(0,10))
                    for icon, val in pairs[:4]:
                        ctk.CTkButton(row, text=f"{icon} {val}",
                            fg_color=BG4, hover_color=BG3, text_color=ACCENT,
                            font=(FM,9), height=24, corner_radius=6,
                            command=lambda v=val: self._copy_fact(v)
                            ).pack(side="left", padx=(0,6))
                except Exception:
                    pass  # bubble destroyed meanwhile

        # Feed rolling context window (now we have the translation)
        self._context_window.append({
            "spk":         speaker,
            "spk_name":    self._speaker_names.get(speaker) if speaker >= 0 else None,
            "lang":        lang,
            "text":        text,
            "translation": tr,
        })

        # Persist to disk
        if pid in self.committed:
            self._session_write(self.committed[pid])

    def _refill_done(self):
        self._refill_job = None
        self._rebuild()

    # ── Text cleanup ─────────────────────────────────────────────
    @staticmethod
    def _clean_text(text: str, domain: str = "Auto") -> str:
        """Normalise text before sending to Claude.

        In Legal and Medical strict modes, stutter/repeat removal is skipped —
        preserving hesitations and uncertainty is critical in those domains.
        """
        strict = domain in ("Legal", "Medical")
        if not strict:
            # Remove consecutive duplicate words only (e.g. "the the meeting" → "the meeting")
            text = re.sub(r'\b(\w+)(\s+\1)+\b', r'\1', text, flags=re.IGNORECASE)
        text = ' '.join(text.split())   # normalise whitespace in all modes
        if not text: return text
        text = text[0].upper() + text[1:]
        if text[-1] in ',;:':
            text = text[:-1].rstrip() + '.'
        elif text[-1] not in '.!?…':
            text += '.'
        return text

    def _maybe_summarize(self):
        """Kick off a background summary of the full session so far.
        Called every 20 bubbles. Skipped if already summarizing or no Claude key."""
        if self._summarizing or not self.translator._claude: return
        self._summarizing = True
        bubbles = list(self.committed.values())
        threading.Thread(
            target=self._generate_summary, args=(bubbles,), daemon=True
        ).start()

    def _generate_summary(self, bubbles: list):
        """Background: ask Claude Haiku to compress the conversation into 2-3 sentences.
        Stored in _conversation_summary; used in every subsequent translation call
        instead of sending the entire transcript."""
        try:
            # Take at most the last 15 bubbles for the summary source
            lines = []
            for b in bubbles[-15:]:
                name = (self._speaker_names.get(b.speaker)
                        or (f"Speaker {b.speaker}" if b.speaker >= 0 else ""))
                prefix = f"{name}: " if name else ""
                lines.append(f"{prefix}[{b.lang.upper()}] {b.text}")
            transcript = "\n".join(lines)
            resp = self.translator._claude.messages.create(
                model="claude-haiku-4-5",
                max_tokens=180,
                temperature=0.0,
                messages=[{"role":"user","content":
                    "Summarize this interpreted conversation in 2-3 sentences. "
                    "Note: who the speakers are, the main topic, key terminology, "
                    "names, organizations, or numbers mentioned. Be concise.\n\n"
                    + transcript}]
            )
            self._conversation_summary = resp.content[0].text.strip()
            self.log.log(f"Memory: {self._conversation_summary[:80]}…","INFO")
        except Exception as e:
            self.log.log(f"Summary failed: {e}","WARN")
        finally:
            self._summarizing = False

    def _session_write(self, p: Phrase):
        """Append a single bubble to the on-disk JSONL session cache."""
        if not self._session_file: return
        try:
            self._session_file.write(json.dumps({
                "id": p.id, "ts": p.ts, "lang": p.lang,
                "speaker": p.speaker, "conf": round(p.conf, 3),
                "text": p.text, "translation": p.translation,
            }, ensure_ascii=False) + "\n")
            self._session_file.flush()
        except: pass

    # ══════════════════════════════════════════════════════════════
    # Bubble rendering
    # ══════════════════════════════════════════════════════════════
    def _row(self, p: Phrase):
        bubble, _, _ = self._bubble_card(p, live=False)
        return bubble

    def _on_search_key(self, event=None):
        # Rebuild 250ms after the last keystroke, not on every one.
        if getattr(self, "_search_job", None):
            try: self.root.after_cancel(self._search_job)
            except Exception: pass
        self._search_job = self.root.after(250, self._rebuild)

    def _on_window_resize(self, event=None):
        if self._resize_job:
            try: self.root.after_cancel(self._resize_job)
            except Exception: pass
        self._resize_job = self.root.after(350, self._apply_wrap)

    def _apply_wrap(self):
        # Recompute wrap widths on resize. IMPORTANT: this never flips the
        # split/stacked LAYOUT (that flip fed a resize->rebuild->resize loop
        # that froze the app). Layout is a user toggle; here we only size text
        # and auto-stack when the window is genuinely too narrow for columns.
        self._resize_job = None
        if self._rebuilding:
            return
        try:
            w = self.feed.winfo_width()
            if w < 50: return
            try:
                scale = float(ctk.ScalingTracker.get_widget_scaling(self.feed))
                if not scale or scale <= 0: scale = 1.0
            except Exception:
                scale = 1.0
            avail     = w / scale
            new_wrap  = max(260, int(avail - 110))
            col_wrap  = max(200, int((avail - 150) / 2))
            live_wrap = max(260, int(avail - 60))
            narrow    = avail < 780
            self.live_confirmed.configure(wraplength=live_wrap)
            self.live_partial.configure(wraplength=live_wrap)
            changed = (abs(new_wrap - self._wrap) > 50
                       or abs(col_wrap - getattr(self, "_col_wrap", 0)) > 50
                       or narrow != self._narrow)
            self._wrap = new_wrap
            self._col_wrap = col_wrap
            self._narrow = narrow
            if changed:
                self._rebuild()
        except Exception:
            pass

    def _toggle_view(self):
        # User flips split/stacked from the sidebar checkbox.
        self._twocol = bool(self.twocol_var.get())
        self._settings["twocol"] = self._twocol
        self._save_settings()
        self._rebuild()

    def _update_empty_state(self):
        """Friendly hint in the feed when there's nothing to show yet."""
        has_bubbles = bool(self.committed)
        if has_bubbles and self._empty_lbl is not None:
            try: self._empty_lbl.destroy()
            except Exception: pass
            self._empty_lbl = None
        elif not has_bubbles and self._empty_lbl is None:
            try:
                wrap = ctk.CTkFrame(self.feed, fg_color="transparent")
                img = self._empty_logo()
                if img is not None:
                    ctk.CTkLabel(wrap, image=img, text="",
                                 fg_color="transparent").pack(pady=(70,18))
                    pad_top = 0
                else:
                    pad_top = 120
                ctk.CTkLabel(wrap,
                    text=("Your conversation will appear here\n\n"
                          "▶  Press  ⏺ Start Listening  when your call begins"),
                    fg_color="transparent", text_color=DIM, font=(F,13),
                    justify="center").pack(pady=(pad_top,0))
                wrap.pack()
                self._empty_lbl = wrap
            except Exception:
                self._empty_lbl = None

    def _empty_logo(self):
        """Dimmed 72px Sayvo mark for the empty feed. Cached; None on failure."""
        if hasattr(self, "_empty_logo_img"):
            return self._empty_logo_img
        self._empty_logo_img = None
        try:
            from PIL import Image
            p = self._icon_path()
            if p:
                im = Image.open(p)
                try: im.size = (256, 256)   # pick the largest ico frame
                except Exception: pass
                im = im.convert("RGBA").resize((72, 72), Image.LANCZOS)
                a = im.getchannel("A").point(lambda v: int(v * 0.35))
                im.putalpha(a)
                self._empty_logo_img = ctk.CTkImage(im, size=(72, 72))
        except Exception:
            self._empty_logo_img = None
        return self._empty_logo_img

    def _rebuild(self):
        if self._rebuilding:      # never re-enter (kills any resize loop)
            return
        self._rebuilding = True
        try:
            try:    q = self.search_entry.get().strip().lower()
            except Exception: q = ""
            for w in self.feed.winfo_children(): w.destroy()
            matches = [p for p in self.committed.values()
                       if not q or q in p.text.lower()
                       or q in (p.translation or "").lower()]
            self._empty_lbl = None   # destroyed with the other children above
            for p in matches[-80:]:    # keep the newest 80 on screen (perf)
                self._row(p)
            self._update_empty_state()
            # ORIGINAL | TRANSLATION header only when actually two-column
            two = (self._twocol and self.show_tr.get() and not self._narrow)
            try:
                if two:
                    self.col_hdr.pack(fill="x", padx=40, pady=(2,0), before=self.feed)
                else:
                    self.col_hdr.pack_forget()
            except Exception: pass
            if self.autoscroll.get(): self._scroll_feed_to_bottom()
        finally:
            self._rebuilding = False

    # ── Glossary manager ─────────────────────────────────────────
    def _open_glossary(self):
        win = ctk.CTkToplevel(self.root)
        win.title("Glossary Manager")
        self._apply_icon(win)
        win.geometry("540x500"); win.resizable(False, False)
        win.configure(fg_color=BG)
        win.grab_set()   # modal

        ctk.CTkLabel(win, text="Custom Glossary",
            fg_color="transparent", text_color=TEXT, font=(F,14,"bold")
        ).pack(anchor="w", padx=22, pady=(18,2))
        ctk.CTkLabel(win,
            text="Rules listed here are injected into every Claude translation call.",
            fg_color="transparent", text_color=DIM, font=(F,9)
        ).pack(anchor="w", padx=22, pady=(0,12))

        # ── Scrollable term list ──────────────────────────────────
        scroll = ctk.CTkScrollableFrame(win, fg_color=BG2, corner_radius=10)
        scroll.pack(fill="both", expand=True, padx=22, pady=(0,10))

        def refresh():
            for w in scroll.winfo_children(): w.destroy()
            if not self._glossary:
                ctk.CTkLabel(scroll, text="No terms yet — add one below.",
                    fg_color="transparent", text_color=DIM,
                    font=(F,10)).pack(pady=24)
                return
            for term, rule in list(self._glossary.items()):
                row = ctk.CTkFrame(scroll, fg_color=BG3, corner_radius=8)
                row.pack(fill="x", pady=3, padx=6)
                ctk.CTkLabel(row, text=term, fg_color="transparent",
                    text_color=ACCENT, font=(F,10,"bold"),
                    width=90, anchor="w").pack(side="left", padx=(12,6), pady=10)
                ctk.CTkLabel(row, text=rule, fg_color="transparent",
                    text_color=TEXT, font=(F,9), anchor="w",
                    wraplength=300, justify="left").pack(
                        side="left", fill="x", expand=True, pady=10)
                ctk.CTkButton(row, text="✕", width=28, height=28,
                    fg_color="transparent", hover_color=RED,
                    text_color=DIM, font=(F,9,"bold"), corner_radius=4,
                    command=lambda t=term: [
                        self._glossary.pop(t, None),
                        self._save_glossary(), refresh()
                    ]).pack(side="right", padx=8)

        refresh()

        # ── Add new term ──────────────────────────────────────────
        add = ctk.CTkFrame(win, fg_color=BG2, corner_radius=10)
        add.pack(fill="x", padx=22, pady=(0,10))

        term_var = tk.StringVar()
        rule_var = tk.StringVar()

        tf = ctk.CTkFrame(add, fg_color="transparent")
        tf.pack(fill="x", padx=12, pady=(12,6))
        ctk.CTkLabel(tf, text="Term", fg_color="transparent",
            text_color=DIM, font=(F,8), width=46, anchor="w").pack(side="left")
        ctk.CTkEntry(tf, textvariable=term_var, placeholder_text="e.g.  MRI",
            fg_color=BG3, text_color=TEXT, border_width=0,
            font=(F,10), corner_radius=6).pack(side="left", fill="x", expand=True)

        rf = ctk.CTkFrame(add, fg_color="transparent")
        rf.pack(fill="x", padx=12, pady=(0,6))
        ctk.CTkLabel(rf, text="Rule", fg_color="transparent",
            text_color=DIM, font=(F,8), width=46, anchor="w").pack(side="left")
        ctk.CTkEntry(rf, textvariable=rule_var,
            placeholder_text="e.g.  always write 'MRI' — do not translate",
            fg_color=BG3, text_color=TEXT, border_width=0,
            font=(F,10), corner_radius=6).pack(side="left", fill="x", expand=True)

        def add_term():
            t = term_var.get().strip(); r = rule_var.get().strip()
            if t and r:
                self._glossary[t] = r
                self._save_glossary()
                term_var.set(""); rule_var.set("")
                refresh()

        ctk.CTkButton(add, text="Add Term", fg_color=ACCENT,
            hover_color=ACCENT_HOV, text_color="white",
            font=(F,10,"bold"), height=36, corner_radius=8,
            command=add_term).pack(fill="x", padx=12, pady=(0,12))

        ctk.CTkButton(win, text="Close", fg_color=BG2, hover_color=BG3,
            text_color=DIM, font=(F,10), height=36, corner_radius=8,
            command=win.destroy).pack(fill="x", padx=22, pady=(0,18))

    def _on_textsize(self, *_):
        self._fs = {"Normal": 0, "Large": 2, "Extra Large": 5}.get(
            self.textsize_var.get(), 0)
        fs = self._fs
        try:
            self.live_confirmed.configure(font=(F, 12+fs))
            self.live_partial.configure(font=(F, 12+fs))
        except Exception: pass
        self._rebuild()
        self._save_settings()

    def _toggle_privacy(self):
        self._privacy = not self._privacy
        if self._privacy:
            self.privacy_btn.configure(text="▶  Resume", fg_color=RED,
                hover_color=RED_HOV, text_color="white")
            self.hdr_status.configure(text="⏸ Privacy pause — audio muted",
                                      text_color=YELLOW)
            self.live_partial.configure(text="Privacy pause — nothing is being transcribed…",
                                        text_color=YELLOW)
        else:
            self.privacy_btn.configure(text="⏸  Privacy", fg_color=BG4,
                hover_color=BG3, text_color=DIM)
            self.hdr_status.configure(
                text="Recording" if self.recording else "Ready",
                text_color=GREEN if self.recording else DIM)
            self.live_partial.configure(text="", text_color=DIM)
            self._last_audio_t = time.time()   # don't trip the watchdog

    def _on_crash(self, exc_type, exc, tb):
        """Unhandled-exception hook: save a crash report, log it, and keep
        running. Shows one friendly dialog per session (not one per error)."""
        import traceback
        txt = "".join(traceback.format_exception(exc_type, exc, tb))
        try: self.log.log(f"CRASH: {exc}", "ERROR")
        except Exception: pass
        path = None
        try:
            d = Path.home()/".interpro"/"crashes"
            d.mkdir(parents=True, exist_ok=True)
            path = d / f"crash_{time.strftime('%Y%m%d_%H%M%S')}.txt"
            path.write_text(txt, encoding="utf-8")
        except Exception: pass
        if not self._crash_dialog_shown:
            self._crash_dialog_shown = True
            try:
                messagebox.showwarning("Something went wrong",
                    "Sayvo hit an internal error but is still running.\n"
                    "If anything looks stuck, press Stop then Start.\n\n"
                    + (f"Details saved to:\n{path}" if path else ""))
            except Exception: pass

    def _copy_bubble(self, pid: str, which: str = "translation"):
        """Copy one side of a bubble. Left-click = translation,
        right-click = original transcript."""
        p = self.committed.get(pid)
        if not p: return
        if which == "original" or not p.translation:
            blob, label = p.text, "original"
        else:
            blob, label = p.translation, "translation"
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(blob)
            self.hdr_status.configure(text=f"Copied {label} ✓", text_color=GREEN)
        except Exception:
            pass
    def _toggle_overlay(self):
        if getattr(self, "_overlay", None):
            try: self._overlay.destroy()
            except Exception: pass
            self._overlay = None
            return
        ov = ctk.CTkToplevel(self.root)
        ov.title("Sayvo — Live")
        self._apply_icon(ov)
        ov.attributes("-topmost", True)
        ov.attributes("-alpha", float(self._settings.get("overlay_alpha", 0.94)))
        ov.configure(fg_color=BG2)
        # bottom-right corner of the primary screen
        sw, sh = ov.winfo_screenwidth(), ov.winfo_screenheight()
        ov.geometry(f"480x190+{sw-500}+{sh-280}")
        top = ctk.CTkFrame(ov, fg_color="transparent")
        top.pack(fill="x", padx=14, pady=(10,0))
        ctk.CTkLabel(top, text="● LIVE", fg_color="transparent",
            text_color=ACCENT, font=(F,9,"bold")).pack(side="left")
        ctk.CTkButton(top, text="⬇ Flush", width=64, height=22,
            fg_color=ACCENT, hover_color=ACCENT_HOV, text_color="white",
            font=(F,8,"bold"), corner_radius=6,
            command=self._manual_flush).pack(side="right")
        self.ov_live = ctk.CTkLabel(ov, text="", fg_color="transparent",
            text_color=TEXT, font=(F,16,"bold"), anchor="nw", justify="left",
            wraplength=440)
        self.ov_live.pack(fill="both", expand=True, padx=14, pady=(6,2))
        self.ov_last = ctk.CTkLabel(ov, text="", fg_color="transparent",
            text_color=DIM, font=(F,11), anchor="w", justify="left",
            wraplength=440)
        self.ov_last.pack(fill="x", padx=14, pady=(0,4))
        def _set_alpha(v):
            try: ov.attributes("-alpha", float(v))
            except Exception: pass
            self._settings["overlay_alpha"] = round(float(v), 2)
        sl = ctk.CTkSlider(ov, from_=0.35, to=1.0, height=12,
            fg_color=BG4, progress_color=ACCENT, button_color=ACCENT,
            command=_set_alpha)
        sl.set(float(self._settings.get("overlay_alpha", 0.94)))
        sl.pack(fill="x", padx=14, pady=(0,10))
        def _closed():
            self._overlay = None
            try: ov.destroy()
            except Exception: pass
        ov.protocol("WM_DELETE_WINDOW", _closed)
        self._overlay = ov

    def _overlay_speech(self, text):
        """Overlay small line: what's being said right now (transcript)."""
        if getattr(self, "_overlay", None):
            try: self.ov_last.configure(text=text)
            except Exception: pass

    def _overlay_last(self, text):
        """Overlay big text: the latest finished translation."""
        if getattr(self, "_overlay", None):
            try: self.ov_live.configure(text=text)
            except Exception: pass

    # ══════════════════════════════════════════════════════════════
    # First-run setup wizard
    # ══════════════════════════════════════════════════════════════
    def _show_wizard(self):
        win = ctk.CTkToplevel(self.root)
        win.title("Welcome to Sayvo")
        self._apply_icon(win)
        win.geometry("620x760"); win.configure(fg_color=BG)
        win.grab_set()

        body = ctk.CTkScrollableFrame(win, fg_color="transparent",
            scrollbar_button_color=BG3, scrollbar_button_hover_color=BG4)
        body.pack(fill="both", expand=True, padx=26, pady=(22,0))

        ctk.CTkLabel(body, text="Welcome to Sayvo 👋",
            fg_color="transparent", text_color=TEXT,
            font=(F,19,"bold")).pack(anchor="w")
        ctk.CTkLabel(body, text="Real-time interpretation for your calls. Let's set you up in one minute.",
            fg_color="transparent", text_color=DIM, font=(F,11)).pack(anchor="w", pady=(2,18))

        def sec(title, sub=""):
            ctk.CTkLabel(body, text=title, fg_color="transparent",
                text_color=ACCENT, font=(F,11,"bold")).pack(anchor="w", pady=(10,2))
            if sub:
                ctk.CTkLabel(body, text=sub, fg_color="transparent",
                    text_color=DIM, font=(F,9), wraplength=520,
                    justify="left").pack(anchor="w", pady=(0,6))

        # STEP 1 — Deepgram key
        sec("STEP 1 — Deepgram API key (required)",
            "Powers the speech-to-text. Create a free key at console.deepgram.com — new accounts include free credit.")
        ctk.CTkEntry(body, textvariable=self.api_key_var, show="●",
            fg_color=BG3, text_color=TEXT, border_width=0, font=(F,10),
            corner_radius=8, height=36,
            placeholder_text="Paste your Deepgram key here…").pack(fill="x")

        # STEP 2 — Claude key
        sec("STEP 2 — Claude API key (required for translation)",
            "Sayvo uses Claude to translate. Get a key at console.anthropic.com. Without it, Sayvo only transcribes.")
        ctk.CTkEntry(body, textvariable=self.anthropic_key_var, show="●",
            fg_color=BG3, text_color=TEXT, border_width=0, font=(F,10),
            corner_radius=8, height=36,
            placeholder_text="Paste your Anthropic key here…").pack(fill="x")

        # STEP 3 — Audio source + test
        sec("STEP 3 — Where is your call audio?",
            "Pick the device you LISTEN with — the '🔊 what you hear' options capture everything playing on it (never your microphone).")
        wiz_combo = ttk.Combobox(body, state="readonly",
            style="IP.TCombobox", font=(F,9))
        wiz_combo["values"] = self.dev_combo["values"]
        try: wiz_combo.current(self.dev_combo.current())
        except: pass
        wiz_combo.pack(fill="x")
        test_lbl = ctk.CTkLabel(body, text="Play some audio (music, a video…), then press Test.",
            fg_color="transparent", text_color=DIM, font=(F,9))
        ctk.CTkButton(body, text="▶  Test Audio (2 s)", fg_color=BG3,
            hover_color=BG4, text_color=GREEN, font=(F,10,"bold"), height=34,
            corner_radius=8,
            command=lambda: self._wiz_test_audio(wiz_combo, test_lbl)
            ).pack(anchor="w", pady=(8,2))
        test_lbl.pack(anchor="w", pady=(2,0))

        # STEP 4 — Consent notice
        sec("STEP 4 — One legal thing",
            "Sayvo transcribes call audio. Many jurisdictions — including Florida and about a dozen other US states — require the consent of everyone on a call before it is recorded or transcribed. Complying with the laws where you work is your responsibility.")
        consent = tk.BooleanVar(value=False)
        finish_btn = ctk.CTkButton(win, text="Finish Setup  ✓", fg_color=ACCENT,
            hover_color=ACCENT_HOV, text_color="white", font=(F,12,"bold"),
            height=44, corner_radius=10, state="disabled",
            command=lambda: self._wiz_finish(win, wiz_combo))
        ctk.CTkCheckBox(body, text="I understand and accept this responsibility",
            variable=consent, fg_color=ACCENT, hover_color=BG4,
            text_color=TEXT, font=(F,10), corner_radius=4, border_color=BORDER,
            checkmark_color="white",
            command=lambda: finish_btn.configure(
                state="normal" if consent.get() else "disabled")
            ).pack(anchor="w", pady=(6,14))

        finish_btn.pack(fill="x", padx=26, pady=(8,6))
        def _skip():
            # Don't nag on every launch — setup can be re-run from ⚙ Settings
            self._settings["wizard_done"] = True
            self._save_settings()
            win.destroy()
        ctk.CTkButton(win, text="Skip — I'll set up later (⚙ Settings)",
            fg_color="transparent", hover_color=BG3, text_color=DIM,
            font=(F,9), height=28, corner_radius=8,
            command=_skip).pack(pady=(0,14))

    def _wiz_test_audio(self, wiz_combo, result_lbl):
        i = wiz_combo.current()
        if not (0 <= i < len(self.devices)):
            result_lbl.configure(text="Select a device first.", text_color=YELLOW); return
        dev = self.devices[i]
        result_lbl.configure(text="Listening for 2 seconds…", text_color=DIM)
        def run():
            peak = {"v": 0.0}
            def on_chunk(c):
                try: peak["v"] = max(peak["v"], float(np.abs(c).max()))
                except Exception: pass
            try:
                cap = AudioCapture(dev["index"], on_chunk, lambda *a: None, self.log,
                                   prefer_direct=dev.get("is_loopback", False),
                                   output_loopback=dev.get("is_output_loopback", False))
                cap.start(); time.sleep(2.2); cap.stop()
                v = peak["v"]
                if v > 0.01:
                    msg, col = "✓ Audio detected — you're all set!", GREEN
                elif v > 0.0005:
                    msg, col = "~ Very quiet — turn the volume up and test again.", YELLOW
                else:
                    msg, col = "✗ Silence. Make sure audio is playing on this device, then retest.", RED
            except Exception as e:
                msg, col = f"✗ Could not open device: {e}", RED
            self.root.after(0, lambda: result_lbl.configure(text=msg, text_color=col))
        threading.Thread(target=run, daemon=True).start()

    def _wiz_finish(self, win, wiz_combo):
        k = self.api_key_var.get().strip()
        if k: self._save_key_file(k)
        ak = self.anthropic_key_var.get().strip()
        if ak:
            self._save_key_file(ak, "anthropic.key")
            self.translator.set_anthropic_key(ak)
        i = wiz_combo.current()
        if 0 <= i < len(self.devices):
            self.dev_combo.current(i); self._on_dev()
        self._settings["wizard_done"] = True
        self._save_settings()
        win.destroy()
        self.hdr_status.configure(text="Setup complete ✓", text_color=GREEN)

    # ══════════════════════════════════════════════════════════════
    # Settings dialog (API keys — rarely touched, out of the sidebar)
    # ══════════════════════════════════════════════════════════════
    def _open_settings(self):
        win = ctk.CTkToplevel(self.root)
        win.title("Settings"); self._apply_icon(win)
        win.geometry("520x400"); win.configure(fg_color=BG); win.grab_set()
        ctk.CTkLabel(win, text="⚙  Settings", fg_color="transparent",
            text_color=TEXT, font=(F,15,"bold")).pack(anchor="w", padx=26, pady=(20,14))

        def key_row(title, hint, var, save_cmd, accent):
            ctk.CTkLabel(win, text=title, fg_color="transparent",
                text_color=accent, font=(F,10,"bold")).pack(anchor="w", padx=26, pady=(6,3))
            row = ctk.CTkFrame(win, fg_color=BG3, corner_radius=10)
            row.pack(fill="x", padx=26)
            e = ctk.CTkEntry(row, textvariable=var, show="●",
                fg_color=BG3, text_color=TEXT, border_width=0, font=(F,10),
                corner_radius=10)
            e.pack(side="left", fill="x", expand=True, ipady=4, padx=(10,0))
            ctk.CTkButton(row, text="Save", fg_color="transparent",
                hover_color=BG4, text_color=accent, font=(F,9,"bold"),
                width=56, height=34, corner_radius=8,
                command=save_cmd).pack(side="left", padx=6)
            ctk.CTkLabel(win, text=hint, fg_color="transparent",
                text_color=DIM, font=(F,8)).pack(anchor="w", padx=26, pady=(3,4))

        key_row("DEEPGRAM API KEY  (required — speech to text)",
                "Get a free key at console.deepgram.com",
                self.api_key_var, self._save_key, ACCENT)
        key_row("CLAUDE API KEY  (required — translation)",
                "console.anthropic.com — without it Sayvo only transcribes",
                self.anthropic_key_var, self._save_anthropic_key, PURPLE)

        ctk.CTkButton(win, text="🧙  Run Setup Wizard again",
            fg_color=BG3, hover_color=BG4, text_color=TEXT, font=(F,10),
            height=36, corner_radius=10,
            command=lambda: (win.destroy(), self._show_wizard())
            ).pack(fill="x", padx=26, pady=(14,0))
        ctk.CTkButton(win, text="Close", fg_color=BG2, hover_color=BG3,
            text_color=DIM, font=(F,10), height=36, corner_radius=10,
            command=win.destroy).pack(fill="x", padx=26, pady=(10,6))
        ctk.CTkLabel(win, text=f"Sayvo v{__version__}",
            fg_color="transparent", text_color=DIM, font=(F,8)
            ).pack(pady=(0,10))

    # ══════════════════════════════════════════════════════════════
    # One-click call summary
    # ══════════════════════════════════════════════════════════════
    def _call_summary(self):
        if not self.committed:
            messagebox.showinfo("Nothing yet", "Record a conversation first."); return
        if not self.translator._claude:
            messagebox.showinfo("Claude key needed",
                "Add your Anthropic API key (sidebar) to generate call summaries.")
            return
        win = ctk.CTkToplevel(self.root)
        win.title("Call Summary"); self._apply_icon(win)
        win.geometry("640x560"); win.configure(fg_color=BG); win.grab_set()
        ctk.CTkLabel(win, text="📄 Call Summary", fg_color="transparent",
            text_color=TEXT, font=(F,15,"bold")).pack(anchor="w", padx=24, pady=(18,8))
        box = ctk.CTkTextbox(win, fg_color=BG2, text_color=TEXT, font=(F,12),
            corner_radius=12, wrap="word")
        box.pack(fill="both", expand=True, padx=22, pady=(0,10))
        box.insert("1.0", "⟳  Reading the conversation and writing your summary…")
        box.configure(state="disabled")
        def copy_all():
            try:
                self.root.clipboard_clear()
                self.root.clipboard_append(box.get("1.0", "end").strip())
                self.hdr_status.configure(text="Summary copied ✓", text_color=GREEN)
            except Exception: pass
        ctk.CTkButton(win, text="⧉  Copy Summary", fg_color=ACCENT,
            hover_color=ACCENT_HOV, text_color="white", font=(F,11,"bold"),
            height=40, corner_radius=10, command=copy_all
            ).pack(fill="x", padx=22, pady=(0,18))

        bubbles = list(self.committed.values())
        def run():
            try:
                lines = []
                for b in bubbles[-120:]:
                    lines.append(f"[{TRANS_NAME.get(b.lang, b.lang)}] {b.text}")
                    if b.translation:
                        lines.append(f"   -> {b.translation}")
                prompt = (
                    "This is the transcript of an interpreted phone call "
                    "(original lines with their translations). Write:\n"
                    "1. SUMMARY — 3-5 sentences of what the call was about and "
                    "what was decided.\n"
                    "2. KEY DETAILS — every name, phone number, amount, date, "
                    "account/reference number mentioned, one per line.\n"
                    "3. ACTION ITEMS — what each side committed to do, "
                    "one per line. If none, say 'None'.\n"
                    "Plain text, no markdown symbols.\n\n" + "\n".join(lines))
                resp = self.translator._claude.messages.create(
                    model="claude-haiku-4-5",
                    max_tokens=700,
                    temperature=0.2,
                    messages=[{"role": "user", "content": prompt}]
                )
                out = resp.content[0].text.strip()
            except Exception as e:
                out = f"Could not generate the summary:\n{e}"
            def show():
                try:
                    box.configure(state="normal")
                    box.delete("1.0", "end"); box.insert("1.0", out)
                    box.configure(state="disabled")
                except Exception: pass
            self.root.after(0, show)
        threading.Thread(target=run, daemon=True).start()

    # ══════════════════════════════════════════════════════════════
    # Session history browser
    # ══════════════════════════════════════════════════════════════
    def _session_meta(self, f):
        # (epoch_start, n_segments, duration_sec) for one session file.
        try:
            lines = [l for l in f.read_text(encoding="utf-8").splitlines() if l.strip()]
        except Exception:
            lines = []
        n = len(lines)
        try:
            dt = time.mktime(time.strptime(f.stem.replace("session_",""), "%Y%m%d_%H%M%S"))
        except Exception:
            dt = f.stat().st_mtime if f.exists() else time.time()
        dur = 0.0
        if n >= 2:
            try:
                first = json.loads(lines[0]).get("ts", dt)
                last  = json.loads(lines[-1]).get("ts", dt)
                dur = max(0.0, float(last) - float(first))
            except Exception:
                dur = 0.0
        return dt, n, dur

    @staticmethod
    def _fmt_dur(sec):
        sec = int(sec)
        if sec < 60: return f"{sec}s"
        m, s = divmod(sec, 60)
        if m < 60: return f"{m}m"
        h, m = divmod(m, 60)
        return f"{h}h {m}m"

    def _open_sessions(self):
        win = ctk.CTkToplevel(self.root)
        win.title("Session History")
        self._apply_icon(win)
        win.geometry("680x600"); win.configure(fg_color=BG)
        win.grab_set()
        ctk.CTkLabel(win, text="Session History",
            fg_color="transparent", text_color=TEXT,
            font=(F,15,"bold")).pack(anchor="w", padx=24, pady=(18,2))
        ctk.CTkLabel(win, text="Every recording is auto-saved. Open one to review it in the feed.",
            fg_color="transparent", text_color=DIM, font=(F,9)).pack(anchor="w", padx=24, pady=(0,8))

        sdir  = Path.home()/".interpro"/"sessions"
        files = sorted(sdir.glob("session_*.jsonl"), reverse=True) if sdir.exists() else []
        meta  = []
        for f in files[:200]:
            dt, n, dur = self._session_meta(f)
            if n > 0:
                meta.append((f, dt, n, dur))

        period_var = tk.StringVar(value="Today")
        tabs = ctk.CTkSegmentedButton(win, values=["Today","Week","Month","All"],
            variable=period_var, fg_color=BG2, selected_color=ACCENT,
            selected_hover_color=ACCENT_HOV, unselected_color=BG3,
            unselected_hover_color=BG4, text_color=TEXT, font=(F,10),
            command=lambda *_: render())
        tabs.pack(fill="x", padx=22, pady=(4,8))

        stats = ctk.CTkFrame(win, fg_color=BG2, corner_radius=12)
        stats.pack(fill="x", padx=22, pady=(0,8))
        stats.columnconfigure((0,1,2), weight=1)
        def stat_tile(col, accent):
            f = ctk.CTkFrame(stats, fg_color="transparent")
            f.grid(row=0, column=col, sticky="nsew", pady=12)
            v = ctk.CTkLabel(f, text="0", fg_color="transparent",
                text_color=accent, font=(F,18,"bold")); v.pack()
            k = ctk.CTkLabel(f, text="", fg_color="transparent",
                text_color=DIM, font=(F,8)); k.pack()
            return v, k
        sv_n, sk_n = stat_tile(0, TEXT)
        sv_t, sk_t = stat_tile(1, ACCENT)
        sv_a, sk_a = stat_tile(2, PURPLE)
        sk_n.configure(text="Sessions"); sk_t.configure(text="Total time"); sk_a.configure(text="Avg / session")

        scroll = ctk.CTkScrollableFrame(win, fg_color=BG2, corner_radius=12)
        scroll.pack(fill="both", expand=True, padx=22, pady=(0,18))

        def in_period(dt, period):
            now = time.time()
            if period == "All":   return True
            if period == "Today": return time.strftime("%Y%m%d") == time.strftime("%Y%m%d", time.localtime(dt))
            if period == "Week":  return dt >= now - 7*86400
            if period == "Month": return dt >= now - 30*86400
            return True

        def render():
            for w in scroll.winfo_children(): w.destroy()
            period = period_var.get()
            sel = [m for m in meta if in_period(m[1], period)]
            total = sum(m[3] for m in sel)
            n_sess = len(sel)
            avg = (total / n_sess) if n_sess else 0
            sv_n.configure(text=str(n_sess))
            sv_t.configure(text=self._fmt_dur(total))
            sv_a.configure(text=self._fmt_dur(avg))
            if not sel:
                ctk.CTkLabel(scroll, text="No sessions in this period.",
                    fg_color="transparent", text_color=DIM, font=(F,10)).pack(pady=30)
                return
            for f, dt, n, dur in sel:
                when = time.strftime("%b %d, %Y  %H:%M", time.localtime(dt))
                row = ctk.CTkFrame(scroll, fg_color=BG3, corner_radius=10)
                row.pack(fill="x", pady=4, padx=8)
                left = ctk.CTkFrame(row, fg_color="transparent")
                left.pack(side="left", padx=(14,8), pady=10)
                ctk.CTkLabel(left, text=when, fg_color="transparent",
                    text_color=TEXT, font=(F,11,"bold")).pack(anchor="w")
                ctk.CTkLabel(left, text=f"{n} segments · {self._fmt_dur(dur)}",
                    fg_color="transparent", text_color=DIM, font=(F,9)).pack(anchor="w")
                def _del(pf=f):
                    try: pf.unlink(missing_ok=True)
                    except Exception: pass
                    meta[:] = [m for m in meta if m[0] != pf]
                    render()
                ctk.CTkButton(row, text="✕", width=30, height=28,
                    fg_color="transparent", hover_color=RED, text_color=DIM,
                    font=(F,10,"bold"), corner_radius=6, command=_del
                    ).pack(side="right", padx=(4,10))
                ctk.CTkButton(row, text="Open", width=64, height=28,
                    fg_color=ACCENT, hover_color=ACCENT_HOV, text_color="white",
                    font=(F,9,"bold"), corner_radius=6,
                    command=lambda p=f: self._load_session(p, win)
                    ).pack(side="right", padx=4)
        render()

    def _load_session(self, path, win=None):
        if self.recording:
            messagebox.showwarning("Recording active",
                "Stop the current recording before loading a past session.")
            return
        try:
            rows = []
            for l in path.read_text(encoding="utf-8").splitlines():
                if not l.strip(): continue
                try: rows.append(json.loads(l))
                except Exception: pass   # one corrupt line must not kill the load
        except Exception as e:
            messagebox.showerror("Error", f"Could not read session:\n{e}"); return
        if not rows:
            messagebox.showinfo("Empty", "That session has no saved segments."); return
        if self.committed and not messagebox.askyesno(
                "Load Session", "Replace the conversation currently on screen?"):
            return
        # Quiet clear (no confirmation dialog — user already confirmed)
        self.committed.clear()
        for w in self.feed.winfo_children(): w.destroy()
        self._reset_feed_view()
        # Namespace loaded ids by file stem — original "para_0001" ids would
        # collide with this session's fresh bubble counter and overwrite.
        stem = path.stem.replace("session_", "")
        for n, d in enumerate(rows):
            p = Phrase(id=f"h{stem}_{n:04d}",
                       text=d.get("text",""), translation=d.get("translation",""),
                       lang=d.get("lang","en"), conf=float(d.get("conf",1.0)),
                       ts=float(d.get("ts", time.time())), partial=False,
                       speaker=int(d.get("speaker",-1)))
            self.committed[p.id] = p
        # A loaded conversation is history — don't let the previous call's
        # summary/context/facts leak into future translations.
        self._conversation_summary = ""
        self._context_window.clear()
        self._clear_facts()
        self._empty_lbl = None
        for p in list(self.committed.values())[-80:]:    # keep newest 80 (perf)
            self._row(p)
        self._update_empty_state()
        self.seg_count  = len(rows)
        self.word_count = sum(len(d.get("text","").split()) for d in rows)
        self.sv_segs.configure(text=str(self.seg_count))
        self.sv_words.configure(text=str(self.word_count))
        self._scroll_feed_to_bottom()
        if win:
            try: win.destroy()
            except Exception: pass

    # ── Export / Clear / Quit ────────────────────────────────────
    # ── Export helpers ────────────────────────────────────────────
    @staticmethod
    def _srt_ts(ms: int) -> str:
        h, r  = divmod(ms, 3_600_000)
        m, r  = divmod(r,     60_000)
        s, ms = divmod(r,      1_000)
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

    @staticmethod
    def _vtt_ts(ms: int) -> str:
        h, r  = divmod(ms, 3_600_000)
        m, r  = divmod(r,     60_000)
        s, ms = divmod(r,      1_000)
        return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"

    def _export(self):
        if not self.committed:
            messagebox.showinfo("Empty", "Record first."); return
        path = filedialog.asksaveasfilename(
            defaultextension=".txt",
            filetypes=[
                ("Plain Text",     "*.txt"),
                ("SRT Subtitles",  "*.srt"),
                ("VTT Captions",   "*.vtt"),
                ("All files",      "*.*"),
            ],
            initialfile=f"sayvo_{time.strftime('%Y%m%d_%H%M%S')}.txt")
        if not path: return
        ext = Path(path).suffix.lower()
        if   ext == ".srt": self._export_srt(path)
        elif ext == ".vtt": self._export_vtt(path)
        else:               self._export_txt(path)
        messagebox.showinfo("Exported", f"Saved:\n{path}")

    def _export_txt(self, path):
        lines = ["Sayvo Transcript",
                 f"Generated: {time.strftime('%Y-%m-%d %H:%M:%S')}", "="*60, ""]
        for p in self.committed.values():
            ts = time.strftime("%H:%M:%S", time.localtime(p.ts))
            lines.append(f"[{ts}] [{p.lang.upper()}]  {p.text}")
            if p.translation: lines.append(f"             -> {p.translation}")
            lines.append("")
        if self._facts:
            lines += ["=" * 60, "KEY FACTS", "-" * 60]
            lines += [f"  {icon}  {val}" for val, icon in self._facts.items()]
            lines.append("")
        notes = self._notes_snapshot()
        if notes:
            lines += ["=" * 60, "NOTES", "-" * 60]
            lines += notes
            lines.append("")
        with open(path, "w", encoding="utf-8") as f: f.write("\n".join(lines))

    def _subtitle_times(self, p, t0_abs: float):
        """Return (start_ms, end_ms) for a bubble.
        Uses Deepgram's actual word timestamps when available,
        falls back to word-count estimation otherwise."""
        t0 = int((p.ts - t0_abs) * 1000)
        if p.ts_end > 0:
            t1 = int((p.ts_end - t0_abs) * 1000)
        else:
            t1 = t0 + max(2000, len(p.text.split()) * 400)
        return t0, t1

    def _export_srt(self, path):
        items  = list(self.committed.values())
        t0_abs = items[0].ts if items else time.time()
        lines  = []
        for i, p in enumerate(items, 1):
            t0, t1 = self._subtitle_times(p, t0_abs)
            lines += [str(i),
                      f"{self._srt_ts(t0)} --> {self._srt_ts(t1)}",
                      p.text]
            if p.translation: lines.append(p.translation)
            lines.append("")
        with open(path, "w", encoding="utf-8") as f: f.write("\n".join(lines))

    def _export_vtt(self, path):
        items  = list(self.committed.values())
        t0_abs = items[0].ts if items else time.time()
        lines  = ["WEBVTT", ""]
        for i, p in enumerate(items, 1):
            t0, t1 = self._subtitle_times(p, t0_abs)
            lines += [str(i),
                      f"{self._vtt_ts(t0)} --> {self._vtt_ts(t1)}",
                      p.text]
            if p.translation: lines.append(p.translation)
            lines.append("")
        with open(path, "w", encoding="utf-8") as f: f.write("\n".join(lines))

    def _clear(self):
        if not messagebox.askyesno("Clear","Delete all segments?"): return
        # Cancel any pending flush timers so they don't fire on stale state
        if self._grace_timer:
            self.root.after_cancel(self._grace_timer); self._grace_timer = None
        if self._para_timer:
            self._para_timer.cancel(); self._para_timer = None
        with self._para_lock:
            self._para_text = ""; self._para_conf = 0.0
            self._para_ts = None; self._para_speaker = -1
        self._current_partial = ""
        self._clear_facts()
        self.committed.clear()
        self.seg_count = self.word_count = 0
        self._context_window.clear()
        self._conversation_summary = ""
        self._speaker_names.clear()
        for w in self.feed.winfo_children(): w.destroy()
        self._reset_feed_view()
        self.root.after(250, self._reset_feed_view)   # catch the late shrink
        self._empty_lbl = None
        self._update_empty_state()
        self.sv_segs.configure(text="0")
        self.sv_words.configure(text="0")
        self.sv_lang.configure(text="—")
        self.live_confirmed.configure(text="")
        self.live_partial.configure(text="Waiting for speech…", text_color=TEXT)
        self._overlay_speech("")
        if self.recording:
            self.session_start = time.time(); self.sv_time.configure(text="0:00")

    # ── Monthly usage tracking ───────────────────────────────────
    def _usage_month(self):
        key = time.strftime("%Y-%m")
        u = self._settings.setdefault("usage", {})
        return u.setdefault(key, {"sec": 0, "claude": 0})

    def _usage_refresh(self):
        m = self._usage_month()
        hrs  = m.get("sec", 0) / 3600
        cost = m.get("claude", 0) * 0.0025
        try:
            self.sv_month.configure(text=f"{hrs:.1f}h")
            self.sv_cost.configure(text=f"~${cost:.2f}")
        except Exception: pass

    def _quit(self):
        self._save_settings()
        try:
            import keyboard
            keyboard.unhook_all_hotkeys()
        except Exception:
            pass
        self._stop()
        if self._session_file:
            try: self._session_file.close()
            except: pass
        self.log.close(); self.root.destroy()


# ── Entry ────────────────────────────────────────────────────────
if __name__ == "__main__":
    if "--selftest" in sys.argv:
        # Build check, no UI: can this exe load the smart-pause model?
        SpeechGate.preload()
        ok = SpeechGate._session is not None
        if ok:
            g = SpeechGate()
            ok = g._speech_in(np.zeros(1600, np.float32)) is False and not g.failed
        rs_ok = False
        try:
            rs = Resampler(48000)
            t = np.arange(48000) / 48000.0
            y = np.concatenate([rs.run(c) for c in np.array_split(
                (0.5 * np.sin(2 * np.pi * 440 * t)).astype(np.float32), 100)])
            rs_ok = abs(len(y) - 16000) <= 1 and 0.45 < float(np.abs(y[2000:]).max()) < 0.55
        except Exception:
            pass
        ok = ok and rs_ok
        out = Path.home() / ".interpro" / "selftest.txt"
        out.write_text(f"v{__version__} smart_pause+resampler={'OK' if ok else 'FAIL'} "
                       f"{SpeechGate._load_err or ''}", encoding="utf-8")
        sys.exit(0 if ok else 1)
    missing = []
    for pkg, pip in [("sounddevice","sounddevice"), ("numpy","numpy"),
                     ("websocket","websocket-client"),
                     ("customtkinter","customtkinter")]:
        try: __import__(pkg)
        except: missing.append(pip)
    if missing:
        root = tk.Tk(); root.withdraw()
        messagebox.showerror("Missing", f"Run:\npip install {' '.join(missing)}")
        sys.exit(1)
    App()
