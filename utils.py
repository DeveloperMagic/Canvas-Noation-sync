import time
from functools import wraps

def retry(exceptions=(Exception,), tries=3, delay=1.0, backoff=2.0):
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            _tries, _delay = tries, delay
            while _tries > 1:
                try:
                    return fn(*args, **kwargs)
                except exceptions:
                    time.sleep(_delay)
                    _tries -= 1
                    _delay *= backoff
            return fn(*args, **kwargs)
        return wrapper
    return decorator
