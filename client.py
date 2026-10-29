#!/usr/bin/env python3
"""
Load generator. Run from a client host, e.g.:
    h1 python3 client.py -n 300 -c 10
    h1 python3 client.py --proto udp -n 300 -c 10
"""
import argparse
import socket
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

VIP = "10.0.0.100"
TCP_PORT = 5000
UDP_PORT = 5001


def one_request(proto, i, timeout):
    start = time.time()
    try:
        if proto == "tcp":
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(timeout)
            s.connect((VIP, TCP_PORT))
            s.sendall(("REQ %d" % i).encode())
            reply = s.recv(1024).decode()
            s.close()
        else:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.settimeout(timeout)
            s.sendto(("REQ %d" % i).encode(), (VIP, UDP_PORT))
            reply, _ = s.recvfrom(1024)
            reply = reply.decode()
            s.close()
        if not reply:
            return None, 0
        return reply, (time.time() - start) * 1000.0
    except Exception:
        return None, 0


def main():
    p = argparse.ArgumentParser()
    p.add_argument("-n", type=int, default=100, help="total requests")
    p.add_argument("-c", type=int, default=5, help="parallel workers")
    p.add_argument("--proto", choices=["tcp", "udp"], default="tcp")
    p.add_argument("--timeout", type=float, default=10.0)
    a = p.parse_args()

    start = time.time()
    with ThreadPoolExecutor(max_workers=a.c) as ex:
        results = list(ex.map(lambda i: one_request(a.proto, i, a.timeout),
                              range(a.n)))
    total = time.time() - start

    counts = Counter(r[0] for r in results if r[0])
    lats = [r[1] for r in results if r[0]]
    failed = a.n - len(lats)

    print("\n=== %s, %d requests, %d workers ===" % (a.proto.upper(), a.n, a.c))
    for name in sorted(counts):
        print("  %s answered %4d  (%.1f%%)" % (name, counts[name],
                                              100.0 * counts[name] / max(len(lats), 1)))
    if failed:
        print("  FAILED requests: %d" % failed)
    if lats:
        print("Average latency : %.1f ms" % (sum(lats) / len(lats)))
    print("Total time      : %.2f s" % total)
    print("Throughput      : %.1f req/s\n" % (len(lats) / total if total else 0))


if __name__ == "__main__":
    main()
