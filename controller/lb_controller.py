"""
Adaptive Load Balancer - POX (OpenFlow 1.0) component.

Run from the pox folder:
    ./pox.py lb_controller --mode=adaptive
    ./pox.py lb_controller --mode=roundrobin
    ./pox.py lb_controller --mode=leastconn
"""
import json

from pox.core import core
import pox.openflow.libopenflow_01 as of
from pox.lib.addresses import IPAddr, EthAddr
from pox.lib.packet.ethernet import ethernet
from pox.lib.packet.arp import arp

log = core.getLogger()

VIP = IPAddr("10.0.0.100")
VMAC = EthAddr("00:00:00:00:00:64")
REPORT_PORT = 9999          # servers send UDP load reports to VIP:9999
IDLE_TIMEOUT = 30           # seconds
LB_PRIORITY = 0x9000

SERVERS = [
    {"name": "h4", "ip": IPAddr("10.0.0.4"), "mac": EthAddr("00:00:00:00:00:04"), "port": 4},
    {"name": "h5", "ip": IPAddr("10.0.0.5"), "mac": EthAddr("00:00:00:00:00:05"), "port": 5},
    {"name": "h6", "ip": IPAddr("10.0.0.6"), "mac": EthAddr("00:00:00:00:00:06"), "port": 6},
]

MODES = ("roundrobin", "leastconn", "adaptive")


class LoadBalancer(object):
    def __init__(self, connection, mode):
        self.connection = connection
        self.mode = mode
        self.mac_to_port = {}
        self.sticky = {}
        self.rr = 0
        self.by_ip = {}
        self.stats = {}
        for s in SERVERS:
            self.by_ip[s["ip"]] = s
            self.stats[s["name"]] = {"active": 0, "avg_ms": 1.0,
                                     "pending": 0, "assigned": 0}
        connection.addListeners(self)
        log.info("Load balancer started, mode=%s (switch %s)", mode, connection)

    # ------------------------------------------------------------------
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
        except Exception as ex:
            log.warning("bad load report from %s: %s", ip.srcip, ex)

    # ------------------------------------------------------------------
    def choose(self):
        if self.mode == "roundrobin":
            s = SERVERS[self.rr % len(SERVERS)]
            self.rr += 1
            return s

        def key(s):
            st = self.stats[s["name"]]
            if self.mode == "leastconn":
                return (st["active"] + st["pending"], st["assigned"])
            # adaptive: expected wait time
            return ((st["active"] + st["pending"] + 1) * st["avg_ms"],
                    st["assigned"])

        return min(SERVERS, key=key)

    def balance(self, event, ip, l4):
        in_port = event.port
        proto = ip.protocol
        flow_key = (ip.srcip, l4.srcport, proto)

        server = self.sticky.get(flow_key)
        if server is None:
            server = self.choose()
            if len(self.sticky) > 10000:
                self.sticky.clear()
            self.sticky[flow_key] = server
            st = self.stats[server["name"]]
            st["pending"] += 1
            st["assigned"] += 1
            totals = " ".join("%s=%d" % (s["name"], self.stats[s["name"]]["assigned"])
                              for s in SERVERS)
            log.info("%s %s:%s -> %s | active=%d pending=%d avg=%.0fms | totals %s",
                     "TCP" if proto == 6 else "UDP", ip.srcip, l4.srcport,
                     server["name"], st["active"], st["pending"], st["avg_ms"], totals)

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


def launch(mode="adaptive"):
    if mode not in MODES:
        raise ValueError("mode must be one of %s" % (MODES,))

    def start(event):
        LoadBalancer(event.connection, mode)

    core.openflow.addListenerByName("ConnectionUp", start)
