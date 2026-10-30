# Adaptive Load Balancer (Mininet + POX)

Computer Networks (UE25CS243A), Jackfruit Mini Project: SDN + Socket Programming.

Many clients use one service hosted on several servers. A static balancer such as round robin gives every
server the same share and ignores how busy each one is, so a slow server builds a queue while faster ones
sit idle. This project builds an **adaptive load balancer** inside an SDN. A POX controller exposes one
virtual IP (VIP) to the clients, reads live load reports from the servers, and installs OpenFlow rules that
send each new flow to the least-loaded server. It balances both TCP and UDP traffic.

## Architecture

```
clients: h1 h2 h3 --+
                    +-- s1 (OpenFlow switch) -- POX controller
servers: h4 h5 h6 --+
```

| Item | Value |
|---|---|
| Virtual IP (VIP) | 10.0.0.100 (no real host owns it) |
| Servers | h4 and h5 (0.02 s per request), h6 (0.30 s per request, deliberately slow) |
| Server ports | TCP 5000, UDP 5001 |
| Load reports | UDP to the VIP on port 9999, once per second (active requests and average processing time) |

### How a request flows

1. A client sends a request to the VIP. The controller answers the client's ARP lookup for the VIP.
2. The first packet of each new flow reaches the controller as a packet-in.
3. Each server sends a small UDP load report to the VIP. The controller reads it. This is the socket-to-SDN integration.
4. The controller picks a server using the selected algorithm.
5. It installs two rules in the switch: client to VIP is rewritten to client to server, and server to client is rewritten back to VIP to client. The rest of the flow never touches the controller.

### Algorithms

| Mode | Rule |
|---|---|
| `roundrobin` | Servers take turns, ignoring load |
| `leastconn` | Fewest active plus pending requests |
| `adaptive` | Lowest score, where score = (active + pending + 1) x avg_ms |

"Pending" counts flows assigned to a server since its last load report, which stops a whole burst going to one server.

## Repository structure

```
.
├── server.py                 TCP/UDP backend, sends load reports (runs on h4, h5, h6)
├── client.py                 Load generator, prints per-server counts, latency, throughput
├── topology.py               Mininet topology: s1, h1 to h6, starts the servers
├── controller/
│   └── lb_controller.py      POX load balancer (copy into pox/ext/ to run)
└── docs/screenshots/         Output screenshots for every mode and test case
```

## Requirements

- Ubuntu (tested on WSL2) with Python 3
- Mininet and Open vSwitch: `sudo apt install -y mininet openvswitch-switch`
- POX: `git clone https://github.com/noxrepo/pox ~/pox`

## How to run

```bash
# 1. Put the controller where POX can load it
cp controller/lb_controller.py ~/pox/ext/

# 2. Make sure Open vSwitch is running
sudo service openvswitch-switch start

# 3. Terminal 1: start the controller (choose roundrobin, leastconn or adaptive)
cd ~/pox
LB_MODE=adaptive python3 pox.py lb_controller

# 4. Terminal 2: start the network, from the project folder
cd <this repo>
sudo mn -c
sudo python3 topology.py
```

At the `mininet>` prompt, run the three test cases:

```
h1 python3 client.py -n 20 -c 2
h1 python3 client.py -n 300 -c 10
h1 python3 client.py --proto udp -n 300 -c 10
```

To compare modes, type `exit`, press Ctrl+C in Terminal 1, then repeat with `LB_MODE=roundrobin` and `LB_MODE=leastconn`.
To clean up: `exit`, then `sudo mn -c`.

## Test cases and results

Each mode was run on three loads. Requests per server are shown as h4 / h5 / h6.

| Test | Mode | Requests h4 / h5 / h6 | Avg latency | Total time | Throughput |
|---|---|---|---|---|---|
| TCP, 20 requests, 2 workers | Adaptive | 10 / 10 / 0 | 92.4 ms | 0.93 s | 21.6 req/s |
| | Round robin | 7 / 6 / 6 (1 failed) | 192.3 ms | 10.01 s* | 1.9 req/s* |
| | Least conn | 7 / 6 / 6 (1 failed) | 190.2 ms | 10.01 s* | 1.9 req/s* |
| TCP, 300 requests, 10 workers | Adaptive | 146 / 147 / 6 (1 failed) | 44.8 ms | 10.11 s* | 29.6 req/s* |
| | Round robin | 100 / 99 / 101 | 140.8 ms | 4.39 s | 68.3 req/s |
| | Least conn | 106 / 108 / 86 | 125.4 ms | 3.92 s | 76.6 req/s |
| UDP, 300 requests, 10 workers | Adaptive | 146 / 148 / 6 | 31.8 ms | 1.01 s | 297.7 req/s |
| | Round robin | 100 / 100 / 100 | 129.5 ms | 4.05 s | 74.1 req/s |
| | Least conn | 108 / 106 / 86 | 116.8 ms | 3.64 s | 82.3 req/s |

\* The run included one failed request, so total time stretched to about 10 s and the throughput is not comparable.

### What the results show

- Adaptive sent only 2% of the 300-request traffic to the slow server h6. Round robin sent about 33%, least conn about 29%.
- Adaptive had the lowest average latency in both 300-request tests: about 3x lower than round robin for TCP (44.8 vs 140.8 ms) and about 4x lower for UDP (31.8 vs 129.5 ms).
- On UDP, adaptive also had about 4x the throughput of round robin (297.7 vs 74.1 req/s).
- Adaptive starts evenly until the first load reports arrive (about 1 s), so a few requests still reach h6.
- Each case was run once. The 20-request runs are dominated by startup effects.

## Screenshots

| | TCP, 20 requests | TCP, 300 requests | UDP, 300 requests |
|---|---|---|---|
| Adaptive | ![](docs/screenshots/adaptive_tcp20.png) | ![](docs/screenshots/adaptive_tcp300.png) | ![](docs/screenshots/adaptive_udp300.png) |
| Round robin | ![](docs/screenshots/roundrobin_tcp20.png) | ![](docs/screenshots/roundrobin_tcp300.png) | ![](docs/screenshots/roundrobin_udp300.png) |
| Least conn | ![](docs/screenshots/leastconn_tcp20.png) | ![](docs/screenshots/leastconn_tcp300.png) | ![](docs/screenshots/leastconn_udp300.png) |

## Troubleshooting

- `ovs-vsctl: database connection failed`: run `sudo service openvswitch-switch start`, then `sudo mn -c`.
- Ping or client requests fail: start the controller first, then the network.
- `Address already in use`: stale servers from an earlier run. Run `sudo mn -c` and `sudo pkill -f server.py`.
- If you change the number of servers, update the server table in the controller and the host setup in `topology.py`.

## Team
Hiten T

| Name | SRN | Part |
|---|---|---|
| [Member 1] | [SRN] | [e.g. server.py and client.py] |
| [Member 2] | [SRN] | [e.g. topology.py] |
| [Member 3] | [SRN] | [e.g. lb_controller.py] |
