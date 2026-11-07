import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from retry_policy_v2 import should_retry_v2
assert should_retry_v2(500) is True
assert should_retry_v2(599) is True
assert should_retry_v2(404) is False
assert should_retry_v2(429) is False
assert should_retry_v2(600) is False
