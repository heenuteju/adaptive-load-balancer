#!/usr/bin/env python3
"""
Load generator. Run from a client host, e.g.:
    h1 python3 client.py -n 300 -c 10                    fixed number of requests
    h1 python3 client.py --proto udp -n 300 -c 10
    h1 python3 client.py --duration 24 -c 10 --retries 2 --timeout 2
                                                         keep sending for 24 s and
                                                         print a per-second timeline

--retries K   a failed request is retried up to K times on a new connection, so
              the balancer can send the retry to a server that is still alive.
--timeout T   seconds to wait for a reply before a try counts as failed.
"""
import argparse
import itertools
import socket
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

VIP = "10.0.0.100"
TCP_PORT = 5000
UDP_PORT = 5001


def attempt(vip, proto, i, timeout):
    """One try. Returns the reply text, or None if it failed."""
    try:
        if proto == "tcp":
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(timeout)
            s.connect((vip, TCP_PORT))
            s.sendall(("REQ %d" % i).encode())
            reply = s.recv(1024).decode()
            s.close()
        else:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.settimeout(timeout)
            s.sendto(("REQ %d" % i).encode(), (vip, UDP_PORT))
            reply, _ = s.recvfrom(1024)
            reply = reply.decode()
            s.close()
        return reply or None
    except Exception:
        return None


def one_request(vip, proto, i, timeout, retries):
    """Returns (reply or None, latency in ms including retries, retries used)."""
    start = time.time()
    used = 0
    while True:
        reply = attempt(vip, proto, i, timeout)
        if reply:
            return reply, (time.time() - start) * 1000.0, used
        if used >= retries:
            return None, 0, used
        used += 1
        time.sleep(0.05 * used)       # brief pause so retries do not flood the network


def main():
    p = argparse.ArgumentParser()
    p.add_argument("-n", type=int, default=100, help="total requests")
    p.add_argument("-c", type=int, default=5, help="parallel workers")
    p.add_argument("--proto", choices=["tcp", "udp"], default="tcp")
    p.add_argument("--timeout", type=float, default=10.0)
    p.add_argument("--retries", type=int, default=0)
    p.add_argument("--duration", type=float, default=0,
                   help="run for this many seconds instead of sending -n requests")
    p.add_argument("--timeline", action="store_true",
                   help="print per-second results (always on with --duration)")
    p.add_argument("--vip", default=VIP)
    a = p.parse_args()

    start = time.time()
    if a.duration > 0:
        end = start + a.duration
        ids = itertools.count()

        def worker(_):
            out = []
            while time.time() < end:
                t = time.time() - start
                r = one_request(a.vip, a.proto, next(ids), a.timeout, a.retries)
                out.append((t,) + r)
            return out

        with ThreadPoolExecutor(max_workers=a.c) as ex:
            records = [x for part in ex.map(worker, range(a.c)) for x in part]
        label = "%.0f s" % a.duration
    else:
        def job(i):
            t = time.time() - start
            return (t,) + one_request(a.vip, a.proto, i, a.timeout, a.retries)

        with ThreadPoolExecutor(max_workers=a.c) as ex:
            records = list(ex.map(job, range(a.n)))
        label = "%d requests" % a.n
    total = time.time() - start

    counts = Counter(r[1] for r in records if r[1])
    lats = [r[2] for r in records if r[1]]
    failed = len(records) - len(lats)
    retried = sum(r[3] for r in records)

    print("\n=== %s, %s, %d workers ===" % (a.proto.upper(), label, a.c))
    if a.duration > 0:
        print("  requests sent   : %d" % len(records))
    for name in sorted(counts):
        print("  %s answered %4d  (%.1f%%)" % (name, counts[name],
                                              100.0 * counts[name] / max(len(lats), 1)))
    if failed:
        print("  FAILED requests: %d" % failed)
    if retried:
        print("  Retries used   : %d" % retried)
    if lats:
        print("Average latency : %.1f ms" % (sum(lats) / len(lats)))
    print("Total time      : %.2f s" % total)
    print("Throughput      : %.1f req/s\n" % (len(lats) / total if total else 0))

    if a.timeline or a.duration > 0:
        names = sorted(counts)
        print("Timeline (requests answered in each second)")
        print("  sec  " + " ".join("%5s" % n for n in names) + "  failed  avg ms")
        for sec in range(int(total) + 1):
            part = [r for r in records if int(r[0]) == sec]
            if not part:
                continue
            row = Counter(r[1] for r in part if r[1])
            ok = [r[2] for r in part if r[1]]
            print("  %3d  " % sec + " ".join("%5d" % row.get(n, 0) for n in names)
                  + "  %6d  %6.0f" % (len(part) - len(ok),
                                      sum(ok) / len(ok) if ok else 0))
        print()


if __name__ == "__main__":
    main()
