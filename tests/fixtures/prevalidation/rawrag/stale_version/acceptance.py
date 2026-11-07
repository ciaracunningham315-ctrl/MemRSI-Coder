import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from retry_policy import should_retry
assert should_retry(429) is True
assert should_retry(503) is True
assert should_retry(404) is False
assert should_retry(600) is False
