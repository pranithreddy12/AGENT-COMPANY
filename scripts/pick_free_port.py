"""Print the first free TCP port on 127.0.0.1 starting at 8000. Used by startup.bat — a separate
.py file instead of an inline one-liner because embedding real Python (with its own string quoting)
inside a batch FOR/F command substitution is exactly the kind of nested-quote situation cmd.exe's
parser reliably breaks on.
"""
import socket
import sys

start = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
for port in range(start, start + 200):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", port))
            print(port)
            break
        except OSError:
            continue
else:
    print(start)  # fallback: let the caller's own bind error surface if truly nothing is free
