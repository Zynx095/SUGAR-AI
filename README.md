# SUGAR-AI 🍬

## Offline-First Multimodal AI Assistant for Windows

SUGAR-AI is a high-performance local AI assistant focused on real-time interaction, privacy, and modular extensibility. Designed for engineers, creators, and hardware tinkerers, the system combines local LLM inference, advanced speech recognition, natural voice synthesis, automation workflows, and media integration into a single desktop assistant architecture.

Built with an offline-first philosophy, SUGAR-AI minimizes cloud dependency while maintaining low-latency responsiveness and system-level control.

---

# ✨ Features

### 🎙️ Real-Time Speech Recognition

Powered by `faster-whisper` using the `large-v3-turbo` model for fast and accurate local speech-to-text processing.

### 🧠 Local LLM Backend

Runs entirely through `Ollama`, supporting models such as:

* Llama 3
* Mistral
* Phi
* Gemma
* Other local GGUF-compatible models

### 🔊 Natural Voice Synthesis

Uses `MeloTTS` for expressive, human-like speech output.

### 🎵 Spotify Integration

Integrated with the official Spotify Web API for:

* Playback control
* Search
* Pause/Resume
* Track skipping
* Current track retrieval

### 🖥️ System Automation

Voice-triggered desktop workflows powered by `pyautogui`.

Examples:

* Open applications
* Control media
* Trigger desktop shortcuts
* Launch development tools

### 🛡️ Privacy-Focused Design

* Local inference
* Secure `.env` credential handling
* Voice activity detection using `webrtcvad`
* Minimal cloud dependency

### 🧪 Experimental Hardware Integration

Ongoing work toward wearable assistant integration and smart-glasses compatibility.

---

# 🛠️ Tech Stack

| Component                | Technology       |
| ------------------------ | ---------------- |
| UI Framework             | `customtkinter`  |
| Local Inference          | `Ollama`         |
| Speech-to-Text           | `faster-whisper` |
| Text-to-Speech           | `MeloTTS`        |
| Media Integration        | `spotipy`        |
| Voice Activity Detection | `webrtcvad`      |
| Automation               | `pyautogui`      |

---

# 🚀 Installation

## 1. Prerequisites

Install the following before setup:

* Python 3.10+
* Ollama
  https://ollama.com/
* Spotify Developer Account
  https://developer.spotify.com/dashboard

---

## 2. Clone Repository

```bash
git clone https://github.com/YOUR_USERNAME/SUGAR-AI.git
cd SUGAR-AI
```

---

## 3. Create Virtual Environment

### Windows

```bash
python -m venv venv
venv\Scripts\activate
```

### Linux / macOS

```bash
python -m venv venv
source venv/bin/activate
```

---

## 4. Install Dependencies

```bash
pip install -r requirements.txt
```

---

# ⚙️ Environment Configuration

Create a `.env` file in the root directory:

```env
SPOTIFY_CLIENT_ID=your_client_id
SPOTIFY_CLIENT_SECRET=your_client_secret
```

> Ensure your Spotify Redirect URI matches the value configured internally in the application.

---

# ▶️ Running SUGAR-AI

Ensure Ollama is running in the background first.

Then launch:

```bash
python main.py
```

---

# 🎮 Example Commands

```text
"Sugar, play Master of Puppets."
"Sugar, what song is playing?"
"Sugar, open VS Code."
"Sugar, pause the music."
```

---

# 🗺️ Development Roadmap

## Completed

* [x] Windows migration and compatibility layer
* [x] Official Spotify API integration
* [x] Secure environment variable architecture
* [x] Local speech recognition pipeline
* [x] Modular voice assistant framework

## In Progress

* [ ] AI-powered VS Code drafting engine
* [ ] Memory-enhanced conversational workflows
* [ ] Smart glasses / wearable integration
* [ ] Multi-agent task routing
* [ ] Animated assistant UI system
* [ ] Multi-voice support

---

# 🧠 Architecture Philosophy

SUGAR-AI is designed around:

* modular AI pipelines
* offline-first execution
* hardware extensibility
* low-latency interaction
* local ownership of data and inference

The long-term goal is to evolve SUGAR-AI into a fully integrated personal AI operating layer capable of desktop control, contextual memory, and wearable deployment.

---

# 🙌 Credits & Inspiration

This project was inspired in part by the following open-source project:

Jarvis MLX by Huw Prosser
https://github.com/huwprosser/jarvis-mlx

SUGAR-AI significantly extends the original concept through:

* Windows platform adaptation
* Modified architecture and workflows
* Spotify API integration
* Additional assistant capabilities
* Experimental wearable integration
* Expanded local assistant functionality

---

# 👤 Author

## Yukith M Joseph

Engineering Student @ Presidency University, Bengaluru

Focused on:

* AI Systems
* Robotics
* Hardware Engineering
* Intelligent Assistive Interfaces
* Human-AI Interaction

---

# 📄 License & Usage

This repository is intended for educational, research, and personal development purposes.

Third-party models, frameworks, and dependencies remain subject to their respective licenses and ownership.

Users are responsible for ensuring compliance with:

* Meta Llama licenses
* Spotify API Terms of Service
* Ollama model licensing
* Any additional upstream dependencies

---

# ⭐ Future Vision

SUGAR-AI is evolving toward a fully local AI ecosystem:

* desktop intelligence
* wearable computing
* contextual memory
* multimodal interaction
* autonomous workflows

A personal AI that feels less like software —
and more like a digital companion layer woven into the operating system itself.
