import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from evidence import visible_evidence
events = [{"repository_id":"foreign","text":"foreign evidence"}, {"repository_id":"other","text":"shared evidence"}]
assert visible_evidence(events, "foreign") == events
