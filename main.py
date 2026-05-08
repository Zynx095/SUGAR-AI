"""
Sugar Voice Assistant — UI Edition
A beautiful, editable dark interface for the Sugar AI voice assistant.
Requires: customtkinter, melo, sounddevice, faster_whisper, pydantic,
          numpy, playsound, librosa (and your custom modules)
"""

import os
from dotenv import load_dotenv

# Securely load API keys from .env file
load_dotenv()

os.environ["ANONYMIZED_TELEMETRY"] = "False"

# Silence harmless library deprecation warnings
import warnings
warnings.filterwarnings("ignore")

import subprocess
import webbrowser
import pyautogui
import pyperclip
import spotipy
from spotipy.oauth2 import SpotifyOAuth
import time
import threading
import re
import queue
import numpy as np
import sounddevice as sd
import customtkinter as ctk
from datetime import datetime
from pydantic import BaseModel

# ── Voice Engine Imports ───────────────────────────────────────────────────────
from melo.api import TTS
from stt.VoiceActivityDetection import VADDetector
from faster_whisper import WhisperModel

# ── Custom Brain Imports ───────────────────────────────────────────────────────
from brain_models import route_model, call_ollama
from config import ROSTER
from memory import store_memory, retrieve_memory

# ══════════════════════════════════════════════════════════════════════════════
#  SETTINGS  (edit these defaults in the UI at runtime too)
# ══════════════════════════════════════════════════════════════════════════════
DEFAULT_MASTER_PROMPT = (
    "You are a fast, sharp AI assistant named Sugar. "
    "Keep responses short, clear, and conversational. "
    "The user's name is Yuki, but do not start every sentence with greetings."
)
DEFAULT_SENSITIVITY     = 0.3
DEFAULT_TTS_SPEED       = 1.1
DEFAULT_VOICE           = "EN-Newest"
DEFAULT_WHISPER_MODEL   = "large-v3-turbo"
HISTORY_WINDOW          = 4          # how many messages to keep in context


# ══════════════════════════════════════════════════════════════════════════════
#  DATA MODELS
# ══════════════════════════════════════════════════════════════════════════════
class ChatMLMessage(BaseModel):
    role: str
    content: str


# ══════════════════════════════════════════════════════════════════════════════
#  VOICE CLIENT  (pure logic, no UI references)
# ══════════════════════════════════════════════════════════════════════════════
class VoiceClient:
    """Headless voice processing engine — posts events to a queue for the UI."""

    def __init__(self, event_queue: queue.Queue, settings: dict):
        self.event_q     = event_queue
        self.settings    = settings          # live reference — UI can update it
        self.listening   = False
        self.is_awake    = False
        self.history: list[ChatMLMessage] = []
        self.vad_data    = queue.Queue()
        self.speech_lock = threading.Lock()
        self._running    = True

        self.spotify_engine = None
        try:
            self.spotify_engine = spotipy.Spotify(auth_manager=SpotifyOAuth(
                client_id=os.environ.get("SPOTIFY_CLIENT_ID"),
                client_secret=os.environ.get("SPOTIFY_CLIENT_SECRET"),
                redirect_uri="https://www.google.com/", 
                scope="user-read-playback-state user-modify-playback-state"
            ))
            self._emit("log", "Spotify API Connected.")
        except Exception as e:
            self._emit("log", f"Spotify API Error: Check your .env file! {e}")
        self._emit("log", "Initialising Whisper STT…")
        # --- STORAGE FIX: Force download to D: Drive ---
        self.stt = WhisperModel(
            DEFAULT_WHISPER_MODEL, 
            device="cpu", 
            compute_type="int8",
            download_root=r"D:\Sugar_Models\Whisper"  # Put your exact D: folder path here
        )

        self._emit("log", "Initialising MeloTTS…")
        self.tts = TTS(language="EN_NEWEST", device="cpu")
        self._emit("log", f"Voices: {list(self.tts.hps.data.spk2id.keys())}")

        self.vad = VADDetector(
            lambda: None,
            self._on_speech_end,
            sensitivity=DEFAULT_SENSITIVITY,
        )

        self._emit("status", {"awake": False, "listening": False})

    # ── Event helpers ──────────────────────────────────────────────────────────
    def _emit(self, kind: str, payload=None):
        self.event_q.put({"kind": kind, "payload": payload})

    # ── Audio callbacks ────────────────────────────────────────────────────────
    def _on_speech_end(self, data):
        if data.any():
            self.vad_data.put(data)

    # ── Public controls ────────────────────────────────────────────────────────
    def start(self):
        threading.Thread(target=self.vad.startListening, daemon=True).start()
        threading.Thread(target=self._transcription_loop, daemon=True).start()
        self._resume_listening()

    def stop(self):
        self._running = False
        self.listening = False

    def force_wake(self):
        self.is_awake = True
        self._emit("status", {"awake": True, "listening": self.listening})
        self._emit("log", "Manually woken.")

    def force_sleep(self):
        self.is_awake = False
        self._emit("status", {"awake": False, "listening": self.listening})
        self.speak("Standing by.")

    # ── Listening helpers ──────────────────────────────────────────────────────
    def _flush_buffer(self):
        while not self.vad_data.empty():
            self.vad_data.get()

    def _toggle_listening(self):
        self._flush_buffer()
        self.listening = not self.listening
        self._emit("status", {"awake": self.is_awake, "listening": self.listening})
        if self.listening:
            try:
                # --- AUDIO FIX: Temporarily comment out the beep ---
                # playsound("beep.mp3") 
                pass
            except Exception:
                pass 

    def _resume_listening(self, silent=False):
        self._flush_buffer()
        self.listening = True
        self._emit("status", {"awake": self.is_awake, "listening": True})
        if not silent:
            self._emit("log", "Listening…")

