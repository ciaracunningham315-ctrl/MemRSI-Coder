import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from session_cache import session_cache_key
assert session_cache_key("s-4") == "identity:session:s-4"
