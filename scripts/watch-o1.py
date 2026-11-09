"""Sample existing application counters; keep failed scrapes visible after OOM."""
import json
import sys
import time
import urllib.error
import urllib.request

with open(sys.argv[1], "x", buffering=1) as out:
    while True:
        sample = {"unix_time": time.time()}
        try:
            with urllib.request.urlopen("http://127.0.0.1:8081/metrics", timeout=0.5) as response:
                sample["metrics"] = response.read().decode()
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            sample["error"] = str(exc)
        out.write(json.dumps(sample) + "\n")
        time.sleep(0.25)
