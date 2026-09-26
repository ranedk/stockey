#!/usr/bin/env python3
"""Read `SRC=... DPT=...` firewall lines on stdin and report which ports
Cloudflare was blocked on.

Split out of deploy.sh because deciding whether an address falls inside one of
Cloudflare's two dozen published prefixes is a subnet test, and subnet tests in
shell are how you get an answer that is wrong for /15s.

The ranges come from the local nginx real_ip config that setup_nginx_proxy.sh
already wrote, NOT from the network. Two reasons: a diagnostic that needs the
internet to work is useless exactly when the network is the problem, and that
file holds the ranges this host actually allowed — which is the set the question
is really about.
"""
import ipaddress
import re
import subprocess
import sys
from collections import Counter

REALIP = "/etc/nginx/conf.d/cloudflare-realip.conf"


def cloudflare_networks():
    nets = []
    try:
        with open(REALIP) as f:
            for line in f:
                m = re.match(r"\s*set_real_ip_from\s+(\S+);", line)
                if m:
                    nets.append(ipaddress.ip_network(m.group(1)))
    except OSError:
        pass
    if nets:
        return nets
    # Fall back to the published lists. curl rather than urllib: Cloudflare
    # rejects urllib's default user-agent, which silently yields zero ranges and
    # a diagnostic that cheerfully reports nothing is wrong.
    for url in ("https://www.cloudflare.com/ips-v4", "https://www.cloudflare.com/ips-v6"):
        try:
            out = subprocess.run(["curl", "-fsS", "--max-time", "15", url],
                                 capture_output=True, text=True, check=True).stdout
            nets += [ipaddress.ip_network(l.strip()) for l in out.splitlines() if l.strip()]
        except Exception:
            pass
    return nets


def main():
    nets = cloudflare_networks()
    if not nets:
        print("(could not determine Cloudflare's ranges — skipping this check)", file=sys.stderr)
        return
    hits = Counter()
    for line in sys.stdin:
        m = re.search(r"SRC=([0-9a-fA-F.:]+).*?DPT=(\d+)", line)
        if not m:
            continue
        try:
            ip = ipaddress.ip_address(m.group(1))
        except ValueError:
            continue
        if any(ip.version == n.version and ip in n for n in nets):
            hits[m.group(2)] += 1
    for port, n in sorted(hits.items(), key=lambda kv: -kv[1]):
        print(f"{n} blocked connection(s) from Cloudflare to port {port}")


if __name__ == "__main__":
    main()
