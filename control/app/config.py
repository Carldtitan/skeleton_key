import os
import pathlib

DATA_DIR = pathlib.Path(os.environ.get("SK_DATA_DIR", "/data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "skeleton_key.db"

WORKER_TOKEN = os.environ["SK_WORKER_TOKEN"]
# Comma-separated VPC addresses of worker daemons, e.g. "10.10.0.4:7000,10.10.0.5:7000"
WORKERS = [w.strip() for w in os.environ.get("SK_WORKERS", "10.10.0.4:7000").split(",") if w.strip()]

INFERENCE_URL = os.environ.get("VULTR_INFERENCE_URL", "https://api.vultrinference.com/v1")
INFERENCE_KEY = os.environ["VULTR_INFERENCE_KEY"]
BROWSE_MODEL = os.environ.get("SK_BROWSE_MODEL", "qwen3.8-27b")
CODE_MODEL = os.environ.get("SK_CODE_MODEL", "glm-5.3")

EXPLORE_MAX_STEPS = int(os.environ.get("SK_EXPLORE_MAX_STEPS", "60"))

# Public base URL (Caddy terminates TLS for this domain).
PUBLIC_URL = os.environ.get("SK_PUBLIC_URL") or f"https://{os.environ.get('SK_DOMAIN', 'localhost')}"

# Optional frontier baseline for the race (comparison only; the product itself uses Vultr inference).
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY") or None
FRONTIER_MODEL = os.environ.get("SK_FRONTIER_MODEL", "claude-opus-5")
TOOL_AGENT_MODEL = os.environ.get("SK_TOOL_AGENT_MODEL", BROWSE_MODEL)
JUDGE_MODEL = os.environ.get("SK_JUDGE_MODEL", "glm-5.3-flash")

# USD per token for the frontier baseline (Anthropic list prices).
ANTHROPIC_PRICES = {
    "claude-opus-5": (5 / 1e6, 25 / 1e6),
    "claude-opus-5-5": (4 / 1e6, 20 / 1e6),
    "claude-sonnet-5": (2 / 1e6, 10 / 1e6),
    "claude-haiku-4-5": (1 / 1e6, 5 / 1e6),
}
HEALTH_CHECK_SECONDS = int(os.environ.get("SK_HEALTH_CHECK_SECONDS", "900"))

# The live browser view is served straight from this server (Vercel cannot proxy WebSockets).
LIVE_BASE = os.environ.get("SK_LIVE_BASE") or f"https://{os.environ.get('SK_DOMAIN', 'localhost')}"
