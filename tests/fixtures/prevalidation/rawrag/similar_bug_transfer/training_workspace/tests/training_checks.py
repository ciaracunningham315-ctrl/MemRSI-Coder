import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from header_utils import get_header
assert get_header({"x_request_id": "r-19"}, "X-Request-Id") == "r-19"
assert get_header({"X_REQUEST_ID": "r-20"}, "x-request-id") == "r-20"
