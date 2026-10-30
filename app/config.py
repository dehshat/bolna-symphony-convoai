"""Runtime configuration, read from environment variables (and an optional local .env file)."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader so local runs work without python-dotenv. Real env vars win."""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


_load_dotenv(ROOT / ".env")


def _bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except ValueError:
        return default


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except ValueError:
        return default


def _json_map(name: str) -> dict:
    raw = os.getenv(name, "").strip()
    if not raw:
        return {}
    try:
        value = json.loads(raw)
        return value if isinstance(value, dict) else {}
    except json.JSONDecodeError:
        return {}


def _public_url() -> str:
    """PUBLIC_BASE_URL, else the domain Railway/Render inject automatically."""
    url = os.getenv("PUBLIC_BASE_URL", "").strip()
    if not url and os.getenv("RAILWAY_PUBLIC_DOMAIN"):
        url = "https://" + os.getenv("RAILWAY_PUBLIC_DOMAIN", "").strip()
    if not url:
        url = os.getenv("RENDER_EXTERNAL_URL", "").strip()
    return url.rstrip("/")


@dataclass
class Settings:
    # simulation | hybrid | live  (the UI can override per session)
    default_mode: str = field(default_factory=lambda: os.getenv("EXECUTION_MODE", "simulation").lower())
    public_base_url: str = field(default_factory=_public_url)
    # Create/update the Bolna agents on every boot (needs BOLNA_API_KEY and a public URL)
    auto_sync_bolna: bool = field(default_factory=lambda: _bool("AUTO_SYNC_BOLNA_AGENT", True))

    # --- Bolna ---
    bolna_api_key: str = field(default_factory=lambda: os.getenv("BOLNA_API_KEY", ""))
    bolna_api_base: str = field(default_factory=lambda: os.getenv("BOLNA_API_BASE", "https://api.bolna.ai").rstrip("/"))
    # Single agent id, or a per-language map like {"hi": "...", "kn": "..."}
    bolna_agent_id: str = field(default_factory=lambda: os.getenv("BOLNA_AGENT_ID", ""))
    bolna_agent_ids: dict = field(default_factory=lambda: _json_map("BOLNA_AGENT_IDS"))
    bolna_from_number: str = field(default_factory=lambda: os.getenv("BOLNA_FROM_NUMBER", ""))
    bolna_telephony_provider: str = field(default_factory=lambda: os.getenv("BOLNA_TELEPHONY_PROVIDER", "plivo"))
    bolna_llm_provider: str = field(default_factory=lambda: os.getenv("BOLNA_LLM_PROVIDER", "openai"))
    # gpt-5.4-mini is Bolna's recommended voice model (fastest TTFT). reasoning_effort "none" skips thinking
    # for the lowest latency; verbosity "low" keeps spoken turns short. If Bolna rejects it, the sync first drops
    # only the setting it names, then steps down BOLNA_LLM_FALLBACK_MODELS one model at a time.
    bolna_llm_model: str = field(default_factory=lambda: os.getenv("BOLNA_LLM_MODEL", "gpt-5.4-mini"))
    bolna_llm_reasoning_effort: str = field(default_factory=lambda: os.getenv("BOLNA_LLM_REASONING_EFFORT", "none"))
    bolna_llm_verbosity: str = field(default_factory=lambda: os.getenv("BOLNA_LLM_VERBOSITY", "low"))
    bolna_llm_fallback_model: str = field(default_factory=lambda: os.getenv("BOLNA_LLM_FALLBACK_MODEL",
                                                                            "gpt-4.1-mini"))
    bolna_llm_fallback_models: str = field(default_factory=lambda: os.getenv("BOLNA_LLM_FALLBACK_MODELS", "gpt-4.1"))
    # What the last agent sync actually ended up using, and why (shown in ⚙︎ Setup)
    bolna_llm_active: str = ""
    bolna_llm_note: str = ""
    # Per-device hourly limits on what costs money (0 = off): map searches, real-call sessions, chat messages
    rate_limit_search: int = field(default_factory=lambda: _int("RATE_LIMIT_SEARCH_PER_HOUR", 60))
    rate_limit_calls: int = field(default_factory=lambda: _int("RATE_LIMIT_CALLS_PER_HOUR", 20))
    rate_limit_chat: int = field(default_factory=lambda: _int("RATE_LIMIT_CHAT_PER_HOUR", 300))
    rate_limit_voice: int = field(default_factory=lambda: _int("RATE_LIMIT_VOICE_PER_HOUR", 120))
    # Render's free plan sleeps after 15 idle minutes. >0 pings our own public URL this often to stay awake.
    keep_awake_minutes: int = field(default_factory=lambda: _int("KEEP_AWAKE_MINUTES", 0))
    # How many of the best-rated providers the voice flow calls automatically (the user can change the pick)
    auto_call_count: int = field(default_factory=lambda: max(1, min(5, _int("AUTO_CALL_COUNT", 3))))
    # Counter-offers per call before taking the vendor's best price (or walking away above the ceiling)
    max_counter_rounds: int = field(default_factory=lambda: max(0, _int("MAX_COUNTER_ROUNDS", 1)))
    # Agents created on boot. Any of the 11 call languages not listed is created the first time a call needs it.
    bolna_agent_languages: str = field(default_factory=lambda: os.getenv("BOLNA_AGENT_LANGUAGES", "hi,kn"))
    # Turn-taking on live calls: wait this long after the vendor stops before treating it as the end of their turn
    # (vendors pause mid-number: "teen hazaar... paanch sau"), then pause briefly before answering; the vendor must
    # say this many words to interrupt the agent (so "haan", "ji" don't cut it off).
    call_endpointing_ms: int = field(default_factory=lambda: _int("CALL_ENDPOINTING_MS", 900))
    call_response_delay_ms: int = field(default_factory=lambda: _int("CALL_RESPONSE_DELAY_MS", 400))
    call_interruption_words: int = field(default_factory=lambda: _int("CALL_INTERRUPTION_WORDS", 3))
    call_hangup_silence_s: int = field(default_factory=lambda: _int("CALL_HANGUP_SILENCE_SECONDS", 20))
    call_max_seconds: int = field(default_factory=lambda: _int("CALL_MAX_SECONDS", 240))
    # Sarvam models/voice used by the Bolna agent (bulbul:v2 and saarika were retired by Sarvam)
    sarvam_tts_model: str = field(default_factory=lambda: os.getenv("SARVAM_TTS_MODEL", "bulbul:v3"))
    sarvam_tts_voice: str = field(default_factory=lambda: os.getenv("SARVAM_TTS_VOICE", "shubh"))
    sarvam_asr_model: str = field(default_factory=lambda: os.getenv("SARVAM_ASR_MODEL", "saaras:v4"))
    # "var" = {"$var": "x"} type-safe markers, "legacy" = "%(x)s" string templates
    bolna_tool_param_style: str = field(default_factory=lambda: os.getenv("BOLNA_TOOL_PARAM_STYLE", "var"))
    tool_shared_secret: str = field(default_factory=lambda: os.getenv("TOOL_SHARED_SECRET", ""))
    admin_token: str = field(default_factory=lambda: os.getenv("ADMIN_TOKEN", ""))

    # --- Provider search + calling safety ---
    # Google Maps Platform key with "Places API (New)" enabled. The AI Studio key does NOT work for Places.
    google_maps_api_key: str = field(default_factory=lambda: os.getenv("GOOGLE_MAPS_API_KEY", ""))
    # Off by default: every provider found on the map is dialled on the test phone instead of their real number
    call_real_numbers: bool = field(default_factory=lambda: _bool("CALL_REAL_NUMBERS", False))
    test_phone_override: str = field(default_factory=lambda: os.getenv("TEST_PHONE_OVERRIDE", ""))
    # parallel | sequential | auto (auto = sequential when several legs would ring the same phone)
    dial_strategy: str = field(default_factory=lambda: os.getenv("DIAL_STRATEGY", "auto").lower())

    # Phone numbers for hybrid/live mode (E.164, e.g. +919876543210)
    live_test_phone: str = field(default_factory=lambda: os.getenv("LIVE_TEST_PHONE", ""))
    live_vendor_phones: str = field(default_factory=lambda: os.getenv("LIVE_VENDOR_PHONES", ""))

    # --- AI text model for simulated vendors + brief parsing (Google AI Studio / Gemini) ---
    gemini_api_key: str = field(default_factory=lambda: os.getenv("GEMINI_API_KEY", "") or os.getenv("GOOGLE_API_KEY", ""))
    gemini_model: str = field(default_factory=lambda: os.getenv("GEMINI_MODEL", "gemini-2.5-flash"))
    llm_enabled: bool = field(default_factory=lambda: _bool("LLM_ENABLED", True))
    # Same GEMINI_API_KEY, a different model per job. The free tier's daily quota is per model, so the concierge you
    # talk to, the translator and the voice never use up each other's quota. Each list is tried in order; a model that
    # doesn't exist for your key is skipped, a rate-limited one is retried later. Gemma has a large free quota.
    gemini_chat_models: str = field(default_factory=lambda: os.getenv(
        "GEMINI_CHAT_MODELS", "gemini-2.5-flash-lite,gemini-2.0-flash,gemini-3-flash-preview,gemini-2.5-flash,"
                              "gemma-3-27b-it"))
    gemini_localize_models: str = field(default_factory=lambda: os.getenv(
        "GEMINI_LOCALIZE_MODELS", "gemini-2.5-flash,gemini-3-flash-preview,gemini-2.5-flash-lite,gemma-3-27b-it"))
    gemini_stt_models: str = field(default_factory=lambda: os.getenv(
        "GEMINI_STT_MODELS", "gemini-2.5-flash-lite,gemini-2.0-flash,gemini-2.5-flash"))
    gemini_tts_models: str = field(default_factory=lambda: os.getenv(
        "GEMINI_TTS_MODELS", "gemini-2.5-flash-preview-tts,gemini-2.5-flash-tts,gemini-2.5-pro-preview-tts"))
    gemini_tts_voice: str = field(default_factory=lambda: os.getenv("GEMINI_TTS_VOICE", "Kore"))
    # Gemini rewording of simulated call lines (~25 requests per simulation). Off by default because the
    # free tier allows only ~20 requests/day/model; brief parsing still uses Gemini.
    sim_llm_rephrase: bool = field(default_factory=lambda: _bool("SIM_LLM_REPHRASE", False))

    # --- Payments + WhatsApp ---
    razorpay_key_id: str = field(default_factory=lambda: os.getenv("RAZORPAY_KEY_ID", ""))
    razorpay_key_secret: str = field(default_factory=lambda: os.getenv("RAZORPAY_KEY_SECRET", ""))
    token_advance_percent: float = field(default_factory=lambda: _float("TOKEN_ADVANCE_PERCENT", 10))
    whatsapp_token: str = field(default_factory=lambda: os.getenv("WHATSAPP_TOKEN", ""))
    whatsapp_phone_number_id: str = field(default_factory=lambda: os.getenv("WHATSAPP_PHONE_NUMBER_ID", ""))
    buyer_whatsapp: str = field(default_factory=lambda: os.getenv("BUYER_WHATSAPP", ""))

    # --- Orchestration ---
    wave_stagger_seconds: float = field(default_factory=lambda: _float("WAVE_STAGGER_SECONDS", 8))
    hybrid_live_delay_seconds: float = field(default_factory=lambda: _float("HYBRID_LIVE_DELAY_SECONDS", 6))
    sim_speed: float = field(default_factory=lambda: _float("SIM_SPEED", 1.0))
    db_path: str = field(default_factory=lambda: os.getenv("DB_PATH", str(ROOT / "data" / "auction.db")))

    def agent_id_for(self, language: str) -> str:
        return self.bolna_agent_ids.get(language) or self.bolna_agent_ids.get("default") or self.bolna_agent_id

    @property
    def bolna_ready(self) -> bool:
        return bool(self.bolna_api_key and (self.bolna_agent_id or self.bolna_agent_ids))

    def public_summary(self) -> dict:
        """Non-secret view of the config for the admin panel."""
        return {
            "default_mode": self.default_mode,
            "public_base_url": self.public_base_url,
            "bolna_api_key_set": bool(self.bolna_api_key),
            "bolna_agent_ids": self.bolna_agent_ids or ({"default": self.bolna_agent_id} if self.bolna_agent_id else {}),
            "bolna_ready": self.bolna_ready,
            "bolna_telephony_provider": self.bolna_telephony_provider,
            "bolna_llm_model": self.bolna_llm_model,
            "bolna_llm_active": self.bolna_llm_active or self.bolna_llm_model,
            "bolna_llm_note": self.bolna_llm_note,
            "max_counter_rounds": self.max_counter_rounds,
            "bolna_from_number_set": bool(self.bolna_from_number),
            "live_test_phone_set": bool(self.live_test_phone),
            "google_maps_enabled": bool(self.google_maps_api_key),
            "call_real_numbers": self.call_real_numbers,
            "test_phone": (self.test_phone_override or self.live_test_phone)[:4] + "••••"
                          + (self.test_phone_override or self.live_test_phone)[-3:]
                          if (self.test_phone_override or self.live_test_phone) else "",
            "dial_strategy": self.dial_strategy,
            "gemini_enabled": bool(self.gemini_api_key and self.llm_enabled),
            "gemini_model": self.gemini_model,
            "gemini_chat_model": self.gemini_chat_models.split(",")[0].strip(),
            "auto_call_count": self.auto_call_count,
            "keep_awake_minutes": self.keep_awake_minutes,
            "sim_llm_rephrase": self.sim_llm_rephrase,
            "razorpay_enabled": bool(self.razorpay_key_id and self.razorpay_key_secret),
            "whatsapp_api_enabled": bool(self.whatsapp_token and self.whatsapp_phone_number_id),
            "tool_auth_enabled": bool(self.tool_shared_secret),
            "wave_stagger_seconds": self.wave_stagger_seconds,
            "sim_speed": self.sim_speed,
        }


    @property
    def llm_ladder(self) -> list[str]:
        """Configured model first, then the fallbacks in order, without duplicates."""
        models = [self.bolna_llm_model, *self.bolna_llm_fallback_models.split(","), self.bolna_llm_fallback_model]
        return list(dict.fromkeys(m.strip() for m in models if m and m.strip()))


settings = Settings()


def reload_settings() -> Settings:
    """Re-read env (used by tests and after the admin panel changes runtime values)."""
    global settings
    settings = Settings()
    return settings
