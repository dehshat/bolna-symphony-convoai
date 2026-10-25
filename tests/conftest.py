import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Deterministic, offline, fast test environment (set before the app reads its config)
os.environ.update({
    "DB_PATH": ":memory:", "LLM_ENABLED": "false", "SIM_SPEED": "50", "WAVE_STAGGER_SECONDS": "8",
    "HYBRID_LIVE_DELAY_SECONDS": "0", "TOOL_SHARED_SECRET": "", "ADMIN_TOKEN": "", "BOLNA_API_KEY": "",
    "BOLNA_AGENT_ID": "", "BOLNA_AGENT_IDS": "", "RAZORPAY_KEY_ID": "", "RAZORPAY_KEY_SECRET": "",
    "WHATSAPP_TOKEN": "", "GEMINI_API_KEY": "", "PUBLIC_BASE_URL": "https://swarm.example.com",
})

from app import config  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_state(monkeypatch):
    config.reload_settings()
    from app import state
    state.reset(":memory:")
    yield state
