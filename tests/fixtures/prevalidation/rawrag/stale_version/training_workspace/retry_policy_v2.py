"""Archived v2 retry exercise task description:
In retry policy v2, retry only HTTP 5xx server error status codes (500 through 599 inclusive).
Do not retry 429 or any status code >= 600.
"""


def should_retry_v2(status):
    return status >= 500