# ── History helpers ────────────────────────────────────────────────────────
    def _add_to_history(self, content: str, role: str):
        display = content
        if role == "user":
            full = f"{self.settings['master_prompt']}\n\n{content}"
        else:
            full = content

        self.history.append(ChatMLMessage(content=full, role=role))
        self.history = self.history[-HISTORY_WINDOW:]
        self._emit("message", {"role": role, "text": display})
        
        # --- FIX 1: SAVE CHATS TO FILE ---
        try:
            with open("Sugar_Chat_Logs.txt", "a", encoding="utf-8") as f:
                ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                f.write(f"[{ts}] {role.upper()}: {display}\n")
        except Exception as e:
            self._emit("log", f"Failed to save log: {e}")

    # --- THIS WAS MISSING! ---
    def _history_as_string(self) -> str:
        return "\n".join(f"{m.role}: {m.content}" for m in self.history)
    # -------------------------

    # ── Text helpers ───────────────────────────────────────────────────────────
    @staticmethod
    def _split_response(text: str):
        if "```" in text:
            parts = text.split("```")
            return parts[0].strip(), parts[1] if len(parts) > 1 else ""
        return text.strip(), ""

    # ── TTS ────────────────────────────────────────────────────────────────────
    def speak(self, text: str):
        with self.speech_lock:
            try:
                voice_key = self.settings.get("voice", DEFAULT_VOICE)
                speed     = float(self.settings.get("tts_speed", DEFAULT_TTS_SPEED))
                try:
                    voice_id = self.tts.hps.data.spk2id[voice_key]
                except Exception:
                    voice_id = 0 
                
                audio = self.tts.tts_to_file(
                    text, voice_id, speed=speed, quiet=True, sdp_ratio=0.5
                )
                sd.play(audio, 44100)
                sd.wait()
                
            except Exception as e:
                self._emit("log", f"[TTS ERROR] {e}") 

    # ── Main transcription loop ────────────────────────────────────────────────
    def _transcription_loop(self):
        while self._running:
            if not self.vad_data.empty():
                data = self.vad_data.get()

                if self.listening and len(data) > 12000:
                    self._toggle_listening()
                    self._emit("processing", True)

                    audio = data.astype(np.float32) / 32768.0
                    segments, _ = self.stt.transcribe(audio, beam_size=1)
                    user_text = "".join(s.text for s in segments).strip()

                    if not self.is_awake and user_text:
                        self._emit("log", f"[Heard] {user_text}")

                    if not user_text:
                        self._emit("processing", False)
                        self._resume_listening(silent=not self.is_awake)
                        continue

                    text_lower = user_text.lower()

                    # ── Wake / sleep gate ──────────────────────────────────────
                    if not self.is_awake:
                        wake_words = ["sugar", "wake up", "so go"]
                        if any(w in text_lower for w in wake_words):
                            self.is_awake = True
                            self._emit("status", {"awake": True, "listening": True})
                            self._emit("log", "[SYSTEM AWAKE]")
                            pattern = r'(?i)[^a-zA-Z0-9]*\b(sugar|wake up|so go)\b[^a-zA-Z0-9]*'
                            user_text = re.sub(pattern, ' ', user_text).strip()
                            if not user_text:
                                self.speak("I am listening, Yuki.")
                                self._emit("processing", False)
                                self._resume_listening()
                                continue
                        else:
                            self._emit("processing", False)
                            self._resume_listening(silent=True)
                            continue
                    else:
                        sleep_words = ["go to sleep", "standby"]
                        if any(w in text_lower for w in sleep_words):
                            self.is_awake = False
                            self._emit("status", {"awake": False, "listening": True})
                            self._emit("log", "[SYSTEM ASLEEP]")
                            self.speak("Standing by.")
                            self._emit("processing", False)
                            self._resume_listening()
                            continue
                    self._add_to_history(user_text, "user")

                    # ── PHASE 2: SMART OS COMMANDS ─────────────────────────────
                    if self.is_awake:
                        # 1. Open Chrome
                        if "open chrome" in text_lower:
                            self._emit("log", "[Command] Opening Chrome")
                            self.speak("Opening Chrome.")
                            subprocess.Popen(["start", "chrome"], shell=True)
                            self._emit("processing", False)
                            self._resume_listening()
                            continue
                            
                        # 2. Open VS Code
                        if "open code" in text_lower or "vs code" in text_lower:
                            self._emit("log", "[Command] Opening VS Code")
                            self.speak("Launching VS Code.")
                            subprocess.Popen(["code"], shell=True)
                            self._emit("processing", False)
                            self._resume_listening()
                            continue

                        # 3. Check Time
                        if "time" in text_lower and ("what" in text_lower or "current" in text_lower):
                            current_time = datetime.now().strftime("%I:%M %p")
                            self._emit("log", f"[Command] Time check: {current_time}")
                            self.speak(f"The time is {current_time}.")
                            self._emit("processing", False)
                            self._resume_listening()
                            continue
                        # 4. Media Controls (Official API)
                        if self.spotify_engine:
                            try:
                                # -- What song is playing? --
                                if "what" in text_lower and ("song" in text_lower or "playing" in text_lower):
                                    self._emit("log", "[Command] Checking current song")
                                    current = self.spotify_engine.current_playback()
                                    if current and current['is_playing']:
                                        song_name = current['item']['name']
                                        artist = current['item']['artists'][0]['name']
                                        self.speak(f"This is {song_name} by {artist}.")
                                    else:
                                        self.speak("Nothing is currently playing.")
                                    self._emit("processing", False)
                                    self._resume_listening()
                                    continue

                                # -- Next Song --
                                if "next" in text_lower and ("song" in text_lower or "track" in text_lower or "music" in text_lower):
                                    self._emit("log", "[Command] Next Track")
                                    self.speak("Skipping.")
                                    self.spotify_engine.next_track()
                                    self._emit("processing", False)
                                    self._resume_listening()
                                    continue

                                # -- Previous Song --
                                if "previous" in text_lower or "last song" in text_lower:
                                    self._emit("log", "[Command] Previous Track")
                                    self.speak("Going back.")
                                    self.spotify_engine.previous_track()
                                    self._emit("processing", False)
                                    self._resume_listening()
                                    continue

                                # -- Pause/Stop --
                                if "pause" in text_lower or "stop music" in text_lower:
                                    self._emit("log", "[Command] Pause Media")
                                    self.speak("Pausing.")
                                    self.spotify_engine.pause_playback()
                                    self._emit("processing", False)
                                    self._resume_listening()
                                    continue

                                # -- Play Specific Song --
                                if "play" in text_lower.split():
                                    clean_pattern = r'\b(play|music|song|some|a|an|on|spotify|by)\b'
                                    song_query = re.sub(clean_pattern, '', text_lower).strip()
                                    
                                    if song_query:
                                        self._emit("log", f"[Command] Spotify Search: {song_query}")
                                        self.speak(f"Playing {song_query}.")
                                        
                                        # Ask Spotify to find the track
                                        results = self.spotify_engine.search(q=song_query, limit=1, type='track')
                                        if results['tracks']['items']:
                                            track_uri = results['tracks']['items'][0]['uri']
                                            # Instantly play it in the background
                                            self.spotify_engine.start_playback(uris=[track_uri])
                                        else:
                                            self.speak(f"I couldn't find {song_query} on Spotify.")
                                    else:
                                        self._emit("log", "[Command] Resuming Media")
                                        self.speak("Resuming.")
                                        self.spotify_engine.start_playback()
                                        
                                    self._emit("processing", False)
                                    self._resume_listening()
                                    continue
                                    
                            except spotipy.SpotifyException as e:
                                self._emit("log", f"[Spotify Action Error]: Open your app first! {e}")
                                # --- DEADLOCK FIX: Reset the mic even if Spotify crashes ---
                                self._emit("processing", False)
                                self._resume_listening()
                                # Notice there is no "continue" here at the end of the except block!

                        # 5. Type on Screen
                        if "type" in text_lower or "write" in text_lower:
                            # Strip out her name and polite trigger words
                            clean_pattern = r'\b(sugar|can|you|please|type|write|this|down)\b'
                            type_query = re.sub(clean_pattern, '', text_lower).strip()
                            type_query = type_query.strip(".,!?") 
                            
                            if type_query:
                                self._emit("log", f"[Command] Typing: {type_query}")
                                self.speak("Typing in 3 seconds.")
                                time.sleep(3) 
                                pyautogui.write(type_query, interval=0.08)
                            else:
                                self.speak("What would you like me to type?")
                                
                            self._emit("processing", False)
                            self._resume_listening()
                            continue
                    # ── AI processing ──────────────────────────────────────────
                    # --- FIX 1: BLANK SLATE MEMORY ---
                    # We disabled retrieve_memory() so old chats don't bleed into new ones
                    prompt = user_text 

                    route_key = route_model(user_text)
                    model_name = ROSTER.get(route_key, route_key)
                    self._emit("model", model_name)

                    response  = call_ollama(prompt, self._history_as_string(), route_key)
                    store_memory(f"{user_text} -> {response}")

                    clean = re.sub(r'\*+|`+|\[.*?\]', '', response).strip()
                    clean = clean.replace("assistant:", "").replace("user:", "").strip() # FIX GLITCH
                    self._add_to_history(clean, "assistant")
                    self._add_to_history(clean, "assistant")

                    spoken, code = self._split_response(clean)
                    if code:
                        self._emit("code", code)
                        spoken += " I've printed the code in the terminal."

                    self._emit("processing", False)
                    self.speak(spoken)
                    self._resume_listening()
            else:
                time.sleep(0.02)


