#!/usr/bin/env python3
"""
Automated fault scenarios. Start the POX controller first (in another terminal),
then run ONE scenario per controller mode and compare the results:

    sudo python3 scenarios.py failure    --label adaptive
    sudo python3 scenarios.py overload   --label adaptive
    sudo python3 scenarios.py congestion --label adaptive

Repeat with the controller started as --mode=roundrobin (label roundrobin) and
--mode=leastconn (label leastconn). Output is printed and saved in results/.

failure     h4 crashes at --fault-at seconds and comes back --fault-for seconds later
            (--how process kills the server, --how link takes its cable down)
overload    h5 becomes 10x slower for --fault-for seconds
congestion  background traffic (iperf) saturates h4's link for --fault-for seconds;
            needs links limited with bandwidth, done here with --bw (default 10 Mbps)
            and the controller started with --link_mbps=10
"""
import argparse
import os
import threading
import time

from mininet.log import setLogLevel
import topology as T

CLIENT = os.path.join(T.HERE, "client.py")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("scenario", choices=["failure", "overload", "congestion"])
    p.add_argument("--label", default="run", help="controller mode, used in the file name")
    p.add_argument("--duration", type=int, default=24)
    p.add_argument("--fault-at", type=int, default=8)
    p.add_argument("--fault-for", type=int, default=8)
    p.add_argument("--how", choices=["process", "link"], default="process")
    p.add_argument("--proto", choices=["tcp", "udp"], default="tcp")
    p.add_argument("--workers", type=int, default=10)
    p.add_argument("--bw", type=float, default=10.0)
    a = p.parse_args()
    if os.geteuid() != 0:
        raise SystemExit("run with sudo")

    setLogLevel("warning")
    net = T.build_network(bw=a.bw if a.scenario == "congestion" else None)
    T.start_servers(net)
    h1, h3, h4, h5 = (net.get(n) for n in ("h1", "h3", "h4", "h5"))
    if a.scenario == "congestion":
        h4.cmd("iperf -s -u > /tmp/iperf_server.log 2>&1 &")
    time.sleep(2)                      # let the first load reports arrive

    t0 = []

    def stamp(msg):
        print("[t=%4.1fs] %s" % (time.time() - t0[0], msg), flush=True)

    def inject():
        if a.scenario == "failure":
            if a.how == "link":
                net.configLinkStatus("s1", "h4", "down")
            else:
                h4.cmd("pkill -f 'server.py --name h4'")
            stamp("FAULT: h4 is down (%s)" % a.how)
        elif a.scenario == "overload":
            h5.cmd("pkill -USR1 -f 'server.py --name h5'")
            stamp("FAULT: h5 is now 10x slower")
        else:
            h3.cmd("iperf -c 10.0.0.4 -u -b 9M -t %d > /tmp/iperf_client.log 2>&1 &" % a.fault_for)
            stamp("FAULT: 9 Mbps of background traffic on h4's link")

    def restore():
        if a.scenario == "failure":
            if a.how == "link":
                net.configLinkStatus("s1", "h4", "up")
            else:
                T.start_server(net, "h4")
            stamp("RESTORED: h4 is back")
        elif a.scenario == "overload":
            h5.cmd("pkill -USR1 -f 'server.py --name h5'")
            stamp("RESTORED: h5 is back to normal speed")
        else:
            stamp("background traffic ended")

    threading.Timer(a.fault_at, inject).start()
    threading.Timer(a.fault_at + a.fault_for, restore).start()

    cmd = ("python3 %s --duration %d -c %d --retries 2 --timeout 2 --proto %s"
           % (CLIENT, a.duration, a.workers, a.proto))
    print("\n*** scenario=%s controller=%s: %s" % (a.scenario, a.label, cmd))
    t0.append(time.time())
    out = h1.cmd(cmd)
    print(out)

    os.makedirs(os.path.join(T.HERE, "results"), exist_ok=True)
    path = os.path.join(T.HERE, "results", "%s_%s.txt" % (a.scenario, a.label))
    with open(path, "w") as f:
        f.write("scenario=%s controller=%s fault_at=%ss fault_for=%ss how=%s proto=%s\n\n"
                % (a.scenario, a.label, a.fault_at, a.fault_for, a.how, a.proto))
        f.write(out)
    print("*** saved %s" % path)

    T.stop_servers(net)
    h4.cmd("pkill iperf")
    net.stop()


if __name__ == "__main__":
    main()
