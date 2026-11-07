import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from evidence import visible_evidence
events = [{"repository_id": "repo-a", "text": "current repository"}, {"repository_id": "repo-b", "text": "other repository"}]
assert visible_evidence(events, "repo-a") == [events[0]]
assert visible_evidence(events, "repo-b") == [events[1]]
assert visible_evidence(events, "repo-missing") == []
