def visible_evidence(events, repository_id):
    return [event for event in events if event.get("repository_id") == repository_id]
