"""
Adaptive Load Balancer - POX (OpenFlow 1.0) component.

Run from the pox folder:
    ./pox.py lb_controller --mode=adaptive
    ./pox.py lb_controller --mode=roundrobin        (static baseline)
    ./pox.py lb_controller --mode=leastconn

Options:
    --health=yes|no   server failure and overload detection.
                      Default: on for leastconn and adaptive, off for
                      roundrobin, because the static baseline has no monitoring.
    --link_mbps=10    capacity of each link, used to turn port byte counters
                      into a utilisation figure (match topology.py --bw).

What it does
  * answers ARP for the VIP and balances every new TCP/UDP flow sent to it
  * reads the servers' UDP load reports (active requests, avg_ms)     [server load]
  * polls the switch port counters once a second                      [network load]
  * marks a server DOWN when its reports stop or its switch port goes down,
    deletes the flow rules that point at it and stops giving it new flows
  * marks a server OVERLOADED when it reports too many active requests;
    new flows avoid it until it recovers
  * flow affinity: a flow that already has a server keeps it (sticky table plus
    switch rules). Only a flow whose server died is decided again.
"""
import json
import time

from pox.core import core
import pox.openflow.libopenflow_01 as of
from pox.lib.addresses import IPAddr, EthAddr
from pox.lib.packet.ethernet import ethernet
from pox.lib.packet.arp import arp
from pox.lib.recoco import Timer
from pox.lib.util import str_to_bool

log = core.getLogger()

VIP = IPAddr("10.0.0.100")
VMAC = EthAddr("00:00:00:00:00:64")
REPORT_PORT = 9999          # servers send UDP load reports to VIP:9999
IDLE_TIMEOUT = 30           # seconds a flow rule survives without traffic
LB_PRIORITY = 0x9000

FAIL_TIMEOUT = 3.0          # seconds without a load report -> server is DOWN
OVERLOAD_ACTIVE = 8         # reported active requests at/above this -> OVERLOADED
OVERLOAD_CLEAR = 3          # back at/below this -> no longer overloaded
STICKY_TTL = 120            # forget a flow's server after this many idle seconds
PORT_STATS_INTERVAL = 1.0   # seconds between port counter polls
NET_WEIGHT = 3.0            # a fully used link makes a server look (1 + 3) = 4x busier

SERVERS = [
    {"name": "h4", "ip": IPAddr("10.0.0.4"), "mac": EthAddr("00:00:00:00:00:04"), "port": 4},
    {"name": "h5", "ip": IPAddr("10.0.0.5"), "mac": EthAddr("00:00:00:00:00:05"), "port": 5},
    {"name": "h6", "ip": IPAddr("10.0.0.6"), "mac": EthAddr("00:00:00:00:00:06"), "port": 6},
]

MODES = ("roundrobin", "leastconn", "adaptive")


