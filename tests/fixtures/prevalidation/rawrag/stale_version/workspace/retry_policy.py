def should_retry(status):
    return status >= 500

def should_retry_v2(status):
    return status >= 500
