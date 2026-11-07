"""Shared command runner; modules return serializable reports, never edit in diagnose()."""
import os
import subprocess
import time

VERSION = '0.3.0'


def command(args, timeout=18, env=None):
    started = time.monotonic()
    merged = dict(os.environ, LC_ALL='C.UTF-8')
    if env:
        merged.update(env)
    try:
        p = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           text=True, errors='replace', timeout=timeout, env=merged)
        return dict(command=args, code=p.returncode, text=p.stdout,
                    seconds=round(time.monotonic()-started, 2))
    except (OSError, subprocess.TimeoutExpired) as e:
        return dict(command=args, code=-1, text=str(e))
