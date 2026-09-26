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
