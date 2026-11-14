"""Central configuration. Everything is overridable by environment variables so the
same code runs on a laptop, in a container, and in a GitHub Actions refresh job."""
import os
from pathlib import Path
from dotenv import load_dotenv
load_dotenv()
BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.getenv("FINN_DATA_DIR", BASE_DIR / "data"))
DB_PATH = Path(os.getenv("FINN_DB", DATA_DIR / "finn.db"))
SNAPSHOT_DIR = DATA_DIR / "snapshot"
FIXTURE_DIR = DATA_DIR / "fixtures"
FRONTEND_DIR = Path(os.getenv("FINN_FRONTEND", BASE_DIR.parent / "frontend"))

# ---- LLM routing -----------------------------------------------------------
# Two tiers. "fast" does bulk, low-stakes work (classifying an ambiguous filing,
# one-line summaries, pulling guidance statements out of transcript chunks).
# "deep" is only used where reasoning matters (promised-vs-delivered verdicts,
# bull/bear). Both default to Gemini because its free tier covers this prototype.
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
LLM_FAST_MODEL = os.getenv("FINN_LLM_FAST", "gemini-3.5-flash-lite")
LLM_DEEP_MODEL = os.getenv("FINN_LLM_DEEP", "gemini-3.8-flash")
# Optional OpenAI-compatible endpoint (Groq, OpenRouter, a local Ollama...).
OPENAI_BASE_URL = os.getenv("FINN_OPENAI_BASE_URL", "")
OPENAI_API_KEY = os.getenv("FINN_OPENAI_API_KEY", "")

LLM_ENABLED = bool(GEMINI_API_KEY or OPENAI_BASE_URL)

# Demo mode layers clearly-labelled synthetic scenarios on top of live data so the
# three showcase workflows (management change, results, material event) always fire.
DEMO_MODE = os.getenv("FINN_DEMO", "1") == "1"

# Simple shared-secret gate for the deployed prototype (the network is personal data).
ACCESS_TOKEN = os.getenv("FINN_ACCESS_TOKEN", "")

HTTP_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
