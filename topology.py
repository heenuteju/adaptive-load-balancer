#!/usr/bin/env python3
"""
Mininet topology for the Adaptive Load Balancer (POX version).

h1,h2,h3 = clients      h4,h5,h6 = servers      VIP = 10.0.0.100
Host n is connected to switch port n (the controller relies on this).

Run with:
    sudo python3 topology.py              normal links
    sudo python3 topology.py --bw 10      every link limited to 10 Mbps
                                          (needed for the congestion test)
"""
import argparse
import os
import time
from functools import partial

from mininet.net import Mininet
from mininet.node import RemoteController, OVSKernelSwitch
from mininet.link import TCLink
from mininet.cli import CLI
from mininet.log import setLogLevel

VIP = "10.0.0.100"
VMAC = "00:00:00:00:00:64"
HERE = os.path.dirname(os.path.abspath(__file__))

# server name -> simulated work per request (seconds)
SERVER_DELAYS = {"h4": 0.02, "h5": 0.02, "h6": 0.30}


def build_network(bw=None):
    """Create and start the network. bw (Mbps) limits every link when given."""
    kw = {"link": TCLink} if bw else {}
    net = Mininet(controller=None,
                  switch=partial(OVSKernelSwitch, protocols="OpenFlow10"),
                  autoSetMacs=False, **kw)

    net.addController("c0", controller=RemoteController,
                      ip="127.0.0.1", port=6633)
    s1 = net.addSwitch("s1")

    hosts = []
    for i in range(1, 7):
        h = net.addHost("h%d" % i,
                        ip="10.0.0.%d/24" % i,
                        mac="00:00:00:00:00:0%d" % i)
        opts = {"bw": bw} if bw else {}
        net.addLink(h, s1, port2=i, **opts)
        hosts.append(h)

    net.start()

    # static ARP so hosts never need ARP floods (VIP included)
    for h in hosts:
        h.cmd("arp -s %s %s" % (VIP, VMAC))
        for o in hosts:
            if o is not h:
                h.cmd("arp -s %s %s" % (o.IP(), o.MAC()))
    return net


def start_server(net, name):
    delay = SERVER_DELAYS[name]
    net.get(name).cmd("python3 %s/server.py --name %s --delay %s > /tmp/%s.log 2>&1 &"
                      % (HERE, name, delay, name))


def start_servers(net):
    for name in SERVER_DELAYS:
        start_server(net, name)
    time.sleep(2)


def stop_servers(net):
    net.get("h4").cmd("pkill -f server.py")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--bw", type=float, default=None,
                   help="limit every link to this many Mbps")
    a = p.parse_args()

    setLogLevel("info")
    net = build_network(a.bw)
    start_servers(net)

    print("\n*** Adaptive Load Balancer ready. VIP = %s" % VIP)
    print("*** Servers: h4 (0.02s), h5 (0.02s), h6 (0.30s - slow)")
    print("*** Try: h1 python3 client.py -n 300 -c 10")
    print("*** Fault injection by hand (see README):")
    print("***   crash h4:     h4 pkill -f 'server.py --name h4'")
    print("***   restart h4:   h4 python3 server.py --name h4 --delay 0.02 &")
    print("***   link down/up: link s1 h4 down   /   link s1 h4 up")
    print("***   overload h5:  h5 pkill -USR1 -f 'server.py --name h5'  (run again to undo)\n")
    CLI(net)

    stop_servers(net)
    net.stop()


if __name__ == "__main__":
    main()
