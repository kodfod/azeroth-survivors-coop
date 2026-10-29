"""Make the co-op server reachable from outside: UPnP port mapping on the
router, a reachability check from the internet, and the addresses to share.

NAT punch-through is not possible: the game only opens a plain WebSocket to
a URL, so guests can't take part in hole punching.
"""
import ipaddress
import json
import re
import socket
import subprocess
import urllib.request
import xml.etree.ElementTree as ET
from urllib.parse import urljoin

SSDP_ADDR = ("239.255.255.250", 1900)
WAN_SERVICES = ("urn:schemas-upnp-org:service:WANIPConnection:2",
                "urn:schemas-upnp-org:service:WANIPConnection:1",
                "urn:schemas-upnp-org:service:WANPPPConnection:1")
CHECK_URL = "https://ifconfig.co/port/{port}"   # answers {"ip", "port", "reachable"}
MAPPING_NAME = "Azeroth Survivors co-op"
CGNAT = ipaddress.ip_network("100.64.0.0/10")    # also Tailscale's range


def local_ip():
    """The LAN address the default route uses (no packet is actually sent)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.0.2.1", 9))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def default_gateway():
    """IPv4 default gateway, read from the Windows routing table."""
    try:
        out = subprocess.run(["route", "print", "-4", "0.0.0.0"], capture_output=True, text=True,
                             timeout=5, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
    except Exception:
        return None
    m = re.search(r"^\s*0\.0\.0\.0\s+0\.0\.0\.0\s+(\d+\.\d+\.\d+\.\d+)", out, re.M)
    return m.group(1) if m else None


def tailscale_ips():
    try:
        addrs = socket.gethostbyname_ex(socket.gethostname())[2]
    except OSError:
        return []
    return [a for a in addrs if ipaddress.ip_address(a) in CGNAT]


def is_public(ip):
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return a.is_global and a not in CGNAT


class Upnp:
    def __init__(self, control_url, service):
        self.control_url, self.service = control_url, service

    @classmethod
    def discover(cls, timeout=3.0):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        sock.settimeout(timeout)
        # Asking the gateway directly too: Windows Firewall often drops the
        # replies to a multicast search but lets a direct reply through.
        targets = [SSDP_ADDR] + ([(gw, 1900)] if (gw := default_gateway()) else [])
        try:
            for st in ("urn:schemas-upnp-org:device:InternetGatewayDevice:1", "upnp:rootdevice"):
                req = ("M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\n"
                       f'MAN: "ssdp:discover"\r\nMX: 2\r\nST: {st}\r\n\r\n')
                for target in targets:
                    sock.sendto(req.encode(), target)
            seen = set()
            while True:
                try:
                    data, _ = sock.recvfrom(4096)
                except socket.timeout:
                    return None
                except ConnectionResetError:
                    continue  # the gateway said nothing listens on 1900: UPnP is off there
                m = re.search(rb"(?im)^location:\s*(\S+)", data)
                if not m or m.group(1) in seen:
                    continue
                seen.add(m.group(1))
                gw = cls._from_description(m.group(1).decode())
                if gw:
                    return gw
        finally:
            sock.close()

    @classmethod
    def _from_description(cls, location):
        try:
            with urllib.request.urlopen(location, timeout=3) as r:
                root = ET.fromstring(r.read())
        except Exception:
            return None
        for svc in root.iter():
            if not svc.tag.endswith("service"):
                continue
            fields = {c.tag.split("}")[-1]: (c.text or "").strip() for c in svc}
            if fields.get("serviceType") in WAN_SERVICES and fields.get("controlURL"):
                return cls(urljoin(location, fields["controlURL"]), fields["serviceType"])
        return None

    def _call(self, action, **args):
        body = "".join(f"<{k}>{v}</{k}>" for k, v in args.items())
        envelope = ('<?xml version="1.0"?><s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
                    's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body>'
                    f'<u:{action} xmlns:u="{self.service}">{body}</u:{action}></s:Body></s:Envelope>')
        req = urllib.request.Request(self.control_url, envelope.encode(), {
            "Content-Type": 'text/xml; charset="utf-8"',
            "SOAPAction": f'"{self.service}#{action}"'})
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.read().decode("utf-8", "replace")

    def external_ip(self):
        m = re.search(r"<NewExternalIPAddress>([^<]*)<", self._call("GetExternalIPAddress"))
        return m.group(1) if m else None

    def add_mapping(self, port, lan_ip, lease=0):
        self._call("AddPortMapping", NewRemoteHost="", NewExternalPort=port, NewProtocol="TCP",
                   NewInternalPort=port, NewInternalClient=lan_ip, NewEnabled=1,
                   NewPortMappingDescription=MAPPING_NAME, NewLeaseDuration=lease)

    def remove_mapping(self, port):
        self._call("DeletePortMapping", NewRemoteHost="", NewExternalPort=port, NewProtocol="TCP")


def check_reachable(port):
    """Ask an outside service to connect back to us. None if it can't be asked."""
    try:
        # the service turns away Python's default User-Agent
        req = urllib.request.Request(CHECK_URL.format(port=port), headers={
            "Accept": "application/json", "User-Agent": "curl/8.9.1"})
        with urllib.request.urlopen(req, timeout=10) as r:
            info = json.loads(r.read())
        return info.get("ip"), bool(info.get("reachable"))
    except Exception:
        return None


def setup(port, log, use_upnp=True, check=True):
    """Open the port if possible and report what guests should use.
    Returns the Upnp gateway that holds a mapping (to remove on exit), or None."""
    lan = local_ip()
    mapped, gw, wan = None, None, None
    if use_upnp:
        gw = Upnp.discover()
        if not gw:
            log("UPnP: no router answered; it's probably turned off in the router's settings")
        else:
            try:
                wan = gw.external_ip()
                try:
                    gw.add_mapping(port, lan)
                except Exception:
                    gw.add_mapping(port, lan, lease=86400)  # some routers only take timed leases
                mapped = gw
                log(f"UPnP: router forwards TCP {port} to {lan}:{port}")
            except Exception as e:
                log(f"UPnP: router refused the port mapping ({e})")
            if wan and not is_public(wan):
                log(f"UPnP: the router's own internet address is {wan}, which is not public "
                    "(carrier-grade NAT or a second router). Port forwarding can't work here.")

    result = check_reachable(port) if check else None
    if result is None:
        if check:
            log("reachability: couldn't run the outside check")
    else:
        ip, ok = result
        if ok:
            log(f"reachability: port {port} IS reachable from the internet")
            log(f"  friends use: ws://{ip}:{port}/coop/lobby/")
        else:
            log(f"reachability: port {port} is NOT reachable from the internet at {ip}")
            log("  forward TCP port manually on the router, or use Tailscale")

    log(f"  same network: ws://{lan}:{port}/coop/lobby/")
    for ts in tailscale_ips():
        log(f"  over Tailscale: ws://{ts}:{port}/coop/lobby/")
    return mapped
