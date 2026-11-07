import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cache_keys import profile_cache_key
assert profile_cache_key("u-7") == "identity:profile:u-7"
assert profile_cache_key(42) == "identity:profile:42"
