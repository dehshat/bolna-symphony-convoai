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
    bolna_llm_model: str = field(default_factory=lambda: os.getenv("BOLNA_LLM_MODEL", "gpt-4.1-mini"))
    bolna_agent_languages: str = field(default_factory=lambda: os.getenv("BOLNA_AGENT_LANGUAGES", "hi,kn"))
    # Sarvam models/voice used by the Bolna agent (bulbul:v2 and saarika were retired by Sarvam)
    sarvam_tts_model: str = field(default_factory=lambda: os.getenv("SARVAM_TTS_MODEL", "bulbul:v3"))
    sarvam_tts_voice: str = field(default_factory=lambda: os.getenv("SARVAM_TTS_VOICE", "shubh"))
    sarvam_asr_model: str = field(default_factory=lambda: os.getenv("SARVAM_ASR_MODEL", "saaras:v4"))
    # "var" = {"$var": "x"} type-safe markers, "legacy" = "%(x)s" string templates
    bolna_tool_param_style: str = field(default_factory=lambda: os.getenv("BOLNA_TOOL_PARAM_STYLE", "var"))
    tool_shared_secret: str = field(default_factory=lambda: os.getenv("TOOL_SHARED_SECRET", ""))
    admin_token: str = field(default_factory=lambda: os.getenv("ADMIN_TOKEN", ""))

    # Phone numbers for hybrid/live mode (E.164, e.g. +919876543210)
    live_test_phone: str = field(default_factory=lambda: os.getenv("LIVE_TEST_PHONE", ""))
    live_vendor_phones: str = field(default_factory=lambda: os.getenv("LIVE_VENDOR_PHONES", ""))

    # --- AI text model for simulated vendors + brief parsing (Google AI Studio / Gemini) ---
    gemini_api_key: str = field(default_factory=lambda: os.getenv("GEMINI_API_KEY", "") or os.getenv("GOOGLE_API_KEY", ""))
    gemini_model: str = field(default_factory=lambda: os.getenv("GEMINI_MODEL", "gemini-2.5-flash"))
    llm_enabled: bool = field(default_factory=lambda: _bool("LLM_ENABLED", True))
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
            "bolna_from_number_set": bool(self.bolna_from_number),
            "live_test_phone_set": bool(self.live_test_phone),
            "gemini_enabled": bool(self.gemini_api_key and self.llm_enabled),
            "gemini_model": self.gemini_model,
            "sim_llm_rephrase": self.sim_llm_rephrase,
            "razorpay_enabled": bool(self.razorpay_key_id and self.razorpay_key_secret),
            "whatsapp_api_enabled": bool(self.whatsapp_token and self.whatsapp_phone_number_id),
            "tool_auth_enabled": bool(self.tool_shared_secret),
            "wave_stagger_seconds": self.wave_stagger_seconds,
            "sim_speed": self.sim_speed,
        }


settings = Settings()


def reload_settings() -> Settings:
    """Re-read env (used by tests and after the admin panel changes runtime values)."""
    global settings
    settings = Settings()
    return settings
