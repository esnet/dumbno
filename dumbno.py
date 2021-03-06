from jsonrpclib import Server
import socket
import time
import json
import sys
import logging
import logging.handlers
import ConfigParser
import gc
import iptools
from collections import namedtuple

def make_rule(s, d, proto="ip", sp=None, dp=None):
    a = "host %s" % s 
    ap = sp and "eq %s" % sp or ""

    b = "host %s" % d
    bp = dp and "eq %s" % dp or ""

    rule = "%s %s %s %s %s" % (proto, a, ap, b, bp)
    return rule.replace("  ", " ").strip()

class AristaACLManager:
    def __init__(self, ip, user, password, ports, egress_ports, logger):
        self.uri = "https://%s:%s@%s/command-api" % (user, password, ip)
        self.ports = ports
        self.egress_ports = egress_ports

        # Enable V6
        self.families = set(['ip', 'ipv6'])
        #self.families = set(['ip'])
        self.acls = {};
        self.ACL = namedtuple("ACL", "name family")

        # Build acls list from names in config and family list.
        for family in self.families:
            for port in ports.values():
                self.acls[self.ACL(port,family)] = []

        self.logger = logger
        self.min = 500
        self.max = 100000
        self.seq = self.min + 1
        self.switch = Server( self.uri)

        self.acl_hitcounts = {}

    def acl_exists(self, acl):
        cmds = [
            "enable",
            "show %s access-lists %s" % (acl.family,acl.name),
        ]
        response = self.switch.runCmds(version=1, cmds=cmds, format='json')
        acls = response[1]['aclList']
        return bool(acls)

    def port_has_acl(self, port, acl):
        cmds = [
            "enable",
            "show running-config interfaces %s" % port,
        ]
        response = self.switch.runCmds(version=1, cmds=cmds, format='text')
        output = response[1]['output']
        expected_line = '%s access-group %s in' % (acl.family,acl.name)
        return expected_line in output

    def setup_acl(self,  acl):
        if self.acl_exists(acl):
            return True

        self.logger.info("Setting up %s ACL %s", acl.family,acl.name)
        cmds = [
            "enable",
            "configure",
            "%s access-list %s" % (acl.family,acl.name),
            "statistics per-entry",
            "10 permit tcp any any fin",
            "20 permit tcp any any syn",
            "30 permit tcp any any rst",
            "100001 permit %s any any" % acl.family,
        ]
        response = self.switch.runCmds(version=1, cmds=cmds)

    def setup_port_acl(self, port, acl):
        self.setup_acl(acl)
        if self.port_has_acl(port, acl):
            return True

        self.logger.info("Setting up %s ACL %s for port %s", acl.family, acl.name, port)
        cmds = [
            "enable",
            "configure",
            "interface %s" % port,
            "%s access-group %s in" % (acl.family,acl.name),
        ]
        response = self.switch.runCmds(version=1, cmds=cmds)

    def setup(self):
        self.logger.info("Setup...")

        for family in self.families:
            for port, acl in self.ports.items():
                self.setup_port_acl(port, self.ACL(acl,family))

    def refresh(self):
        self.all_seqs = set()
        self.all_rules = set()
        self.total_acls = 0 

        cmds = [
            "enable",
        ]
        for acl in self.acls:
            cmds.append("show %s access-lists %s" % (acl.family,acl.name))
        response = self.switch.runCmds(version=1, cmds=cmds, format='json')
      
        for acl, result in zip(self.acls, response[1:]):
            acls = result['aclList'][0]['sequence']

            #save the acl name inside each record
            #packetCount only exists if it is non-zero
            for entry in acls:
                entry['name'] = acl.name
                entry['family'] = acl.family
                entry['counterData'].setdefault('packetCount', 0)

            self.acls[acl] = acls

            seqs  = set(x["sequenceNumber"] for x in acls)
            rules = set(x["text"] for x in acls)

            self.all_seqs.update(seqs)
            self.all_rules.update(rules)

            self.total_acls += len(acls)

        return self.acls

    def dump(self, acls, op="CURRENT"):
        if not acls:
            return
        for x in acls:
            x['op'] = op
            x['packetCount'] = x['counterData']['packetCount']
            self.logger.info('op=%(op)s acl=%(name)s family=%(family)s seq=%(sequenceNumber)s rule="%(text)s" matches=%(packetCount)s' % x)

    def calc_next(self):
        for x in range(self.seq, self.max) + range(self.min, self.seq):
            if x % 2 == 0: continue #i want an odd number
            if x not in self.all_seqs:
                return x
        raise Exception("Too many ACLS?")

    def add_acl(self, src, dst, proto="ip", sport=None, dport=None):
        rule = make_rule(src, dst, proto, sport, dport)

        if rule in self.all_rules:
            return False

        cmds = [
            "enable",
            "configure",
        ]

        self.seq = self.calc_next()

        if iptools.ipv4.validate_ip(src):
            cmdfamily = 'ip'
        elif iptools.ipv6.validate_ip(src):
            cmdfamily = 'ipv6'
        else:
            sys.stderr.write("src IP not v4 or v6:  %s\n" % src)
            sys.exit(1)

        for acl in self.acls:
            if (acl.family == cmdfamily):        
                cmds.extend([
                    "%s access-list %s" % (acl.family,acl.name),
                    "%d deny %s" % (self.seq, rule),
                ])

        self.logger.info("op=ADD seq=%s rule=%r" % (self.seq, rule))
        response = self.switch.runCmds(version=1, cmds=cmds, format='text')
        self.all_rules.add(rule)
        self.all_seqs.add(self.seq)
        return True

    def remove_acls(self, to_remove):
        cmds = [
            "enable",
            "configure",
        ]
            
        # This could be more efficient in the commands we issue to the Arista
        # by grouping rules per acl,family, but we usually don't seen that many
        # at a time anyway.
        for acl, entries in to_remove.items():
            cmds.append("%s access-list %s" % (acl.family,acl.name))
            for r in entries:
                cmds.append("no %s" % r['sequenceNumber'])
        self.logger.debug("Sending:" + "\n".join(cmds))
        response = self.switch.runCmds(version=1, cmds=cmds, format='text')

    def is_expired(self, acl):
        if acl['sequenceNumber'] <= self.min or acl['sequenceNumber'] >= self.max:
            return False
        if 'any any' in acl['text']:
            return False

        hit_key = (acl['name'], acl['family'], acl['sequenceNumber'])
        packet_count = acl['counterData']['packetCount']

        #have I checked this ACL before?
        if hit_key in self.acl_hitcounts:
            #If so, has the packet count stayed the same?
            last_packet_count = self.acl_hitcounts[hit_key]
            if packet_count == last_packet_count:
                del self.acl_hitcounts[hit_key]
                return True

        self.acl_hitcounts[hit_key] = packet_count
        return False

    def remove_expired(self):
        acls = self.refresh()
        to_remove = {}
        to_remove_flat = []

        for acl, entries in acls.items():
            removable = filter(self.is_expired, entries)
            for entry in removable:
                entry['name'] = acl.name
                entry['family'] = acl.family
            to_remove[acl] = removable
            to_remove_flat.extend(removable)

        self.dump(to_remove_flat, op="REMOVE")

        if to_remove_flat:
            self.remove_acls(to_remove)
            acls = self.refresh()
            self.logger.info("total_acls=%d", self.total_acls)
            #self.dump(acls)

    def get_stats(self):
        response = self.switch.runCmds(version=1, cmds=["show interfaces counters"], format='json')[0]
        ifs = response['interfaces']

        ibytes = sum(ifs[x]['inOctets'] for x in self.ports)
        ebytes = sum(ifs[x]['outOctets'] for x in self.egress_ports)
        return ibytes, ebytes

    def stats_loop(self, interval=5):
        l_ibytes, l_ebytes = self.get_stats()
        gig = 1024**3
        self.logger.info("total gigs: in=%d out=%d filtered=%d", l_ibytes/gig, l_ebytes/gig, (l_ibytes-l_ebytes)/gig)

        while True:
            time.sleep(interval)
            ibytes, ebytes = self.get_stats()

            ibw = (ibytes - l_ibytes) *8 / interval / 1024 / 1024
            ebw = (ebytes - l_ebytes) *8 / interval / 1024 / 1024

            self.logger.info("total gigs: in=%d out=%d filtered=%d", ibytes/gig, ebytes/gig, (ibytes-ebytes)/gig)

            self.logger.info("mbps: in=%d out=%d filtered=%d", ibw, ebw, ibw-ebw)

            l_ibytes, l_ebytes = ibytes, ebytes

            gc.collect()