# ══════════════════════════════════════════════════════════════════════════════
#  UI HELPERS
# ══════════════════════════════════════════════════════════════════════════════
DARK_BG      = "#0c0c14"
PANEL_BG     = "#13131f"
CARD_BG      = "#1a1a2e"
ACCENT_CYAN  = "#00d4ff"
ACCENT_PURP  = "#9b8fff"
ACCENT_GREEN = "#39d98a"
ACCENT_RED   = "#ff4f6d"
ACCENT_AMBER = "#ffb347"
TEXT_PRI     = "#e8e8f0"
TEXT_SEC     = "#7a7a9a"
BORDER       = "#2a2a3e"
FONT_MONO    = ("Courier New", 12)
FONT_MONO_SM = ("Courier New", 10)
FONT_BODY    = ("Segoe UI", 12)
FONT_TITLE   = ("Segoe UI", 22, "bold")
FONT_LABEL   = ("Segoe UI", 10)


def hex_to_rgb(h: str):
    h = h.lstrip("#")
    return tuple(int(h[i:i+2], 16) for i in (0, 2, 4))


# ══════════════════════════════════════════════════════════════════════════════
#  MAIN APPLICATION WINDOW
# ══════════════════════════════════════════════════════════════════════════════
class SugarApp(ctk.CTk):
    def __init__(self):
        super().__init__()

        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("dark-blue")

        self.title("SUGAR  ·  Voice AI Interface")
        self.geometry("1260x800")
        self.minsize(900, 600)
        self.configure(fg_color=DARK_BG)

        # ── Shared state ───────────────────────────────────────────────────────
        self.event_q   = queue.Queue()
        self.settings  = {
            "master_prompt": DEFAULT_MASTER_PROMPT,
            "sensitivity":   DEFAULT_SENSITIVITY,
            "tts_speed":     DEFAULT_TTS_SPEED,
            "voice":         DEFAULT_VOICE,
        }
        self.client: VoiceClient | None = None
        self._pulse_phase = 0
        self._settings_open = False

        # ── Build UI ───────────────────────────────────────────────────────────
        self._build_layout()
        self._build_sidebar()
        self._build_chat_area()
        self._build_settings_panel()

        # ── Boot sequence ──────────────────────────────────────────────────────
        self._log("SUGAR v2.0  —  initialising…")
        self._log("Loading models. This may take a moment.")
        threading.Thread(target=self._init_client, daemon=True).start()

        # ── Event loop ─────────────────────────────────────────────────────────
        self._poll_events()
        self._pulse_loop()

    # ══════════════════════════════════════════════════════════════════════════
    #  LAYOUT SCAFFOLDING
    # ══════════════════════════════════════════════════════════════════════════
    def _build_layout(self):
        self.grid_columnconfigure(0, weight=0, minsize=220)  # sidebar
        self.grid_columnconfigure(1, weight=1)               # chat
        self.grid_columnconfigure(2, weight=0, minsize=0)    # settings (hidden)
        self.grid_rowconfigure(0, weight=1)

    # ══════════════════════════════════════════════════════════════════════════
    #  SIDEBAR
    # ══════════════════════════════════════════════════════════════════════════
    def _build_sidebar(self):
        sb = ctk.CTkFrame(self, fg_color=PANEL_BG, corner_radius=0, width=220)
        sb.grid(row=0, column=0, sticky="nsew")
        sb.grid_propagate(False)
        sb.grid_rowconfigure(6, weight=1)

        # Logo
        logo_frame = ctk.CTkFrame(sb, fg_color="transparent")
        logo_frame.grid(row=0, column=0, padx=20, pady=(28, 0), sticky="w")

        ctk.CTkLabel(
            logo_frame, text="◈ SUGAR", font=("Courier New", 18, "bold"),
            text_color=ACCENT_CYAN
        ).pack(side="left")

        ctk.CTkLabel(
            sb, text="voice ai interface",
            font=FONT_LABEL, text_color=TEXT_SEC
        ).grid(row=1, column=0, padx=22, pady=(2, 24), sticky="w")

        # Status card
        self.status_frame = ctk.CTkFrame(sb, fg_color=CARD_BG, corner_radius=10)
        self.status_frame.grid(row=2, column=0, padx=16, pady=(0, 16), sticky="ew")

        # Pulse indicator canvas
        self.pulse_canvas = ctk.CTkCanvas(
            self.status_frame, width=14, height=14,
            bg=CARD_BG, highlightthickness=0
        )
        self.pulse_canvas.pack(side="left", padx=(14, 8), pady=14)
        self._draw_pulse(ACCENT_RED)   # start red (offline)

        status_right = ctk.CTkFrame(self.status_frame, fg_color="transparent")
        status_right.pack(side="left", pady=10)

        self.status_label = ctk.CTkLabel(
            status_right, text="OFFLINE", font=("Courier New", 11, "bold"),
            text_color=ACCENT_RED
        )
        self.status_label.pack(anchor="w")
        self.status_sub = ctk.CTkLabel(
            status_right, text="initialising…", font=FONT_LABEL,
            text_color=TEXT_SEC
        )
        self.status_sub.pack(anchor="w")

        # Divider
        ctk.CTkFrame(sb, fg_color=BORDER, height=1).grid(
            row=3, column=0, padx=16, pady=0, sticky="ew"
        )

        # Control buttons
        btn_cfg = dict(
            fg_color=CARD_BG, hover_color="#252535",
            text_color=TEXT_PRI, corner_radius=8,
            font=("Segoe UI", 12), anchor="w", height=38,
        )
        ctrl = ctk.CTkFrame(sb, fg_color="transparent")
        ctrl.grid(row=4, column=0, padx=14, pady=14, sticky="ew")
        ctrl.grid_columnconfigure(0, weight=1)

        self.wake_btn = ctk.CTkButton(
            ctrl, text="⚡  Wake Sugar", command=self._manual_wake, **btn_cfg
        )
        self.wake_btn.grid(row=0, column=0, pady=4, sticky="ew")

        self.sleep_btn = ctk.CTkButton(
            ctrl, text="💤  Sleep", command=self._manual_sleep, **btn_cfg,
            state="disabled"
        )
        self.sleep_btn.grid(row=1, column=0, pady=4, sticky="ew")

        self.clear_btn = ctk.CTkButton(
            ctrl, text="🗑  Clear Chat", command=self._clear_chat, **btn_cfg
        )
        self.clear_btn.grid(row=2, column=0, pady=4, sticky="ew")

        self.settings_btn = ctk.CTkButton(
            ctrl, text="⚙  Settings", command=self._toggle_settings,
            **btn_cfg
        )
        self.settings_btn.grid(row=3, column=0, pady=(12, 4), sticky="ew")

        # Model badge
        self.model_frame = ctk.CTkFrame(sb, fg_color=CARD_BG, corner_radius=10)
        self.model_frame.grid(row=5, column=0, padx=16, pady=(0, 16), sticky="ew")

        ctk.CTkLabel(
            self.model_frame, text="ACTIVE MODEL", font=("Courier New", 9),
            text_color=TEXT_SEC
        ).pack(anchor="w", padx=12, pady=(10, 2))
        self.model_label = ctk.CTkLabel(
            self.model_frame, text="—", font=("Courier New", 12, "bold"),
            text_color=ACCENT_PURP
        )
        self.model_label.pack(anchor="w", padx=12, pady=(0, 10))

        # Log area
        ctk.CTkLabel(
            sb, text="SYSTEM LOG", font=("Courier New", 9),
            text_color=TEXT_SEC
        ).grid(row=6, column=0, padx=18, pady=(8, 2), sticky="sw")

        self.log_box = ctk.CTkTextbox(
            sb, fg_color=DARK_BG, text_color=TEXT_SEC,
            font=FONT_MONO_SM, wrap="word",
            corner_radius=0, border_width=0,
            state="disabled", height=160,
        )
        self.log_box.grid(row=7, column=0, padx=0, pady=0, sticky="sew")

    # ══════════════════════════════════════════════════════════════════════════
    #  CHAT AREA
    # ══════════════════════════════════════════════════════════════════════════
    def _build_chat_area(self):
        chat_outer = ctk.CTkFrame(self, fg_color=DARK_BG, corner_radius=0)
        chat_outer.grid(row=0, column=1, sticky="nsew")
        chat_outer.grid_rowconfigure(0, weight=1)
        chat_outer.grid_columnconfigure(0, weight=1)

        # Header bar
        header = ctk.CTkFrame(chat_outer, fg_color=PANEL_BG, corner_radius=0, height=56)
        header.grid(row=0, column=0, sticky="new")
        header.grid_propagate(False)

        ctk.CTkLabel(
            header, text="Conversation", font=("Segoe UI", 15, "bold"),
            text_color=TEXT_PRI
        ).pack(side="left", padx=24, pady=0)

        self.proc_label = ctk.CTkLabel(
            header, text="", font=("Courier New", 11),
            text_color=ACCENT_AMBER
        )
        self.proc_label.pack(side="right", padx=24)

        self.ts_label = ctk.CTkLabel(
            header, text="", font=FONT_LABEL, text_color=TEXT_SEC
        )
        self.ts_label.pack(side="right", padx=8)
        self._update_clock()

        # Scrollable chat feed
        self.chat_scroll = ctk.CTkScrollableFrame(
            chat_outer, fg_color=DARK_BG, corner_radius=0,
            scrollbar_button_color=BORDER,
            scrollbar_button_hover_color=ACCENT_PURP,
        )
        self.chat_scroll.grid(row=1, column=0, sticky="nsew", padx=0, pady=0)
        chat_outer.grid_rowconfigure(1, weight=1)
        self.chat_scroll.grid_columnconfigure(0, weight=1)

        # Transcription bar
        self.trans_frame = ctk.CTkFrame(
            chat_outer, fg_color=PANEL_BG, corner_radius=0, height=48
        )
        self.trans_frame.grid(row=2, column=0, sticky="sew")
        self.trans_frame.grid_propagate(False)

        ctk.CTkLabel(
            self.trans_frame, text="●", font=("Segoe UI", 10),
            text_color=ACCENT_CYAN
        ).pack(side="left", padx=(16, 6), pady=0)

        self.trans_label = ctk.CTkLabel(
            self.trans_frame, text="Standing by…",
            font=("Courier New", 11), text_color=TEXT_SEC
        )
        self.trans_label.pack(side="left", pady=0)

        # Initial welcome card
        self._add_system_card(
            "Sugar is ready. Say \"Sugar\" to wake her, "
            "or use the buttons on the left."
        )

    def _add_message_bubble(self, role: str, text: str):
        """Append a styled chat bubble."""
        is_user = role == "user"
        time_str = datetime.now().strftime("%H:%M")

        outer = ctk.CTkFrame(self.chat_scroll, fg_color="transparent")
        outer.pack(fill="x", padx=20, pady=6)
        outer.grid_columnconfigure(0, weight=1)

        if is_user:
            bg       = "#1e2040"
            label_c  = ACCENT_CYAN
            align    = "e"
            name     = "YUKI"
        else:
            bg       = CARD_BG
            label_c  = ACCENT_PURP
            align    = "w"
            name     = "SUGAR"

        # Name + time header
        hdr = ctk.CTkFrame(outer, fg_color="transparent")
        if is_user:
            hdr.pack(anchor="e")
        else:
            hdr.pack(anchor="w")

        ctk.CTkLabel(
            hdr, text=name, font=("Courier New", 10, "bold"),
            text_color=label_c
        ).pack(side="left" if not is_user else "right", padx=(0, 6))

        ctk.CTkLabel(
            hdr, text=time_str, font=FONT_LABEL, text_color=TEXT_SEC
        ).pack(side="left" if not is_user else "right")

        # Bubble
        bubble = ctk.CTkFrame(
            outer, fg_color=bg, corner_radius=12
        )
        if is_user:
            bubble.pack(anchor="e", pady=(2, 0))
        else:
            bubble.pack(anchor="w", pady=(2, 0))

        ctk.CTkLabel(
            bubble, text=text, font=FONT_BODY, text_color=TEXT_PRI,
            wraplength=520, justify="left", padx=14, pady=10
        ).pack()

        # Scroll to bottom
        self.after(50, lambda: self.chat_scroll._parent_canvas.yview_moveto(1.0))

    def _add_system_card(self, text: str):
        card = ctk.CTkFrame(
            self.chat_scroll,
            fg_color=CARD_BG,
            corner_radius=10,
            border_width=1,
            border_color=BORDER,
        )
        card.pack(fill="x", padx=20, pady=8)
        ctk.CTkLabel(
            card, text=text, font=("Courier New", 11),
            text_color=TEXT_SEC, wraplength=640
        ).pack(padx=16, pady=10)

    def _clear_chat(self):
        for w in self.chat_scroll.winfo_children():
            w.destroy()
        self._add_system_card("Chat cleared.")

    # ══════════════════════════════════════════════════════════════════════════
    #  SETTINGS PANEL
    # ══════════════════════════════════════════════════════════════════════════
    def _build_settings_panel(self):
        self.settings_panel = ctk.CTkFrame(
            self, fg_color=PANEL_BG, corner_radius=0, width=300
        )
        # Not gridded initially; toggled via _toggle_settings

        sp = self.settings_panel
        sp.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            sp, text="SETTINGS", font=("Courier New", 13, "bold"),
            text_color=ACCENT_CYAN
        ).grid(row=0, column=0, padx=20, pady=(24, 4), sticky="w")

        ctk.CTkLabel(
            sp, text="Editable live — no restart required",
            font=FONT_LABEL, text_color=TEXT_SEC
        ).grid(row=1, column=0, padx=20, pady=(0, 16), sticky="w")

        row = 2

        def section(label):
            nonlocal row
            ctk.CTkLabel(
                sp, text=label, font=("Courier New", 10),
                text_color=TEXT_SEC
            ).grid(row=row, column=0, padx=20, pady=(14, 2), sticky="w")
            row += 1

        def slider_row(label, from_, to, init, key, fmt="{:.2f}"):
            nonlocal row
            ctk.CTkLabel(sp, text=label, font=FONT_BODY, text_color=TEXT_PRI)\
                .grid(row=row, column=0, padx=20, sticky="w")
            row += 1
            val_var = ctk.StringVar(value=fmt.format(init))
            s_row = ctk.CTkFrame(sp, fg_color="transparent")
            s_row.grid(row=row, column=0, padx=20, pady=(0, 4), sticky="ew")
            s_row.grid_columnconfigure(0, weight=1)

            val_lbl = ctk.CTkLabel(s_row, textvariable=val_var,
                                   font=("Courier New", 11), text_color=ACCENT_CYAN,
                                   width=40)
            val_lbl.grid(row=0, column=1, padx=(8, 0))

            def on_change(v):
                f = round(float(v), 2)
                self.settings[key] = f
                val_var.set(fmt.format(f))

            sl = ctk.CTkSlider(
                s_row, from_=from_, to=to, command=on_change,
                progress_color=ACCENT_CYAN, button_color=ACCENT_PURP,
                button_hover_color=ACCENT_CYAN,
            )
            sl.set(init)
            sl.grid(row=0, column=0, sticky="ew")
            row += 1

        # TTS Speed
        section("TTS")
        slider_row("Speed", 0.5, 2.0, DEFAULT_TTS_SPEED, "tts_speed")

        # Voice selector
        ctk.CTkLabel(sp, text="Voice", font=FONT_BODY, text_color=TEXT_PRI)\
            .grid(row=row, column=0, padx=20, sticky="w")
        row += 1
        voices = ["EN-Newest", "EN-US", "EN-BR", "EN-AU", "EN-INDIA", "EN-Default"]
        voice_var = ctk.StringVar(value=DEFAULT_VOICE)

        def on_voice(v):
            self.settings["voice"] = v

        ctk.CTkOptionMenu(
            sp, values=voices, variable=voice_var, command=on_voice,
            fg_color=CARD_BG, button_color=ACCENT_PURP,
            button_hover_color=ACCENT_CYAN, text_color=TEXT_PRI,
        ).grid(row=row, column=0, padx=20, pady=(0, 8), sticky="ew")
        row += 1

        # VAD sensitivity
        section("VOICE DETECTION")
        slider_row("Sensitivity", 0.1, 0.9, DEFAULT_SENSITIVITY, "sensitivity")

        ctk.CTkLabel(
            sp, text="(lower = more sensitive)",
            font=FONT_LABEL, text_color=TEXT_SEC
        ).grid(row=row, column=0, padx=20, pady=(0, 4), sticky="w")
        row += 1

        # System prompt
        section("SYSTEM PROMPT")
        self.prompt_box = ctk.CTkTextbox(
            sp, height=180, fg_color=CARD_BG, text_color=TEXT_PRI,
            font=FONT_MONO_SM, wrap="word",
            border_color=BORDER, border_width=1, corner_radius=8,
        )
        self.prompt_box.grid(row=row, column=0, padx=20, pady=(0, 6), sticky="ew")
        self.prompt_box.insert("end", DEFAULT_MASTER_PROMPT)
        row += 1

        ctk.CTkButton(
            sp, text="Apply Prompt",
            fg_color=ACCENT_PURP, hover_color=ACCENT_CYAN,
            text_color=DARK_BG, font=("Segoe UI", 12, "bold"),
            corner_radius=8, height=36,
            command=self._apply_prompt,
        ).grid(row=row, column=0, padx=20, pady=(0, 16), sticky="ew")
        row += 1

        # Wake words info
        section("WAKE WORDS")
        ctk.CTkLabel(
            sp, text='"Sugar"  ·  "Wake up"  ·  "So go"',
            font=("Courier New", 11), text_color=ACCENT_GREEN
        ).grid(row=row, column=0, padx=20, pady=(0, 4), sticky="w")
        row += 1

        section("SLEEP COMMANDS")
        ctk.CTkLabel(
            sp, text='"Go to sleep"  ·  "Standby"',
            font=("Courier New", 11), text_color=ACCENT_AMBER
        ).grid(row=row, column=0, padx=20, pady=(0, 4), sticky="w")

    def _toggle_settings(self):
        if self._settings_open:
            self.settings_panel.grid_forget()
            self.grid_columnconfigure(2, minsize=0)
            self._settings_open = False
            self.settings_btn.configure(text="⚙  Settings")
        else:
            self.settings_panel.grid(row=0, column=2, sticky="nsew")
            self.grid_columnconfigure(2, minsize=300)
            self._settings_open = True
            self.settings_btn.configure(text="✕  Close Settings")

    def _apply_prompt(self):
        txt = self.prompt_box.get("1.0", "end").strip()
        self.settings["master_prompt"] = txt
        self._log("System prompt updated.")
        self._add_system_card("✓ System prompt updated.")

    # ══════════════════════════════════════════════════════════════════════════
    #  LOG HELPER
    # ══════════════════════════════════════════════════════════════════════════
    def _log(self, msg: str):
        ts = datetime.now().strftime("%H:%M:%S")
        self.log_box.configure(state="normal")
        self.log_box.insert("end", f"{ts}  {msg}\n")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    # ══════════════════════════════════════════════════════════════════════════
    #  STATUS PULSE
    # ══════════════════════════════════════════════════════════════════════════
    def _draw_pulse(self, color: str):
        self.pulse_canvas.delete("all")
        self.pulse_canvas.create_oval(1, 1, 13, 13, fill=color, outline="")

    def _pulse_loop(self):
        """Animate the status dot when awake + listening."""
        if self.client and self.client.is_awake and self.client.listening:
            self._pulse_phase = (self._pulse_phase + 1) % 20
            bright = self._pulse_phase < 10
            self._draw_pulse(ACCENT_GREEN if bright else "#1a7a45")
        self.after(120, self._pulse_loop)

    # ══════════════════════════════════════════════════════════════════════════
    #  CLOCK
    # ══════════════════════════════════════════════════════════════════════════
    def _update_clock(self):
        self.ts_label.configure(text=datetime.now().strftime("%A  %H:%M"))
        self.after(5000, self._update_clock)

    # ══════════════════════════════════════════════════════════════════════════
    #  CLIENT INITIALISATION (runs in background thread)
    # ══════════════════════════════════════════════════════════════════════════
    def _init_client(self):
        try:
            self.client = VoiceClient(self.event_q, self.settings)
            self.client.start()
            self.event_q.put({"kind": "ready"})
        except Exception as e:
            self.event_q.put({"kind": "error", "payload": str(e)})

    # ══════════════════════════════════════════════════════════════════════════
    #  EVENT POLLING  (runs in UI thread via after())
    # ══════════════════════════════════════════════════════════════════════════
    def _poll_events(self):
        try:
            while True:
                ev = self.event_q.get_nowait()
                kind    = ev["kind"]
                payload = ev.get("payload")

                if kind == "ready":
                    self._on_ready()

                elif kind == "error":
                    self._log(f"ERROR: {payload}")
                    self._add_system_card(f"⚠ Init error: {payload}")
                    self.status_label.configure(text="ERROR", text_color=ACCENT_RED)

                elif kind == "log":
                    self._log(payload)

                elif kind == "status":
                    self._update_status(payload)

                elif kind == "message":
                    self._add_message_bubble(payload["role"], payload["text"])

                elif kind == "model":
                    self.model_label.configure(text=payload)

                elif kind == "processing":
                    self.proc_label.configure(
                        text="⟳  processing…" if payload else ""
                    )
                    self.trans_label.configure(
                        text="Processing…" if payload else "Listening…"
                    )

                elif kind == "code":
                    print("\n\033[34m[CODE OUTPUT]\033[0m")
                    print(payload)
                    self._add_system_card(
                        "Code block printed to terminal. See console output."
                    )

        except queue.Empty:
            pass
        self.after(40, self._poll_events)

    # ══════════════════════════════════════════════════════════════════════════
    #  STATUS UPDATES
    # ══════════════════════════════════════════════════════════════════════════
    def _on_ready(self):
        self._log("All models loaded. Ready.")
        self.status_label.configure(text="SLEEPING", text_color=ACCENT_AMBER)
        self.status_sub.configure(text='say "Sugar" to wake')
        self._draw_pulse(ACCENT_AMBER)
        self.trans_label.configure(text='Say "Sugar" to wake\u2026')

    def _update_status(self, payload: dict):
        awake     = payload.get("awake", False)
        listening = payload.get("listening", False)

        if awake and listening:
            text, color, sub = "AWAKE", ACCENT_GREEN, "listening"
            self._draw_pulse(ACCENT_GREEN)
        elif awake and not listening:
            text, color, sub = "PROCESSING", ACCENT_AMBER, "thinking…"
            self._draw_pulse(ACCENT_AMBER)
        else:
            text, color, sub = "SLEEPING", TEXT_SEC, 'say "Sugar" to wake'
            self._draw_pulse(ACCENT_RED)

        self.status_label.configure(text=text, text_color=color)
        self.status_sub.configure(text=sub)

        # Button states
        awake_state = "disabled" if awake else "normal"
        sleep_state = "normal"  if awake else "disabled"
        self.wake_btn.configure(state=awake_state)
        self.sleep_btn.configure(state=sleep_state)

    # ══════════════════════════════════════════════════════════════════════════
    #  MANUAL CONTROLS
    # ══════════════════════════════════════════════════════════════════════════
    def _manual_wake(self):
        if self.client:
            self.client.force_wake()
            self._add_system_card("Manually woken.")

    def _manual_sleep(self):
        if self.client:
            self.client.force_sleep()
            self._add_system_card("Sent to sleep.")

    # ══════════════════════════════════════════════════════════════════════════
    #  WINDOW CLOSE
    # ══════════════════════════════════════════════════════════════════════════
    def on_close(self):
        if self.client:
            self.client.stop()
        self.destroy()


# ══════════════════════════════════════════════════════════════════════════════
#  ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    app = SugarApp()
    app.protocol("WM_DELETE_WINDOW", app.on_close)
    app.mainloop()