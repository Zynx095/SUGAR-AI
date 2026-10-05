"""
Sugar Voice Assistant — Native Windows Desktop AI
A beautiful, highly-responsive dark interface for the Sugar AI assistant.
"""

import os
from dotenv import load_dotenv

# Securely load API keys from .env file
load_dotenv()
os.environ["ANONYMIZED_TELEMETRY"] = "False"

import warnings
warnings.filterwarnings("ignore")

import subprocess
import time
import threading
import re
import queue
import numpy as np
import sounddevice as sd
import customtkinter as ctk
import pyautogui
import pyperclip
from datetime import datetime
from pydantic import BaseModel
from typing import Optional, List, Dict

import spotipy
from spotipy.oauth2 import SpotifyOAuth
from faster_whisper import WhisperModel

# ── Voice Engine Imports
from melo.api import TTS
from stt.VoiceActivityDetection import VADDetector

# ── Custom Brain Imports
from brain_models import route_model, call_ollama, parse_model_response
from config import ROSTER, DEFAULT_SENSITIVITY, DEFAULT_TTS_SPEED, DEFAULT_VOICE, DEFAULT_MASTER_PROMPT, HISTORY_WINDOW

# ── Tools (Safe Fallbacks)
try:
    from tools.calculator import extract_arithmetic, calculate
except ImportError:
    def extract_arithmetic(text): return None
    def calculate(expr): return None

try:
    from config import CHAT_LOG_FILE
except ImportError:
    CHAT_LOG_FILE = "sugar_chat.log"

# ============================================================
#  DATA MODELS
# ============================================================
class ChatMLMessage(BaseModel):
    role: str
    content: str


