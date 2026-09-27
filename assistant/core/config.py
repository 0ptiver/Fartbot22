"""Configuration loading: config/config.yaml -> typed settings."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = ROOT / "config" / "config.yaml"
# Your personal overrides (git-ignored, so updates never clash with your settings).
LOCAL_CONFIG_PATH = ROOT / "config" / "local.yaml"
MODELS_DIR = ROOT / "models"


class AssistantConfig(BaseModel):
    name: str = "Nova"
    wake_phrase: str = "hey nova"
    address_user_as: str = "sir"
    timezone: str = "America/Chicago"
    personality: str = "british_butler"


class WebSearchConfig(BaseModel):
    enabled: bool = True
    max_uses: int = 3        # Claude API server tool (anthropic backend)
    max_results: int = 5     # free DuckDuckGo search (local backend)


class LocalLLMConfig(BaseModel):
    host: str = "http://127.0.0.1:11434"
    model: str = "qwen3:4b-instruct-2507-q4_K_M"
    vision_model: str = "qwen3-vl:4b"   # used for "what's on my screen" when vision: ollama
    vision: str = "claude_code"         # claude_code (subscription, no VRAM) | ollama (local)
    vision_max_px: int = 1024           # screenshots are shrunk to this for the local vision model
    vision_keep_alive: str = "2m"       # unload soon after; it competes for VRAM with the chat model
    num_ctx: int = 8192
    keep_alive: str = "30m"             # keep the model in VRAM between turns
    think: bool = False                 # Qwen3 "thinking" adds seconds of silence; off for voice
    temperature: float = 0.6
    timeout_s: float = 30               # a stalled Ollama gives up instead of freezing Nova
    fast_commands: bool = True          # "pause", "what's playing", "play X": no model call


class ClaudeCodeConfig(BaseModel):
    command: str = "claude"             # found on PATH; or a full path to claude.exe
    model: str | None = None            # None = your plan's default; or "sonnet" / "opus"
    timeout_s: float = 300
    max_turns: int = 20
    # Built-in Claude Code tools that exist in the hand-off session. No Bash, no Edit/Write.
    tools: list[str] = Field(default_factory=lambda: ["WebSearch", "WebFetch", "Read"])
    # Tools pre-approved everywhere. Read is deliberately NOT here: in dontAsk mode it can
    # only read inside its own empty working folder, never your other files.
    allowed_tools: list[str] = Field(default_factory=lambda: ["WebSearch", "WebFetch"])


class ExpertConfig(BaseModel):
    backend: str = "claude_code"        # claude_code (your Claude subscription) | anthropic (API)
    claude_code: ClaudeCodeConfig = Field(default_factory=ClaudeCodeConfig)


class BrainConfig(BaseModel):
    backend: str = "local"              # local (Ollama, free) | anthropic (Claude API, paid)
    local: LocalLLMConfig = Field(default_factory=LocalLLMConfig)
    expert: ExpertConfig = Field(default_factory=ExpertConfig)
    chat_model: str = "claude-haiku-4-5"
    expert_model: str = "claude-opus-5"
    chat_max_tokens: int = 1024
    expert_max_tokens: int = 16000
    max_tool_rounds: int = 8
    history_turns: int = 20
    web_search: WebSearchConfig = Field(default_factory=WebSearchConfig)


class VADConfig(BaseModel):
    threshold: float = 0.5
    end_silence_ms: int = 400
    min_speech_ms: int = 150
    preroll_ms: int = 300
    max_utterance_s: float = 30
    pause_ms: int = 160              # start transcribing after this much silence (speculative STT)


class WhisperConfig(BaseModel):
    model: str = "large-v3-turbo"
    device: str = "auto"
    compute_type: str = "auto"
    beam_size: int = 1
    language: str | None = "en"
    hotwords: str | None = None       # words to bias towards; wake mode adds the assistant's name


class DeepgramConfig(BaseModel):
    model: str = "nova-3"


class STTConfig(BaseModel):
    provider: str = "whisper"
    whisper: WhisperConfig = Field(default_factory=WhisperConfig)
    deepgram: DeepgramConfig = Field(default_factory=DeepgramConfig)


class KokoroConfig(BaseModel):
    voice: str = "bm_george"
    lang: str = "en-gb"
    speed: float = 1.05
    device: str = "auto"          # auto (GPU if onnxruntime-gpu works, else CPU) | cuda | cpu
    threads: int | None = None    # CPU threads; None = onnxruntime default. See: assistant ttsbench
    model_file: str = "auto"      # auto = GPU-optimized copy on the GPU, original on the CPU
    cuda_conv_search: str = "heuristic"    # heuristic | default | exhaustive (see ttsbench)


class TTSConfig(BaseModel):
    provider: str = "kokoro"
    kokoro: KokoroConfig = Field(default_factory=KokoroConfig)


class WakeConfig(BaseModel):
    # What Whisper might write when you say the name. Checked near the start or end of a sentence.
    variants: list[str] = Field(default_factory=lambda: [
        "nova", "novah", "novo", "nover", "no va", "noah va", "know va"])
    window_words: int = 3
    follow_up_s: float = 6.0          # after a reply to "Nova, ...", one more request without the name
    acknowledgement: str = "Yes, sir?"  # said when you only say the name
    echo_overlap: float = 0.6         # ignore what sounds like its own voice from the speakers
    echo_window_s: float = 2.0        # ...but only this soon after it stopped talking
    cooldown_ms: int = 350            # ignore the mic briefly after it finishes speaking


class BargeInConfig(BaseModel):
    # verified: interrupt only for words that aren't Nova's own voice (works with speakers)
    # fast: interrupt as soon as you speak (headphones only; speakers would trigger it)
    # off: never interrupt by voice
    mode: str = "verified"
    fast_min_ms: int = 250           # fast mode: speech needed before interrupting
    check_every_s: float = 0.8       # verified mode: how often to check speech over Nova's voice
    min_new_words: int = 2           # verified mode: words needed (besides stop words / the name)
    stop_words: list[str] = Field(default_factory=lambda: [
        "stop", "stop it", "stop talking", "cancel", "never mind", "nevermind", "shut up",
        "quiet", "be quiet", "enough", "that's enough", "okay stop", "ok stop", "wait"])
    merge_window_s: float = 2.5      # speak again this soon, before it answers -> one request


class AECConfig(BaseModel):
    enabled: bool = True             # remove Nova's own voice from the mic (WebRTC AEC3)
    delay_ms: int = 60               # speaker->mic delay hint; the canceller also estimates it
    noise_suppression: bool = False


class VoiceConfig(BaseModel):
    mode: str = "wake"
    aec: AECConfig = Field(default_factory=AECConfig)
    barge_in: BargeInConfig = Field(default_factory=BargeInConfig)
    wake: WakeConfig = Field(default_factory=WakeConfig)
    ptt_hotkey: str = "ctrl+alt+space"
    input_device: str | int | None = None
    output_device: str | int | None = None
    vad: VADConfig = Field(default_factory=VADConfig)
    stt: STTConfig = Field(default_factory=STTConfig)
    tts: TTSConfig = Field(default_factory=TTSConfig)
    confirm_timeout_s: float = 12.0     # how long Nova waits for "yes"/"no" before cancelling
    dictation_timeout_s: float = 120.0  # dictation switches itself off after this much quiet
    ack_sound: bool = True              # a soft tick the moment Nova accepts a request
    still_working_s: float = 1.8        # nothing said by then? "One moment, sir." (0 = never)
    first_chunk_min_chars: int = 12
    # Out loud, replies stop after this many sentences (the whole reply is still in the window).
    max_spoken_sentences: int = 3
    max_spoken_sentences_detail: int = 10   # when you ask for detail ("explain", "tell me about")
    latency_report: bool = True
    # Said when a slow tool starts and nothing has been said yet this turn.
    filler_phrases: list[str] = Field(default_factory=lambda: [
        "One moment, sir.", "On it, sir.", "Right away, sir. Give me a moment."])
    filler_tools: list[str] = Field(default_factory=lambda: [
        "escalate", "look_at_screen", "web_search"])


class ServerConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8765
    # Host headers the server answers to (blocks DNS-rebinding attacks from web pages).
    allowed_hosts: list[str] = Field(default_factory=lambda: ["127.0.0.1", "localhost"])
    # Browser origins allowed to connect (the HUD/PWA get added in later phases).
    allowed_origins: list[str] = Field(default_factory=list)


class MemoryConfig(BaseModel):
    enabled: bool = True
    path: str = "data/memory.db"        # on this PC only
    per_turn: int = 5                   # relevant memories shown to the model per request


class HudConfig(BaseModel):
    enabled: bool = True                # the interactive window, started with `assistant voice`
    port: int = 8766                    # 127.0.0.1 only
    open_window: bool = True            # open it automatically (Edge app window)
    show_ignored: bool = False          # list speech Nova ignored in the activity feed


class PhoneConfig(BaseModel):
    # Phone access over Tailscale. Switched on/off (and set up) in the window's Phone page.
    port: int = 8767                    # on the PC's Tailscale address only
    session_days: int = 30              # a phone stays signed in this long


class ScreenshotConfig(BaseModel):
    max_edge_px: int = 1568
    jpeg_quality: int = 80


class ToolsConfig(BaseModel):
    app_aliases: dict[str, str] = Field(default_factory=dict)
    screenshot: ScreenshotConfig = Field(default_factory=ScreenshotConfig)
    grid_cols: int = 10                 # voice mouse grid over the main screen
    grid_rows: int = 6


class SafetyConfig(BaseModel):
    # The only folders Nova's file tools may touch (plus their OneDrive copies).
    allowed_folders: list[str] = Field(default_factory=lambda: [
        "~/Desktop", "~/Documents", "~/Downloads", "~/Music", "~/Pictures", "~/Videos"])
    notes_folder: str = "~/Documents/Nova Notes"
    risk_overrides: dict[str, str] = Field(default_factory=dict)
    # Never from a phone or another PC (secure by default, even without config.yaml).
    remote_blocked_tools: list[str] = Field(default_factory=lambda: [
        "run_shell", "delete_file", "move_file", "power", "press_key", "window", "mouse", "mouse_grid",
        "type_text", "press_keys", "click_element", "show_numbers", "forget", "teach", "replay_click",
        "replay_keys", "run_routine", "voice_lock", "dictation"])
    audit_log: str = "data/audit.jsonl"

    def audit_path(self) -> Path:
        p = Path(self.audit_log)
        return p if p.is_absolute() else ROOT / p


class RoutineStep(BaseModel):
    tool: str | None = None             # a tool name, e.g. open_app
    args: dict[str, Any] = Field(default_factory=dict)
    wait: float = 0                     # pause (seconds, max 30) instead of a tool
    optional: bool = False              # don't mention it if this step fails


class RoutineConfig(BaseModel):
    phrases: list[str] = Field(default_factory=list)   # what you say, e.g. "gaming mode"
    reply: str = ""                                     # said when it's done
    steps: list[RoutineStep] = Field(default_factory=list)


class Settings(BaseModel):
    assistant: AssistantConfig = Field(default_factory=AssistantConfig)
    brain: BrainConfig = Field(default_factory=BrainConfig)
    voice: VoiceConfig = Field(default_factory=VoiceConfig)
    server: ServerConfig = Field(default_factory=ServerConfig)
    tools: ToolsConfig = Field(default_factory=ToolsConfig)
    safety: SafetyConfig = Field(default_factory=SafetyConfig)
    hud: HudConfig = Field(default_factory=HudConfig)
    phone: PhoneConfig = Field(default_factory=PhoneConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    # Set a routine to null in local.yaml to switch a built-in one off.
    routines: dict[str, RoutineConfig | None] = Field(default_factory=dict)


def _deep_merge(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for k, v in over.items():
        out[k] = _deep_merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def _read_yaml(p: Path) -> dict[str, Any]:
    return (yaml.safe_load(p.read_text(encoding="utf-8")) or {}) if p.exists() else {}


def load_settings(path: str | Path | None = None, local_path: str | Path | None = None) -> Settings:
    """config/config.yaml (defaults, updated with the code) + config/local.yaml (yours)."""
    data = _read_yaml(Path(path) if path else DEFAULT_CONFIG_PATH)
    if path is None or local_path is not None:
        data = _deep_merge(data, _read_yaml(Path(local_path) if local_path else LOCAL_CONFIG_PATH))
    return Settings.model_validate(data)
