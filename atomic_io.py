"""Atomic file publication with bounded retries for transient Windows locks."""
import os
import time


def replace_file(source, destination, attempts=8, base_delay=0.05):
    """Replace destination atomically; retry transient permission/share violations."""
    if attempts < 1:
        raise ValueError("attempts must be positive")
    for attempt in range(attempts):
        try:
            os.replace(source, destination)
            return
        except PermissionError:
            if attempt + 1 == attempts:
                raise
            time.sleep(base_delay * (2 ** attempt))
