"""Watch OVH stock for a server that fits LangLab, in Europe: cheap configurations come and go for
days. Run every 10 minutes (launchd, ops/uk.langlab.ovh-watch.plist); a configuration that turns up is
announced once — a macOS notification and a letter through Resend — and again only after it has gone.

  python3 ops/ovh_watch.py --to me@example.com [--dry]

Only the standard library: it runs on the system python3, outside the project's environment.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import urllib.request
from datetime import datetime
from pathlib import Path

API = "https://ca.api.ovh.com/1.0"  # the account's zone (OVHcloud Worldwide); stock is shared across zones
EU = {"gra", "rbx", "sbg", "fra", "lim", "waw", "lon", "eri", "par"}
STATE = Path.home() / ".cache" / "lp" / "ovh_watch.json"
LOG = STATE.with_suffix(".log")
KEY = Path.home() / ".config" / "lp" / "resend.key"

# (label, plan, what fits). Dedicated: memory/storage prefixes that keep the price in budget.
DEDICATED = [
    ("SYS-3 (Xeon-E 2288G, 8 ядер) — $46.50", "24sys03-v1", ("ram-32g", "ram-64g"), ("softraid-2x960nvme",)),
    ("KS-6 (EPYC 7351P, 16 ядер) — $43.20", "24sk602", None, None),
    ("KS-5-A (Xeon E-2274G, 4 ядра до 4.9 ГГц) — $31.90", "26sk50a-v1", None, None),
]
VPS = [("VPS-4 (8 vCores, 24 ГБ) — $27.50", "vps-2027-model4")]


def get(path: str):
    with urllib.request.urlopen(f"{API}{path}", timeout=30) as r:
        return json.load(r)


def in_stock() -> dict[str, str]:
    """key → human description of every fitting configuration available in Europe right now."""
    found = {}
    for label, plan, mem, disk in DEDICATED:
        for row in get(f"/dedicated/server/datacenter/availabilities?planCode={plan}"):
            if mem and not row["memory"].startswith(mem) or disk and not row["storage"].startswith(disk):
                continue
            for dc in row["datacenters"]:
                if dc["datacenter"] in EU and dc["availability"] not in ("unavailable", "unknown"):
                    key = f"{plan}|{row['memory']}|{row['storage']}|{dc['datacenter']}"
                    found[key] = f"{label}: {row['memory']}, {row['storage']}, {dc['datacenter']} ({dc['availability']})"
    for label, plan in VPS:
        for dc in get(f"/vps/order/rule/datacenter?ovhSubsidiary=WS&planCode={plan}")["datacenters"]:
            if dc["code"].startswith("eu-") and dc["linuxStatus"] == "available":
                found[f"{plan}|{dc['code']}"] = f"{label}: {dc['code']}"
    return found


def notify(to: str, lines: list[str]) -> None:
    text = "Появилось у OVH в Европе:\n\n" + "\n".join(f"• {x}" for x in lines) + \
           "\n\nЗаказ: https://eco.ovhcloud.com/en/ (выделенные), https://www.ovhcloud.com/en/vps/ (VPS)"
    subprocess.run(["osascript", "-e", f'display notification "{lines[0][:120]}" with title "OVH: есть сервер"'],
                   check=False)
    if KEY.is_file():
        body = json.dumps({"from": "LangLab <login@mail.langlab.uk>", "to": [to],
                           "subject": f"OVH: появился сервер — {lines[0][:60]}", "text": text}).encode()
        req = urllib.request.Request("https://api.resend.com/emails", data=body, method="POST", headers={
            "Authorization": f"Bearer {KEY.read_text().strip()}", "Content-Type": "application/json",
            "User-Agent": "langlab-ovh-watch/1"})  # Python's default agent can be turned away at the edge
        urllib.request.urlopen(req, timeout=30).read()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--to", required=True, help="where the letter goes")
    ap.add_argument("--dry", action="store_true", help="print what is in stock, notify nobody, keep no state")
    a = ap.parse_args()
    now = in_stock()
    if a.dry:
        print("\n".join(now.values()) or "nothing that fits in stock")
        return
    seen = set(json.loads(STATE.read_text())) if STATE.exists() else set()
    new = [now[k] for k in sorted(set(now) - seen)]
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(sorted(now)))  # gone configurations drop out, so a comeback is announced again
    with LOG.open("a") as f:
        f.write(f"{datetime.now():%Y-%m-%d %H:%M} in stock {len(now)}, new {len(new)}\n")
    if new:
        notify(a.to, new)


if __name__ == "__main__":
    main()
