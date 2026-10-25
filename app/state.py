"""Process-wide singletons. Always access them as `state.engine` (not `from ... import engine`) so tests can reset."""

from __future__ import annotations

from app import config
from app.auction_engine import AuctionEngine, Store
from app.orchestrator import Orchestrator
from app.ws import broadcaster

engine: AuctionEngine
orchestrator: Orchestrator


def reset(db_path: str | None = None) -> None:
    global engine, orchestrator
    engine = AuctionEngine(store=Store(db_path or config.settings.db_path), publisher=broadcaster.publish)
    orchestrator = Orchestrator(engine)


reset()