# ============================================================
#  VOICE CLIENT (Logic Engine)
# ============================================================
class VoiceClient:
    """Headless voice & task processing engine — posts structured events to UI."""

    def __init__(self, event_queue: queue.Queue, settings: dict):
        self.event_q     = event_queue
        self.settings    = settings
        self.listening   = False
        self.is_awake    = False
        self.history: List[ChatMLMessage] = []
        self.vad_data    = queue.Queue()
        
        self.speech_lock = threading.Lock()
        self.processing_lock = threading.Lock()
        self._running    = True

        self._check_installed_models()
        self._init_spotify()

        self._emit("log", "Initialising STT Whisper Engine…")
        self.stt = WhisperModel("large-v3-turbo", device="cpu", compute_type="int8", download_root=r"D:\Sugar_Models\Whisper")

        self._emit("log", "Initialising MeloTTS…")
        try:
            self.tts = TTS(language="EN_NEWEST", device="cpu")
        except Exception as e:
            self._emit("log", f"TTS Offline: {e}")
            self.tts = None

        self.vad = VADDetector(lambda: None, self._on_speech_end, sensitivity=DEFAULT_SENSITIVITY)
        self._emit("status", {"awake": False, "listening": False})

    def _check_installed_models(self):
        """Asynchronously checks Ollama models without blocking startup."""
        def checker():
            try:
                res = subprocess.run(["ollama", "list"], capture_output=True, text=True, check=True)
                installed = res.stdout.lower()
                for role, model in ROSTER.items():
                    if model.lower() in installed:
                        self._emit("log", f"[Model OK] {role.upper()}: {model}")
                    else:
                        self._emit("log", f"[Model MISSING] {model} -> run 'ollama pull {model}'")
            except Exception as e:
                self._emit("log", f"Failed to check models: {e}")
        threading.Thread(target=checker, daemon=True).start()

    def _init_spotify(self):
        try:
            self.spotify = spotipy.Spotify(auth_manager=SpotifyOAuth(
                client_id=os.environ.get("SPOTIFY_CLIENT_ID"),
                client_secret=os.environ.get("SPOTIFY_CLIENT_SECRET"),
                redirect_uri="https://www.google.com/", 
                scope="user-read-playback-state user-modify-playback-state"
            ))
            self._emit("log", "[Spotify] Connected.")
        except Exception as e:
            self.spotify = None
            self._emit("log", f"[Spotify] Offline mode active. {e}")

    def _emit(self, kind: str, payload=None):
        self.event_q.put({"kind": kind, "payload": payload})

    def _on_speech_end(self, data):
        if data.any():
            self.vad_data.put(data)

    def start(self):
        threading.Thread(target=self.vad.startListening, daemon=True).start()
        threading.Thread(target=self._transcription_loop, daemon=True).start()
        self._resume_listening()

    def stop(self):
        self._running = False
        self.listening = False

    def force_wake(self):
        self.is_awake = True
        self._resume_listening(silent=True)
        self._emit("log", "System manually woken.")

    def force_sleep(self):
        self.is_awake = False
        self._resume_listening(silent=True)
        self.speak("Standing by.")

    def clear_history(self):
        self.history.clear()
        self._emit("log", "Short-term conversation history cleared.")

    def update_vad_sensitivity(self, val: float):
        if hasattr(self.vad, 'sensitivity'):
            self.vad.sensitivity = val
        elif hasattr(self.vad, 'set_sensitivity'):
            self.vad.set_sensitivity(val)

    def _flush_buffer(self):
        while not self.vad_data.empty():
            self.vad_data.get()

    def _toggle_listening(self):
        self._flush_buffer()
        self.listening = not self.listening
        self._emit("status", {"awake": self.is_awake, "listening": self.listening})

    def _resume_listening(self, silent=False):
        self._flush_buffer()
        self.listening = True
        self._emit("status", {"awake": self.is_awake, "listening": True})

    def _add_to_history(self, content: str, role: str):
        self.history.append(ChatMLMessage(role=role, content=content))
        self.history = self.history[-HISTORY_WINDOW:]
        try:
            with open(CHAT_LOG_FILE, "a", encoding="utf-8") as f:
                ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                f.write(f"[{ts}] {role.upper()}: {content}\n")
        except Exception:
            pass

    def _history_for_model(self) -> List[Dict[str, str]]:
        return [{"role": m.role, "content": m.content} for m in self.history]

    def _make_tts_safe(self, text: str) -> str:
        text = re.sub(r'```.*?```', ' I have provided the code in the display. ', text, flags=re.DOTALL)
        text = re.sub(r'http[s]?://\S+', 'a link', text)
        text = re.sub(r'[*#_`\[\]]', '', text)
        return re.sub(r'\s+', ' ', text).strip()

    def _extract_code_blocks(self, text: str) -> List[Dict[str, str]]:
        blocks = []
        for match in re.finditer(r'```(\w+)?\n(.*?)```', text, re.DOTALL):
            blocks.append({"lang": match.group(1) or "code", "code": match.group(2).strip()})
        return blocks

    def speak(self, text: str):
        if not self.tts or not text.strip():
            return
        with self.speech_lock:
            try:
                vid = self.tts.hps.data.spk2id.get(self.settings.get("voice", DEFAULT_VOICE), 0)
                speed = float(self.settings.get("tts_speed", DEFAULT_TTS_SPEED))
                audio = self.tts.tts_to_file(text, vid, speed=speed, quiet=True, sdp_ratio=0.5)
                sd.play(audio, 44100)
                sd.wait()
            except Exception as e:
                self._emit("log", f"[TTS Error] {e}") 

    def _transcription_loop(self):
        while self._running:
            if not self.vad_data.empty():
                data = self.vad_data.get()
                if self.listening and len(data) > 12000:
                    self._toggle_listening()
                    audio = data.astype(np.float32) / 32768.0
                    segments, _ = self.stt.transcribe(audio, beam_size=1)
                    user_text = "".join(s.text for s in segments).strip()

                    if not user_text:
                        self._resume_listening(silent=True)
                        continue

                    # Dispatch to processor
                    threading.Thread(target=self.process_request, args=(user_text, True), daemon=True).start()
            else:
                time.sleep(0.02)

    def process_request(self, user_text: str, is_voice: bool = True):
        text_lower = user_text.lower()

        # 1. Voice Wake/Sleep Gating
        if is_voice:
            if not self.is_awake:
                if any(w in text_lower for w in ["sugar", "wake up", "so go"]):
                    self.is_awake = True
                    user_text = re.sub(r'(?i)[^a-zA-Z0-9]*\b(sugar|wake up|so go)\b[^a-zA-Z0-9]*', ' ', user_text).strip()
                    if not user_text:
                        self.speak("I am listening, Yuki.")
                        self._resume_listening(silent=True)
                        return
                else:
                    self._resume_listening(silent=True)
                    return
            else:
                if any(w in text_lower for w in ["go to sleep", "standby"]):
                    self.is_awake = False
                    self.speak("Standing by.")
                    self._resume_listening(silent=True)
                    return

        # 2. Concurrency Lock
        if not self.processing_lock.acquire(blocking=False):
            if not is_voice:
                self._emit("log", "Ignored input: already processing...")
            return

        try:
            self._emit("processing", True)
            self._emit("message", {"role": "user", "text": user_text})
            self._add_to_history(user_text, "user")

            # 3. Deterministic Local Operations
            if self._handle_os_commands(text_lower): return
            if self._handle_media_commands(text_lower): return
            if self._handle_typing_commands(text_lower): return

            expr = extract_arithmetic(user_text)
            if expr:
                self._emit("model_update", {"route": "LOCAL", "model": "Calculator"})
                ans = calculate(expr)
                if ans is not None:
                    ans_text = f"The answer is {ans}."
                    self._deliver_response(ans_text)
                    return

            # 4. Semantic AI Routing
            self._emit("log", "[Request] Evaluating intent...")
            decision = route_model(user_text)
            route_key = decision.route
            model_name = ROSTER.get(route_key, "general")
            
            self._emit("log", f"[Router] {route_key.upper()} (Confidence: {decision.confidence:.2f})")
            self._emit("model_update", {"route": route_key.upper(), "model": model_name})

            # 5. Model Inference
            start_time = time.time()
            prev_history = self._history_for_model()[:-1]  # Exclude current query just added
            
            response = call_ollama(prompt=user_text, history=prev_history, model_key=route_key)
            self._emit("log", f"[Model] Gen completed in {time.time() - start_time:.2f}s")

            # 6. Response Parsing & Delivery
            self._deliver_response(response)

        except Exception as e:
            self._emit("log", f"[System Error] {str(e)}")
            self._deliver_response("I encountered an internal error processing that request.")
        finally:
            self._emit("processing", False)
            if is_voice:
                self._resume_listening(silent=True)
            self.processing_lock.release()

    def _deliver_response(self, raw_response: str):
        """Parses UI blocks, logs history, and speaks."""
        reasoning, clean_prose = parse_model_response(raw_response)
        
        self._add_to_history(raw_response, "assistant")
        
        # Strip code blocks out of the prose so it isn't rendered twice in the UI bubble
        prose_no_code = re.sub(r'```.*?```', '', clean_prose, flags=re.DOTALL).strip()
        
        # Emit to UI
        self._emit("message", {
            "role": "assistant", 
            "text": prose_no_code or "Here is the code you requested.",
            "reasoning": reasoning
        })

        # Emit code blocks specifically
        for block in self._extract_code_blocks(clean_prose):
            self._emit("code_block", block)

        # TTS
        spoken_text = self._make_tts_safe(clean_prose)
        if spoken_text:
            self.speak(spoken_text)

    # ── Deterministic Action Handlers ──
    def _handle_os_commands(self, text: str) -> bool:
        if "open chrome" in text:
            self.speak("Opening Chrome.")
            subprocess.Popen(["start", "chrome"], shell=True)
            self._deliver_response("Launched Google Chrome.")
            return True
        if "open code" in text or "vs code" in text:
            self.speak("Launching VS Code.")
            subprocess.Popen(["code"], shell=True)
            self._deliver_response("Launched Visual Studio Code.")
            return True
        if "time" in text and ("what" in text or "current" in text):
            t = datetime.now().strftime("%I:%M %p")
            self._deliver_response(f"The time is {t}.")
            return True
        return False

    def _handle_media_commands(self, text: str) -> bool:
        if not self.spotify: return False
        try:
            if "what" in text and ("song" in text or "playing" in text):
                c = self.spotify.current_playback()
                if c and c.get('is_playing'):
                    self._deliver_response(f"This is {c['item']['name']} by {c['item']['artists'][0]['name']}.")
                else:
                    self._deliver_response("Nothing is currently playing.")
                return True
            if "pause" in text or "stop music" in text:
                self.spotify.pause_playback()
                self._deliver_response("Paused Spotify.")
                return True
            if "next song" in text or "skip" in text:
                self.spotify.next_track()
                self._deliver_response("Skipped track.")
                return True
        except spotipy.SpotifyException:
            pass
        return False

    def _handle_typing_commands(self, text: str) -> bool:
        if "type" in text or "write" in text:
            q = re.sub(r'\b(sugar|can|you|please|type|write|this|down)\b', '', text).strip().strip(".,!?")
            if q:
                self.speak("Typing in 3 seconds.")
                time.sleep(3)
                pyautogui.write(q, interval=0.06)
                self._deliver_response(f"Typed: {q}")
            return True
        return False


