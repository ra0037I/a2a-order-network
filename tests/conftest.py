import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ["DOTENV_OVERRIDE"] = "0"         # keep the test settings below even if a .env exists
os.environ["LLM_PROVIDER"] = "mock"          # unit tests never call a real LLM
os.environ.setdefault("REGISTRY_API_KEY", "test-key")
