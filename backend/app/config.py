"""Central configuration.

Everything here is overridable via environment variables and NOTHING requires an
API key: with no key set, `llm_config().enabled` is False and every workflow runs
on its deterministic path.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent

# Local development: read backend/.env if present (gitignored). Real environment variables
# always win; in the container the platform injects them and no .env exists.
try:
    from dotenv import load_dotenv
    load_dotenv(BACKEND_DIR / ".env", override=False)
except ImportError:  # pragma: no cover - dotenv is pinned in requirements.txt
    pass
DATA_DIR = Path(os.getenv("FINN_DATA_DIR", str(BACKEND_DIR / "data")))
DB_PATH = Path(os.getenv("FINN_DB", str(DATA_DIR / "finn.db")))

# Option C ingestion: committed snapshot by default; tickers not in the snapshot
# are fetched live and not re-fetched for this many hours.
LIVE_TTL_HOURS = float(os.getenv("FINN_LIVE_TTL_HOURS", "20"))

# Seconds between requests to NSE. Be polite; NSE rate-limits aggressively.
NSE_DELAY_S = float(os.getenv("FINN_NSE_DELAY_S", "1.5"))


# --------------------------------------------------------------------------- LLM
GEMINI_OPENAI_BASE = "https://generativelanguage.googleapis.com/v1beta/openai/"
_BANNED_MODEL_PREFIXES = ("gemini-2.5",)  # on Google's deprecation track


@dataclass(frozen=True)
class LLMConfig:
    fast_model: str  # bulk JSON extraction: ambiguous filings, appointment bios
    deep_model: str  # promised-vs-delivered synthesis, grounded bull/bear
    base_url: str    # any OpenAI-compatible endpoint
    api_key: str | None
    timeout_s: float = 30.0
    deep_timeout_s: float = 25.0   # hard read timeout; on expiry the call falls back to the fast tier

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)


def llm_config() -> LLMConfig:
    cfg = LLMConfig(
        fast_model=os.getenv("FINN_LLM_FAST", "gemini-3.5-flash-lite"),
        deep_model=os.getenv("FINN_LLM_DEEP", "gemini-3.8-flash"),
        base_url=os.getenv("FINN_LLM_BASE_URL", GEMINI_OPENAI_BASE),
        api_key=os.getenv("FINN_LLM_API_KEY") or os.getenv("GEMINI_API_KEY") or None,
        deep_timeout_s=float(os.getenv("FINN_LLM_DEEP_TIMEOUT_S", "25")),
    )
    for model in (cfg.fast_model, cfg.deep_model):
        if model.lower().startswith(_BANNED_MODEL_PREFIXES):
            raise ValueError(f"Model '{model}' is on the deprecation track; pick another.")
    return cfg


# --------------------------------------------------------------------- Portfolio
@dataclass(frozen=True)
class Holding:
    symbol: str      # NSE symbol
    name: str
    sector: str
    weight_pct: float


# Mock demo portfolio: mixed sectors and sizes (large caps through small/mid caps).
PORTFOLIO: tuple[Holding, ...] = (
    Holding("RELIANCE", "Reliance Industries", "Conglomerate", 10),
    Holding("HDFCBANK", "HDFC Bank", "Banking", 10),
    Holding("INFY", "Infosys", "IT Services", 8),
    Holding("BHARTIARTL", "Bharti Airtel", "Telecom", 7),
    Holding("LT", "Larsen & Toubro", "Capital Goods", 7),
    Holding("TATAPOWER", "Tata Power", "Power", 7),
    Holding("SUNPHARMA", "Sun Pharmaceutical", "Pharma", 6),
    Holding("TITAN", "Titan Company", "Consumer Discretionary", 6),
    Holding("CHOLAFIN", "Cholamandalam Investment & Finance", "NBFC / Rural Lending", 6),
    Holding("POLYCAB", "Polycab India", "Cables & Wires", 6),
    Holding("DIXON", "Dixon Technologies", "Electronics Manufacturing", 6),
    Holding("KAYNES", "Kaynes Technology", "Electronics Manufacturing", 6),
    Holding("ASIANPAINT", "Asian Paints", "Consumer Staples", 5),
    Holding("PIIND", "PI Industries", "Agrochemicals", 5),
    Holding("DEEPAKNTR", "Deepak Nitrite", "Specialty Chemicals", 5),
)

PORTFOLIO_SYMBOLS: tuple[str, ...] = tuple(h.symbol for h in PORTFOLIO)


def weight_of(symbol: str) -> float:
    for h in PORTFOLIO:
        if h.symbol == symbol:
            return h.weight_pct
    return 0.0
