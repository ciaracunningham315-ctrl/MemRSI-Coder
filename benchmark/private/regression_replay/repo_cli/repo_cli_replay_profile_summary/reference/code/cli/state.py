import json
import os
from pathlib import Path


def read(path):
    path = Path(path)
    if not path.exists():
        return {"next_id": 1, "records": []}
    value = json.loads(path.read_text(encoding="utf-8"))
    if type(value) is not dict or type(value.get("next_id")) is not int or type(value.get("records")) is not list:
        raise ValueError("invalid state file")
    return value


def append(path, command, options):
    path = Path(path)
    state = read(path)
    record = {"id": state["next_id"], "command": command, **options}
    state["records"].append(record)
    state["next_id"] += 1
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)
    return record