class DummyACLManager:
    def __init__(self, logger, *args, **kwargs):
        self.logger = logger
    
    def setup(self):
        self.logger.info("DummyACLManager: setup: doing nothing")

    def add_acl(self, src, dst, proto="ip", sport=None, dport=None):
        self.logger.info("DummyACLManager: add_acl: src=%s dst=%s proto=%s sport=%s dport=%s",
                         src, dst, proto, sport, dport)
        
    def remove_expired(self):
        self.logger.info("DummyACLManager: remove_expired: doing nothing")

    def stats_loop(self, interval=5):
        while True:
            self.logger.info("DummyACLManager: stats_loop: doing nothing")
            time.sleep(interval)

BACKENDS = {
    'arista': AristaACLManager,
    'dummy':  DummyACLManager,
}
DEFAULT_BACKEND = 'arista'

class ACLSvr:
    def __init__(self, mgr):
        self.mgr = mgr
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(('', 9000))
        self.sock.settimeout(5)
        self.last_check = 0

    def check(self):
        if time.time() - self.last_check > 30:
            self.mgr.remove_expired()
            self.last_check = time.time()

    def run(self):
        self.mgr.logger.info("Ready..")
        while True:
            self.check()
            sys.stdout.flush()

            try :
                data, addr = self.sock.recvfrom(1024)
            except socket.timeout:
                continue

            record = json.loads(data)
            self.mgr.add_acl(**record)
            self.sock.sendto("ok", addr)

