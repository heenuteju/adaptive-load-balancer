#!/usr/bin/env python3
"""
Backend server. TCP on 5000, UDP on 5001, one thread per request.
Every second it sends a UDP load report (active, avg_ms) to VIP:9999.
Started automatically on h4/h5/h6 by topology.py.
"""
import argparse
import json
import socket
import threading
import time

VIP = "10.0.0.100"
REPORT_PORT = 9999
TCP_PORT = 5000
UDP_PORT = 5001

lock = threading.Lock()
active = 0
avg_ms = 0.0


def do_work(delay):
    """Simulate processing; track active requests and moving-average time."""
    global active, avg_ms
    with lock:
        active += 1
    start = time.time()
    time.sleep(delay)
    elapsed = (time.time() - start) * 1000.0
    with lock:
        active -= 1
        avg_ms = 0.7 * avg_ms + 0.3 * elapsed


def handle_tcp(conn, name, delay):
    try:
        conn.settimeout(10)
        conn.recv(1024)
        do_work(delay)
        conn.sendall(name.encode())
    except Exception:
        pass
    finally:
        conn.close()


def tcp_server(name, delay):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("0.0.0.0", TCP_PORT))
    s.listen(256)
    while True:
        conn, _ = s.accept()
        threading.Thread(target=handle_tcp, args=(conn, name, delay),
                         daemon=True).start()


def handle_udp(sock, addr, name, delay):
    do_work(delay)
    try:
        sock.sendto(name.encode(), addr)
    except Exception:
        pass


def udp_server(name, delay):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("0.0.0.0", UDP_PORT))
    while True:
        _, addr = s.recvfrom(1024)
        threading.Thread(target=handle_udp, args=(s, addr, name, delay),
                         daemon=True).start()


def reporter(name):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    while True:
        with lock:
            msg = json.dumps({"name": name, "active": active,
                              "avg_ms": round(avg_ms, 2)})
        try:
            s.sendto(msg.encode(), (VIP, REPORT_PORT))
        except Exception:
            pass
        time.sleep(1)


def main():
    global avg_ms
    p = argparse.ArgumentParser()
    p.add_argument("--name", required=True)
    p.add_argument("--delay", type=float, required=True)
    a = p.parse_args()
    avg_ms = a.delay * 1000.0      # starting estimate of processing time

    for fn, args in ((tcp_server, (a.name, a.delay)),
                     (udp_server, (a.name, a.delay)),
                     (reporter, (a.name,))):
        threading.Thread(target=fn, args=args, daemon=True).start()
    print("server %s up, delay=%.2fs" % (a.name, a.delay), flush=True)
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    main()
