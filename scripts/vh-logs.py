"""Format volley-hub log lines (from `vh logs`) read on stdin: one line per api event."""
import json
import sys

user, event = sys.argv[1].lower(), sys.argv[2]
for line in sys.stdin:
    i = line.find("{")
    if i < 0:
        continue
    try:
        e = json.loads(line[i:])
    except ValueError:
        continue
    if user and user not in str(e.get("user", "")).lower():
        continue
    if event and e.get("event") != event:
        continue
    if not event and e.get("event") == "http" and str(e.get("path", "")).startswith("/mcp") and e.get("status") != 401:
        continue  # the tool lines already say what happened
    rest = " ".join(f"{k}={v if isinstance(v, (str, int, float)) else json.dumps(v, ensure_ascii=False)}"
                    for k, v in e.items() if k not in ("ts", "event", "user", "role"))
    print(f"{e.get('ts', '')[:19].replace('T', ' ')}  {e.get('event', ''):<6} {e.get('user') or '-':<18} {rest}")
