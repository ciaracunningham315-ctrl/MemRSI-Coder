import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from retry_policy import retry_delay
assert retry_delay({"retry_after": "17"}) == 17
assert retry_delay({"Retry_After": "19"}) == 19
assert retry_delay({"Retry-After": "bad"}) is None
assert retry_delay({}) is None