class ACLClient:
    def __init__(self, host, port=9000):
        self.addr = (host, port)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.settimeout(1)
    
    def add_acl(self, src, dst, proto="ip", sport=None, dport=None):
        msg = json.dumps(dict(src=src,dst=dst,proto=proto,sport=sport,dport=dport))
        self.sock.sendto(msg, self.addr)
        try :
            data, addr = self.sock.recvfrom(1024)
            return data
        except socket.timeout:
            return None

def read_config(cfg_file):
    cfg = ConfigParser.ConfigParser()
    cfg.optionxform=str
    read = cfg.read([cfg_file])
    if not read:
        sys.stderr.write("Error Reading config file %s\n" % cfg_file)
        sys.exit(1)

    config = dict(cfg.items('switch'))
    config["ports"] = dict(cfg.items('ports'))
    config["egress_ports"] = []
    if cfg.has_section('egress_ports'):
        config["egress_ports"] = dict(cfg.items('egress_ports'))

    return config

def get_logger(name="dumbno"):
    logging.basicConfig(level=logging.INFO)
    logger = logging.getLogger(name)
    formatter = logging.Formatter('%(name)s: %(levelname)s %(message)s')
    handler = logging.handlers.SysLogHandler(address='/dev/log')
    handler.setFormatter(formatter)
    handler.setLevel(logging.INFO)
    logger.addHandler(handler)
    return logger


def get_backend(logger, config):
    configured_backend = config.get("backend", DEFAULT_BACKEND)
    backend_class = BACKENDS[configured_backend]
    if 'backend' in config:
        del config['backend']
    logger.debug("Initializing backend with config: %r", config)
    return backend_class(logger=logger, **config)

def launch(config, setup=False):
    logger = get_logger()
    logger.info("Started")
    mgr = get_backend(logger, config)
    if setup:
        mgr.setup()

    svr = ACLSvr(mgr)
    svr.run()

def run_stats(config, setup=False):
    logger = get_logger("dumbno_stats")
    mgr = get_backend(logger, config)
    return mgr.stats_loop()

def main():
    if len(sys.argv) < 2:
        sys.stderr.write("Usage: %s dumbno.ini [setup|stats]\n" % sys.argv[0])
        sys.exit(1)
    cfg_file = sys.argv[1]
    setup = len(sys.argv) == 3 and sys.argv[2] == 'setup'
    stats = len(sys.argv) == 3 and sys.argv[2] == 'stats'

    config = read_config(cfg_file)
    if stats:
        run_stats(config)
    else:
        launch(config, setup=setup)

if __name__ == "__main__":
    main()
