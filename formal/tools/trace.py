#!/usr/bin/env python3
"""Print a TLC counterexample trace one state per line. Usage: trace.py tlc.log [N_last]"""

import re
import sys
from pathlib import Path

COLS = ["grid", "sw", "dpu", "e", "g2", "g2by", "srv", "srvT", "net", "srvObT", "ha",
        "ctl", "acked", "obT", "darkT", "waitT", "holdT", "deadT", "crashed",
        "timedOut", "spurious"]  # fmt: skip

log = Path(sys.argv[1]).read_text()
last = int(sys.argv[2]) if len(sys.argv) > 2 else 10**9
m = re.search(
    r"Error: The behavior up to this point is:\n(.*?)(?:\n\n\d+ states generated|\Z)",
    log,
    re.DOTALL,
)
if not m:
    sys.exit("no trace in log")
states = re.split(r"\nState \d+: ", "\n" + m.group(1))[1:]
print("st  " + " ".join(f"{c[:6]:>6}" for c in COLS))
for i, st in enumerate(states, 1):
    if i < len(states) - last + 1:
        continue
    kv = dict(re.findall(r"/\\ (\w+) = ([^\n]+)", st))
    kv = {
        k: re.sub(r'\(?(\w) :> "?(\w+)"? @@ (\w) :> "?(\w+)"?\)?', r"\2/\4", v)
        for k, v in kv.items()
    }
    print(f"{i:<3} " + " ".join(f"{kv.get(c, '?').strip('"')[:6]:>6}" for c in COLS))
