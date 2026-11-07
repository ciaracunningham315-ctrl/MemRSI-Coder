def should_retry(status):
    return status == 429 or 500 <= status <= 599