# ============================================================
#  UI SYSTEM (CustomTkinter)
# ============================================================
DARK_BG      = "#0a0a0f"
PANEL_BG     = "#12121a"
CARD_BG      = "#1c1c28"
ACCENT_CYAN  = "#00e5ff"
ACCENT_PURP  = "#a68cff"
ACCENT_GREEN = "#00e676"
ACCENT_RED   = "#ff1744"
TEXT_PRI     = "#ffffff"
TEXT_SEC     = "#8f8f9d"
BORDER       = "#292938"

class SugarApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        ctk.set_appearance_mode("dark")
        self.title("SUGAR · Native AI Desktop")
        self.geometry("1300x850")
        self.minsize(1000, 650)
        self.configure(fg_color=DARK_BG)

        self.event_q   = queue.Queue()
        self.settings  = {
            "master_prompt": DEFAULT_MASTER_PROMPT,
            "sensitivity":   DEFAULT_SENSITIVITY,
            "tts_speed":     DEFAULT_TTS_SPEED,
            "voice":         DEFAULT_VOICE,
        }
        self.client: Optional[VoiceClient] = None
        self._pulse_phase = 0

        self._build_layout()
        self._log("SUGAR Engine — Initializing subsystems...")
        
        threading.Thread(target=self._init_client, daemon=True).start()
        self._poll_events()
        self._pulse_loop()

    # ── UI Construction ──
    def _build_layout(self):
        self.grid_columnconfigure(0, weight=0, minsize=260)  # Sidebar
        self.grid_columnconfigure(1, weight=1)               # Main Chat
        self.grid_rowconfigure(0, weight=1)

        # Sidebar
        sb = ctk.CTkFrame(self, fg_color=PANEL_BG, corner_radius=0)
        sb.grid(row=0, column=0, sticky="nsew")
        sb.grid_rowconfigure(4, weight=1)

        ctk.CTkLabel(sb, text="SUGAR", font=("Segoe UI Black", 24), text_color=ACCENT_CYAN, anchor="w").grid(row=0, column=0, padx=24, pady=(30, 0), sticky="ew")
        ctk.CTkLabel(sb, text="Local Intelligence", font=("Segoe UI", 11), text_color=TEXT_SEC, anchor="w").grid(row=1, column=0, padx=24, pady=(0, 20), sticky="ew")

        # Status Card
        self.status_card = ctk.CTkFrame(sb, fg_color=CARD_BG, corner_radius=12)
        self.status_card.grid(row=2, column=0, padx=16, pady=10, sticky="ew")
        
        self.pulse_canvas = ctk.CTkCanvas(self.status_card, width=12, height=12, bg=CARD_BG, highlightthickness=0)
        self.pulse_canvas.pack(side="left", padx=16, pady=16)
        
        status_text = ctk.CTkFrame(self.status_card, fg_color="transparent")
        status_text.pack(side="left", pady=10)
        self.status_lbl = ctk.CTkLabel(status_text, text="STARTING", font=("Segoe UI", 13, "bold"), text_color=TEXT_PRI)
        self.status_lbl.pack(anchor="w")
        self.status_sub = ctk.CTkLabel(status_text, text="Loading models...", font=("Segoe UI", 11), text_color=TEXT_SEC)
        self.status_sub.pack(anchor="w")

        # Controls
        ctrls = ctk.CTkFrame(sb, fg_color="transparent")
        ctrls.grid(row=3, column=0, padx=16, pady=20, sticky="ew")
        btn_cfg = {"fg_color": CARD_BG, "hover_color": "#28283a", "text_color": TEXT_PRI, "font": ("Segoe UI", 12), "height": 40, "anchor": "w"}
        
        self.wake_btn = ctk.CTkButton(ctrls, text="  Wake Sugar", command=lambda: self.client.force_wake() if self.client else None, **btn_cfg)
        self.wake_btn.pack(fill="x", pady=4)
        self.sleep_btn = ctk.CTkButton(ctrls, text="  Sleep", command=lambda: self.client.force_sleep() if self.client else None, state="disabled", **btn_cfg)
        self.sleep_btn.pack(fill="x", pady=4)
        ctk.CTkButton(ctrls, text="  Clear Chat", command=self._clear_chat, **btn_cfg).pack(fill="x", pady=4)

        # Active Model Info
        mod_card = ctk.CTkFrame(sb, fg_color=DARK_BG, corner_radius=12, border_width=1, border_color=BORDER)
        mod_card.grid(row=5, column=0, padx=16, pady=(0, 20), sticky="ew")
        
        ctk.CTkLabel(mod_card, text="ACTIVE ROUTE", font=("Segoe UI", 10, "bold"), text_color=TEXT_SEC).pack(anchor="w", padx=16, pady=(12, 0))
        self.route_lbl = ctk.CTkLabel(mod_card, text="Initializing...", font=("Segoe UI", 14, "bold"), text_color=ACCENT_PURP)
        self.route_lbl.pack(anchor="w", padx=16)
        
        ctk.CTkLabel(mod_card, text="MODEL", font=("Segoe UI", 10, "bold"), text_color=TEXT_SEC).pack(anchor="w", padx=16, pady=(10, 0))
        self.model_lbl = ctk.CTkLabel(mod_card, text="—", font=("Segoe UI", 13), text_color=TEXT_PRI)
        self.model_lbl.pack(anchor="w", padx=16, pady=(0, 12))

        # Main Chat Area
        main = ctk.CTkFrame(self, fg_color="transparent")
        main.grid(row=0, column=1, sticky="nsew")
        main.grid_rowconfigure(0, weight=1)
        main.grid_columnconfigure(0, weight=1)

        self.chat_view = ctk.CTkScrollableFrame(main, fg_color="transparent")
        self.chat_view.grid(row=0, column=0, sticky="nsew", padx=20, pady=(20, 0))
        
        # Idle Hero State
        self.hero_frame = ctk.CTkFrame(self.chat_view, fg_color="transparent")
        self.hero_frame.pack(expand=True, fill="both", pady=150)
        ctk.CTkLabel(self.hero_frame, text="How can I help you, Yuki?", font=("Segoe UI", 28, "bold"), text_color=TEXT_PRI).pack()
        ctk.CTkLabel(self.hero_frame, text="Say 'Sugar' to wake, or type below.", font=("Segoe UI", 14), text_color=TEXT_SEC).pack(pady=10)

        # Input Area
        inp_bg = ctk.CTkFrame(main, fg_color=PANEL_BG, corner_radius=20, height=60, border_width=1, border_color=BORDER)
        inp_bg.grid(row=1, column=0, sticky="ew", padx=30, pady=20)
        inp_bg.grid_columnconfigure(0, weight=1)
        inp_bg.grid_propagate(False)

        self.text_in = ctk.CTkEntry(inp_bg, placeholder_text="Ask Sugar anything...", font=("Segoe UI", 14), fg_color="transparent", border_width=0, text_color=TEXT_PRI)
        self.text_in.grid(row=0, column=0, sticky="nsew", padx=20)
        self.text_in.bind("<Return>", lambda e: self._handle_typed())

        send_btn = ctk.CTkButton(inp_bg, text="SEND", font=("Segoe UI", 13, "bold"), width=80, fg_color=ACCENT_PURP, hover_color=ACCENT_CYAN, text_color=DARK_BG, corner_radius=15, command=self._handle_typed)
        send_btn.grid(row=0, column=1, padx=10, pady=10, sticky="nsew")

    # ── Engine Events ──
    def _init_client(self):
        try:
            self.client = VoiceClient(self.event_q, self.settings)
            self.client.start()
            self.event_q.put({"kind": "ready"})
        except Exception as e:
            self._log(f"Fatal Startup Error: {e}")

    def _poll_events(self):
        try:
            while True:
                ev = self.event_q.get_nowait()
                k, p = ev["kind"], ev.get("payload")

                if k == "ready":
                    self.status_lbl.configure(text="READY")
                    self.status_sub.configure(text="Say 'Sugar' to wake")
                    self.route_lbl.configure(text="GENERAL")
                    self.model_lbl.configure(text=ROSTER[DEFAULT_ROUTE])
                elif k == "log":
                    print(f"[{datetime.now().strftime('%H:%M:%S')}] {p}")
                elif k == "status":
                    aw, ls = p.get("awake", False), p.get("listening", False)
                    if aw and ls:
                        self.status_lbl.configure(text="LISTENING")
                        self.status_sub.configure(text="Waiting for query...")
                        self.wake_btn.configure(state="disabled")
                        self.sleep_btn.configure(state="normal")
                    else:
                        self.status_lbl.configure(text="SLEEPING")
                        self.status_sub.configure(text="Say 'Sugar' to wake")
                        self.wake_btn.configure(state="normal")
                        self.sleep_btn.configure(state="disabled")
                elif k == "processing":
                    if p:
                        self.status_lbl.configure(text="PROCESSING")
                        self.status_sub.configure(text="Thinking...")
                elif k == "model_update":
                    self.route_lbl.configure(text=p["route"])
                    self.model_lbl.configure(text=p["model"])
                elif k == "message":
                    self._render_message(p["role"], p["text"], p.get("reasoning"))
                elif k == "code_block":
                    self._render_code_block(p["lang"], p["code"])
        except queue.Empty:
            pass
        self.after(50, self._poll_events)

    def _pulse_loop(self):
        st = self.status_lbl.cget("text")
        color = ACCENT_RED
        if st == "LISTENING":
            color = ACCENT_GREEN if (self._pulse_phase % 20) < 10 else "#147a45"
            self._pulse_phase += 1
        elif st == "PROCESSING":
            color = ACCENT_AMBER
        elif st == "READY" or st == "SLEEPING":
            color = TEXT_SEC
            
        self.pulse_canvas.delete("all")
        self.pulse_canvas.create_oval(1, 1, 11, 11, fill=color, outline="")
        self.after(100, self._pulse_loop)

    # ── Chat UI Elements ──
    def _handle_typed(self):
        txt = self.text_in.get().strip()
        if not txt or not self.client: return
        self.text_in.delete(0, 'end')
        threading.Thread(target=self.client.process_request, args=(txt, False), daemon=True).start()

    def _render_message(self, role: str, text: str, reasoning: str = None):
        if self.hero_frame.winfo_ismapped():
            self.hero_frame.pack_forget()

        is_user = (role == "user")
        wrapper = ctk.CTkFrame(self.chat_view, fg_color="transparent")
        wrapper.pack(fill="x", padx=10, pady=10)
        wrapper.grid_columnconfigure(0, weight=1)

        align = "e" if is_user else "w"
        bg_col = "#202030" if is_user else CARD_BG
        txt_col = ACCENT_CYAN if is_user else TEXT_PRI

        content_fr = ctk.CTkFrame(wrapper, fg_color=bg_col, corner_radius=14)
        content_fr.pack(anchor=align)

        # Name label
        name = "YUKI" if is_user else "SUGAR"
        ctk.CTkLabel(content_fr, text=name, font=("Segoe UI", 10, "bold"), text_color=txt_col).pack(anchor="w" if not is_user else "e", padx=16, pady=(10, 0))

        # Reasoning block (DeepSeek R1)
        if reasoning:
            res_box = ctk.CTkFrame(content_fr, fg_color=DARK_BG, corner_radius=8)
            res_box.pack(fill="x", padx=16, pady=(8, 0))
            ctk.CTkLabel(res_box, text="[Reasoning Process]", font=("Segoe UI", 10, "italic"), text_color=TEXT_SEC).pack(anchor="w", padx=10, pady=(6,0))
            ctk.CTkLabel(res_box, text=reasoning, font=("Segoe UI", 11), text_color=TEXT_SEC, wraplength=600, justify="left").pack(padx=10, pady=(0,6))

        # Main Text
        if text:
            ctk.CTkLabel(content_fr, text=text, font=("Segoe UI", 14), text_color=TEXT_PRI, wraplength=600, justify="left").pack(padx=16, pady=(4, 12))

        self.after(50, lambda: self.chat_view._parent_canvas.yview_moveto(1.0))

    def _render_code_block(self, lang: str, code: str):
        wrapper = ctk.CTkFrame(self.chat_view, fg_color=CARD_BG, corner_radius=8, border_width=1, border_color=BORDER)
        wrapper.pack(fill="x", padx=10, pady=5)
        
        hdr = ctk.CTkFrame(wrapper, fg_color="transparent")
        hdr.pack(fill="x", padx=10, pady=(8, 4))
        ctk.CTkLabel(hdr, text=lang.upper(), font=("Segoe UI", 11, "bold"), text_color=ACCENT_PURP).pack(side="left")
        
        cpy_btn = ctk.CTkButton(hdr, text="COPY", width=60, height=24, font=("Segoe UI", 10, "bold"), fg_color=PANEL_BG, hover_color=ACCENT_GREEN)
        cpy_btn.pack(side="right")
        
        def do_copy():
            pyperclip.copy(code)
            cpy_btn.configure(text="COPIED", fg_color=ACCENT_GREEN, text_color=DARK_BG)
            self.after(2000, lambda: cpy_btn.configure(text="COPY", fg_color=PANEL_BG, text_color=TEXT_PRI))
        cpy_btn.configure(command=do_copy)

        txt = ctk.CTkTextbox(wrapper, font=("Courier New", 13), fg_color=DARK_BG, text_color="#dcdcaa", height=min(300, max(80, code.count('\n') * 20)), wrap="none")
        txt.pack(fill="x", padx=10, pady=(0, 10))
        txt.insert("end", code)
        txt.configure(state="disabled")

        self.after(50, lambda: self.chat_view._parent_canvas.yview_moveto(1.0))

    def _clear_chat(self):
        for w in self.chat_view.winfo_children():
            if w != self.hero_frame:
                w.destroy()
        if self.client: self.client.clear_history()
        self.hero_frame.pack(expand=True, fill="both", pady=150)

    def _log(self, msg: str):
        print(f"[System] {msg}")

    def on_close(self):
        if self.client: self.client.stop()
        self.destroy()


if __name__ == "__main__":
    app = SugarApp()
    app.protocol("WM_DELETE_WINDOW", app.on_close)
    app.mainloop()