class LoadBalancer(object):
    def __init__(self, connection, mode, health, link_mbps):
        self.connection = connection
        self.mode = mode
        self.health = health
        self.link_mbps = link_mbps
        self.alive = True
        self.mac_to_port = {}
        self.sticky = {}            # flow key -> {"server": ..., "t": last seen}
        self.rr = 0
        self.by_ip = {}
        self.by_port = {}
        self.stats = {}
        self.port_bytes = {}        # port -> (time, rx+tx bytes)
        self.polls = 0
        now = time.time()
        for s in SERVERS:
            self.by_ip[s["ip"]] = s
            self.by_port[s["port"]] = s
            self.stats[s["name"]] = {
                "active": 0, "avg_ms": 1.0, "pending": 0, "assigned": 0,
                "up": True, "overloaded": False, "last_report": now,
                "net_mbps": 0.0, "net_util": 0.0}
        connection.addListeners(self)
        core.openflow.addListenerByName("PortStatsReceived", self._on_port_stats)
        self.timers = [Timer(1.0, self._tick, recurring=True),
                       Timer(PORT_STATS_INTERVAL, self._poll_ports, recurring=True)]
        log.info("Load balancer started, mode=%s health=%s link=%.0fMbps (switch %s)",
                 mode, "on" if health else "off", link_mbps, connection)

    # ------------------------------------------------------------------
    def _handle_ConnectionDown(self, event):
        self.alive = False
        for t in self.timers:
            t.cancel()

    def _handle_PortStatus(self, event):
        """A server's switch port went down: failure detected immediately."""
        try:
            server = self.by_port.get(event.port)
            if server is None or not self.health:
                return
            down_flag = getattr(of, "OFPPS_LINK_DOWN", 1)
            if event.deleted or (event.ofp.desc.state & down_flag):
                self.mark_down(server, "switch port %d went down" % event.port)
        except Exception as ex:
            log.warning("port status handling failed: %s", ex)

    def _handle_PacketIn(self, event):
        packet = event.parsed
        if not packet.parsed:
            return
        in_port = event.port

        a = packet.find('arp')
        if a:
            if a.opcode == arp.REQUEST and a.protodst == VIP:
                self.reply_arp(a, in_port)
            else:
                self.learn_forward(event, packet)
            return

        ip = packet.find('ipv4')
        if ip and ip.dstip == VIP:
            udp = packet.find('udp')
            tcp = packet.find('tcp')
            if udp and udp.dstport == REPORT_PORT:
                self.handle_report(ip, udp)
                self.drop(event)
            elif tcp or udp:
                self.balance(event, ip, tcp or udp)
            else:
                self.drop(event)      # e.g. ping to VIP: not served
            return

        self.learn_forward(event, packet)

    # ------------------------------------------------------------------
    def reply_arp(self, a, in_port):
        r = arp()
        r.opcode = arp.REPLY
        r.hwsrc = VMAC
        r.hwdst = a.hwsrc
        r.protosrc = VIP
        r.protodst = a.protosrc
        e = ethernet(type=ethernet.ARP_TYPE, src=VMAC, dst=a.hwsrc)
        e.payload = r
        msg = of.ofp_packet_out()
        msg.data = e.pack()
        msg.in_port = in_port
        msg.actions.append(of.ofp_action_output(port=of.OFPP_IN_PORT))
        self.connection.send(msg)

    def drop(self, event):
        """Free the switch buffer for a packet we do not forward."""
        if event.ofp.buffer_id is not None:
            msg = of.ofp_packet_out()
            msg.buffer_id = event.ofp.buffer_id
            msg.in_port = event.port
            self.connection.send(msg)

    # ------------------------------------------------------------------
    # Server load monitoring (UDP reports) and failure / overload detection
    # ------------------------------------------------------------------
    def handle_report(self, ip, udp):
        server = self.by_ip.get(ip.srcip)
        if server is None:
            return
        try:
            raw = udp.payload
            if not isinstance(raw, bytes):
                raw = raw.pack()
            data = json.loads(raw.decode())
            st = self.stats[server["name"]]
            st["active"] = int(data["active"])
            st["avg_ms"] = max(float(data["avg_ms"]), 0.1)
            st["pending"] = 0     # fresh report replaces our estimate
            st["last_report"] = time.time()
        except Exception as ex:
            log.warning("bad load report from %s: %s", ip.srcip, ex)
            return
        if self.health:
            if not st["up"]:
                self.mark_up(server)
            self.update_overload(server)

    def update_overload(self, server):
        st = self.stats[server["name"]]
        if not st["overloaded"] and st["active"] >= OVERLOAD_ACTIVE:
            st["overloaded"] = True
            log.warning("SERVER %s OVERLOADED (active=%d avg=%.0fms): "
                        "new flows avoid it, existing flows keep running",
                        server["name"], st["active"], st["avg_ms"])
        elif st["overloaded"] and st["active"] <= OVERLOAD_CLEAR:
            st["overloaded"] = False
            log.warning("SERVER %s recovered from overload (active=%d)",
                        server["name"], st["active"])

    def mark_down(self, server, reason):
        st = self.stats[server["name"]]
        if not st["up"]:
            return
        st["up"] = False
        st["overloaded"] = False
        st["pending"] = 0
        lost = [k for k, v in self.sticky.items()
                if v["server"]["name"] == server["name"]]
        for k in lost:
            del self.sticky[k]
        self.purge_flows(server)
        alive = [s["name"] for s in SERVERS if self.stats[s["name"]]["up"]]
        log.warning("SERVER %s DOWN (%s): removed %d flow(s), new traffic goes to %s",
                    server["name"], reason, len(lost), ", ".join(alive) or "nobody")

    def mark_up(self, server):
        st = self.stats[server["name"]]
        st["up"] = True
        st["pending"] = 0
        st["last_report"] = time.time()
        log.warning("SERVER %s RECOVERED: back in rotation", server["name"])

    def purge_flows(self, server):
        """Delete every switch rule that sends traffic to, or came from, a server."""
        fwd = of.ofp_flow_mod(command=of.OFPFC_DELETE)
        fwd.match = of.ofp_match(dl_type=0x0800, nw_dst=VIP)
        fwd.out_port = server["port"]
        self.connection.send(fwd)
        rev = of.ofp_flow_mod(command=of.OFPFC_DELETE)
        rev.match = of.ofp_match(in_port=server["port"], dl_type=0x0800)
        self.connection.send(rev)

    def _tick(self):
        """Once a second: detect silent servers and forget old flows."""
        if not self.alive:
            return
        now = time.time()
        if self.health:
            for s in SERVERS:
                st = self.stats[s["name"]]
                silent = now - st["last_report"]
                if st["up"] and silent > FAIL_TIMEOUT:
                    self.mark_down(s, "no load report for %.0fs" % silent)
        old = [k for k, v in self.sticky.items() if now - v["t"] > STICKY_TTL]
        for k in old:
            del self.sticky[k]

    # ------------------------------------------------------------------
    # Network load monitoring (switch port counters)
    # ------------------------------------------------------------------
    def _poll_ports(self):
        if not self.alive:
            return
        self.connection.send(of.ofp_stats_request(body=of.ofp_port_stats_request()))

    def _on_port_stats(self, event):
        if not self.alive or event.connection is not self.connection:
            return
        now = time.time()
        for ps in event.stats:
            server = self.by_port.get(ps.port_no)
            if server is None:
                continue
            total = ps.rx_bytes + ps.tx_bytes
            prev = self.port_bytes.get(ps.port_no)
            self.port_bytes[ps.port_no] = (now, total)
            if prev and now > prev[0]:
                mbps = (total - prev[1]) * 8.0 / (now - prev[0]) / 1e6
                util = min(mbps / self.link_mbps, 1.0)
                st = self.stats[server["name"]]
                st["net_mbps"] = mbps
                st["net_util"] = 0.5 * st["net_util"] + 0.5 * util
        self.polls += 1
        if self.polls % 5 == 0:
            log.info("network load: %s", "  ".join(
                "%s=%.2fMb/s(%.0f%%)" % (s["name"], self.stats[s["name"]]["net_mbps"],
                                         100 * self.stats[s["name"]]["net_util"])
                for s in SERVERS))

    # ------------------------------------------------------------------
    # Server selection
    # ------------------------------------------------------------------
    def candidates(self):
        if not self.health:
            return list(SERVERS)          # static baseline: no health knowledge
        up = [s for s in SERVERS if self.stats[s["name"]]["up"]]
        ok = [s for s in up if not self.stats[s["name"]]["overloaded"]]
        return ok or up                   # if all are overloaded, use them anyway

    def choose(self):
        cand = self.candidates()
        if not cand:
            return None
        if self.mode == "roundrobin":
            s = cand[self.rr % len(cand)]
            self.rr += 1
            return s

        def key(s):
            st = self.stats[s["name"]]
            if self.mode == "leastconn":
                return (st["active"] + st["pending"], st["assigned"])
            # adaptive: expected wait time, scaled up when the server's link is busy
            return ((st["active"] + st["pending"] + 1) * st["avg_ms"]
                    * (1.0 + NET_WEIGHT * st["net_util"]), st["assigned"])

        return min(cand, key=key)

    def balance(self, event, ip, l4):
        in_port = event.port
        proto = ip.protocol
        flow_key = (ip.srcip, l4.srcport, proto)

        entry = self.sticky.get(flow_key)
        server = entry["server"] if entry else None
        if server is not None and self.health and not self.stats[server["name"]]["up"]:
            server = None                 # its server died: decide again

        if server is None:
            server = self.choose()
            if server is None:
                log.warning("no server available, dropping flow from %s:%s",
                            ip.srcip, l4.srcport)
                self.drop(event)
                return
            if len(self.sticky) > 10000:
                self.sticky.clear()
            st = self.stats[server["name"]]
            st["pending"] += 1
            st["assigned"] += 1
            totals = " ".join("%s=%d" % (s["name"], self.stats[s["name"]]["assigned"])
                              for s in SERVERS)
            log.info("%s %s:%s -> %s | active=%d pending=%d avg=%.0fms net=%.0f%% | totals %s",
                     "TCP" if proto == 6 else "UDP", ip.srcip, l4.srcport,
                     server["name"], st["active"], st["pending"], st["avg_ms"],
                     100 * st["net_util"], totals)
        self.sticky[flow_key] = {"server": server, "t": time.time()}

        # reverse rule first: server -> client, rewrite source back to VIP
        rev = of.ofp_flow_mod()
        rev.priority = LB_PRIORITY
        rev.idle_timeout = IDLE_TIMEOUT
        rev.match = of.ofp_match(in_port=server["port"], dl_type=0x0800,
                                 nw_proto=proto, nw_src=server["ip"],
                                 nw_dst=ip.srcip, tp_src=l4.dstport,
                                 tp_dst=l4.srcport)
        rev.actions.append(of.ofp_action_dl_addr.set_src(VMAC))
        rev.actions.append(of.ofp_action_nw_addr.set_src(VIP))
        rev.actions.append(of.ofp_action_output(port=in_port))
        self.connection.send(rev)

        # forward rule: client -> VIP rewritten to client -> server
        fwd = of.ofp_flow_mod()
        fwd.priority = LB_PRIORITY
        fwd.idle_timeout = IDLE_TIMEOUT
        fwd.match = of.ofp_match(in_port=in_port, dl_type=0x0800,
                                 nw_proto=proto, nw_src=ip.srcip,
                                 nw_dst=VIP, tp_src=l4.srcport,
                                 tp_dst=l4.dstport)
        fwd.actions.append(of.ofp_action_dl_addr.set_dst(server["mac"]))
        fwd.actions.append(of.ofp_action_nw_addr.set_dst(server["ip"]))
        fwd.actions.append(of.ofp_action_output(port=server["port"]))
        fwd.data = event.ofp          # also releases/sends the first packet
        self.connection.send(fwd)

    # ------------------------------------------------------------------
    def learn_forward(self, event, packet):
        """Plain learning switch for everything that is not load balanced."""
        in_port = event.port
        if packet.src != VMAC:
            self.mac_to_port[packet.src] = in_port

        out = self.mac_to_port.get(packet.dst)
        if packet.dst.is_multicast or out is None:
            msg = of.ofp_packet_out()
            msg.data = event.ofp
            msg.in_port = in_port
            msg.actions.append(of.ofp_action_output(port=of.OFPP_FLOOD))
            self.connection.send(msg)
        elif out == in_port:
            self.drop(event)
        else:
            fm = of.ofp_flow_mod()
            fm.match = of.ofp_match.from_packet(packet, in_port)
            fm.idle_timeout = IDLE_TIMEOUT
            fm.actions.append(of.ofp_action_output(port=out))
            fm.data = event.ofp
            self.connection.send(fm)


def launch(mode="adaptive", health=None, link_mbps="10"):
    if mode not in MODES:
        raise ValueError("mode must be one of %s" % (MODES,))
    health_on = (mode != "roundrobin") if health is None else str_to_bool(health)
    link = float(link_mbps)

    def start(event):
        LoadBalancer(event.connection, mode, health_on, link)

    core.openflow.addListenerByName("ConnectionUp", start)
