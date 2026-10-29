#!/usr/bin/env python3
"""
Mininet topology for the Adaptive Load Balancer (POX version).

h1,h2,h3 = clients      h4,h5,h6 = servers      VIP = 10.0.0.100
Host n is connected to switch port n (the controller relies on this).

Run with:  sudo python3 topology.py
"""
import os
import time
from functools import partial

from mininet.net import Mininet
from mininet.node import RemoteController, OVSKernelSwitch
from mininet.cli import CLI
from mininet.log import setLogLevel

VIP = "10.0.0.100"
VMAC = "00:00:00:00:00:64"
HERE = os.path.dirname(os.path.abspath(__file__))

# server name -> simulated work per request (seconds)
SERVER_DELAYS = {"h4": 0.02, "h5": 0.02, "h6": 0.30}


def main():
    setLogLevel("info")
    net = Mininet(controller=None,
                  switch=partial(OVSKernelSwitch, protocols="OpenFlow10"),
                  autoSetMacs=False)

    net.addController("c0", controller=RemoteController,
                      ip="127.0.0.1", port=6633)
    s1 = net.addSwitch("s1")

    hosts = []
    for i in range(1, 7):
        h = net.addHost("h%d" % i,
                        ip="10.0.0.%d/24" % i,
                        mac="00:00:00:00:00:0%d" % i)
        net.addLink(h, s1, port2=i)
        hosts.append(h)

    net.start()

    # static ARP so hosts never need ARP floods (VIP included)
    for h in hosts:
        h.cmd("arp -s %s %s" % (VIP, VMAC))
        for o in hosts:
            if o is not h:
                h.cmd("arp -s %s %s" % (o.IP(), o.MAC()))

    # start the backend servers
    for name, delay in SERVER_DELAYS.items():
        h = net.get(name)
        h.cmd("python3 %s/server.py --name %s --delay %s > /tmp/%s.log 2>&1 &"
              % (HERE, name, delay, name))
    time.sleep(2)

    print("\n*** Adaptive Load Balancer ready. VIP = %s" % VIP)
    print("*** Servers: h4 (0.02s), h5 (0.02s), h6 (0.30s - slow)")
    print("*** Try: h1 python3 client.py -n 300 -c 10\n")
    CLI(net)

    net.get("h4").cmd("pkill -f server.py")
    net.stop()


if __name__ == "__main__":
    main()
