"""Proxmox multi-cluster deploy bot: Discord, no prefix, Components V2, SSH access.

Commands (no prefix, or with optional `qy!`). Work in servers AND in the bot's DMs:
    create <ram> <cpu> <disk> @user|id [cluster]   admin only
    manage [@user|id]                              your containers (admins can open others')
    reinstall <vmid> [cluster]
    status                                         admin only, cluster/node overview
    help

RAM : "2048" = MB, "2g" / "2gb" = GB, a bare number below 128 is treated as GB.
Disk: GB ("20" or "20g").
In DMs you can't @mention easily, so a raw Discord user ID (or <@id>) works everywhere.

Cluster ssh_mode in config.yaml: "forward" (public DNAT port), "tmate" (temporary ssh relay)
or "sshx" (temporary web-terminal link from sshx.io; outbound only, no public IP needed).

Security (config.yaml `security:` section): per-container bandwidth + process caps, nesting off by default,
per-user command rate limit, admin `suspend` / `unsuspend`. Pair it with harden_node.sh on every node.

Cluster backend in config.yaml: "proxmox" (default) or "incus" -- a plain KVM VPS/VDS that is NOT
running Proxmox. Containers there are Incus (LXC) system containers, controlled over SSH.
"""
import asyncio
import ipaddress
import json
import os
import re
import secrets
import shlex
import sqlite3
import string
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from typing import NamedTuple, Optional

import discord
import paramiko
import yaml
from discord import ui
from proxmoxer import ProxmoxAPI

# ================================================================= config
cfg = yaml.safe_load(open("config.yaml", encoding="utf-8"))
ROOT_ADMINS = set(cfg.get("admin_ids", []))  # from config.yaml; can't be removed via the bot
MAX_PER_USER = int(cfg.get("max_cts_per_user", 3))
BRAND = cfg.get("brand", "Proxmox Cloud")
LIM = cfg.get("limits", {})
MAX_CPU = int(LIM.get("max_cpu", 16))
MAX_RAM_MB = int(LIM.get("max_ram_mb", 32768))
MAX_DISK_GB = int(LIM.get("max_disk_gb", 500))

INV = cfg.get("invites", {})
INVITES_ENABLED = bool(INV.get("enabled", False))
INVITE_ANNOUNCE_CHANNEL_ID = INV.get("announce_channel_id")
INVITE_CLUSTER = INV.get("cluster")  # None = auto-pick
REWARD_TIERS = sorted(INV.get("reward_tiers", []), key=lambda t: t["invites"])
BOOST_TIERS = sorted(cfg.get("boost_tiers", []), key=lambda t: t["boosts"])

OS_TEMPLATES = cfg.get("os_templates", {})  # key -> {"label": ..., "file": ...}; empty = skip OS picker

# false (default): never refuse a deploy because a node is low on free RAM (overcommit is allowed);
# the container just goes on the online node with the most free RAM. true: old behaviour (RAM + 15% must fit).
ENFORCE_CAPACITY = bool(cfg.get("enforce_capacity", False))

# ---- security settings (all optional; safe defaults)
SEC = cfg.get("security", {}) or {}
SEC_DOCKER = bool(SEC.get("install_docker", False))             # install Docker in every new container
SEC_NESTING = bool(SEC.get("nesting", False)) or SEC_DOCKER                  # Docker/LXC inside containers: bigger attack surface
SEC_NET_MBIT = int(SEC.get("net_mbit", 100))                   # per-container bandwidth cap, Mbit/s (0 = unlimited)
SEC_PIDS_MAX = int(SEC.get("pids_max", 2048))                  # per-container process cap, stops fork bombs (0 = off)
SEC_PORT_ISOLATION = bool(SEC.get("port_isolation", True))     # incus: containers can't talk to each other
_RL = SEC.get("rate_limit", {}) or {}
RL_COMMANDS = int(_RL.get("commands", 6))                      # max commands ...
RL_SECONDS = float(_RL.get("per_seconds", 10))                 # ... per this many seconds, per non-admin user
try:
    if os.stat("config.yaml").st_mode & 0o077:
        print("[security] WARNING: config.yaml is readable by other users (it holds tokens) - run: chmod 600 config.yaml", flush=True)
except OSError:
    pass

_CMD_TIMES: dict = {}   # user id -> recent command timestamps
_SSH_LAST: dict = {}    # (user, cluster, vmid) -> last time the SSH/access button ran


def rate_limited(uid: int) -> bool:
    """True if this (non-admin) user is sending commands too fast."""
    if is_admin(uid):
        return False
    now = time.time()
    q = _CMD_TIMES.setdefault(uid, deque())
    while q and now - q[0] > RL_SECONDS:
        q.popleft()
    if len(q) >= RL_COMMANDS:
        return True
    q.append(now)
    return False

MB = 1024 * 1024
GB = 1024 ** 3
GREEN = discord.Colour.from_rgb(46, 204, 113)
RED = discord.Colour.from_rgb(231, 76, 60)
BLUE = discord.Colour.from_rgb(88, 101, 242)
GOLD = discord.Colour.from_rgb(241, 196, 15)
GREY = discord.Colour.from_rgb(127, 140, 141)

RES_LOCK = threading.Lock()  # guards VMID / IP / port reservation


def dbg(msg: str):
    """Timestamped progress line for journalctl (journalctl -u proxmox-bot -f)."""
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)

# ==================================================================== db
db = sqlite3.connect("bot.db", check_same_thread=False)
db.execute(
    """CREATE TABLE IF NOT EXISTS cts (
        vmid INTEGER, cluster TEXT, node TEXT, owner INTEGER,
        hostname TEXT, ip TEXT, ram INTEGER, cpu INTEGER, disk INTEGER,
        created REAL, ssh_port INTEGER, state TEXT DEFAULT 'ready',
        PRIMARY KEY (vmid, cluster))"""
)
_have = {r[1] for r in db.execute("PRAGMA table_info(cts)")}  # migrate older bot.db
if "ssh_port" not in _have:
    db.execute("ALTER TABLE cts ADD COLUMN ssh_port INTEGER")
if "state" not in _have:
    db.execute("ALTER TABLE cts ADD COLUMN state TEXT DEFAULT 'ready'")
if "tmate" not in _have:
    db.execute("ALTER TABLE cts ADD COLUMN tmate TEXT")
if "tmate_at" not in _have:
    db.execute("ALTER TABLE cts ADD COLUMN tmate_at REAL")
if "suspended" not in _have:
    db.execute("ALTER TABLE cts ADD COLUMN suspended INTEGER DEFAULT 0")
db.execute(
    """CREATE TABLE IF NOT EXISTS admins (
        user_id INTEGER PRIMARY KEY, added_by INTEGER, added REAL)"""
)
db.execute(
    """CREATE TABLE IF NOT EXISTS ct_shares (
        cluster TEXT, vmid INTEGER, shared_with INTEGER, shared_by INTEGER, shared_at REAL,
        PRIMARY KEY (cluster, vmid, shared_with))"""
)
db.execute(
    """CREATE TABLE IF NOT EXISTS invite_cache (
        guild_id INTEGER, code TEXT, inviter_id INTEGER, uses INTEGER,
        PRIMARY KEY (guild_id, code))"""
)
db.execute(
    """CREATE TABLE IF NOT EXISTS invite_credits (
        guild_id INTEGER, joined_id INTEGER, inviter_id INTEGER, code TEXT, joined_at REAL,
        PRIMARY KEY (guild_id, joined_id))"""
)
db.execute(
    """CREATE TABLE IF NOT EXISTS reward_claims (
        inviter_id INTEGER, tier_invites INTEGER, claimed_at REAL,
        PRIMARY KEY (inviter_id, tier_invites))"""
)
db.execute(
    """CREATE TABLE IF NOT EXISTS boosts (
        user_id INTEGER PRIMARY KEY, count INTEGER DEFAULT 0, set_by INTEGER, updated_at REAL)"""
)
db.execute(
    """CREATE TABLE IF NOT EXISTS boost_claims (
        user_id INTEGER, tier_boosts INTEGER, claimed_at REAL,
        PRIMARY KEY (user_id, tier_boosts))"""
)
db.commit()

db.execute(
    """CREATE TABLE IF NOT EXISTS extra_ports (
        cluster TEXT, vmid INTEGER, public_port INTEGER, target_port INTEGER, proto TEXT,
        assigned_by INTEGER, assigned_at REAL,
        PRIMARY KEY (cluster, public_port))"""
)
db.commit()
if "token" not in {r[1] for r in db.execute("PRAGMA table_info(cts)")}:
    db.execute("ALTER TABLE cts ADD COLUMN token TEXT")
    db.commit()
CT_FIELDS = ["vmid", "cluster", "node", "owner", "hostname", "ip", "ram", "cpu", "disk", "ssh_port", "tmate", "tmate_at", "suspended"]
COLS = ",".join(CT_FIELDS)


# --------------------------------------------------------------- admins
def is_admin(user_id: int) -> bool:
    if user_id in ROOT_ADMINS:
        return True
    return db.execute("SELECT 1 FROM admins WHERE user_id=?", (user_id,)).fetchone() is not None


def bot_admins() -> list[int]:
    return [r[0] for r in db.execute("SELECT user_id FROM admins")]


def add_admin(user_id: int, added_by: int) -> bool:
    """Returns False if already an admin."""
    if is_admin(user_id):
        return False
    db.execute("INSERT INTO admins VALUES (?,?,?)", (user_id, added_by, time.time()))
    db.commit()
    return True


def remove_admin(user_id: int) -> str:
    """Returns 'root' if protected, 'not_admin' if not one, else 'removed'."""
    if user_id in ROOT_ADMINS:
        return "root"
    if db.execute("SELECT 1 FROM admins WHERE user_id=?", (user_id,)).fetchone() is None:
        return "not_admin"
    db.execute("DELETE FROM admins WHERE user_id=?", (user_id,))
    db.commit()
    return "removed"


# --------------------------------------------------------------- invites
def cache_invites(guild_id: int, invs: list[discord.Invite]):
    for inv in invs:
        if inv.inviter is None:  # vanity URL invite, no owner to credit
            continue
        db.execute(
            "INSERT INTO invite_cache VALUES (?,?,?,?) "
            "ON CONFLICT(guild_id,code) DO UPDATE SET uses=excluded.uses, inviter_id=excluded.inviter_id",
            (guild_id, inv.code, inv.inviter.id, inv.uses or 0),
        )
    db.commit()


def diff_used_invite(guild_id: int, invs: list[discord.Invite]) -> Optional[discord.Invite]:
    """Compare live invites to the cache; return the one whose uses went up."""
    cached = {r[0]: r[1] for r in db.execute("SELECT code, uses FROM invite_cache WHERE guild_id=?", (guild_id,))}
    hit = None
    for inv in invs:
        if inv.inviter is None:
            continue
        before = cached.get(inv.code, 0)
        if (inv.uses or 0) > before:
            hit = inv
            break
    cache_invites(guild_id, invs)
    return hit


def record_join(guild_id: int, joined_id: int, inviter_id: Optional[int], code: Optional[str]):
    db.execute(
        "INSERT OR IGNORE INTO invite_credits VALUES (?,?,?,?,?)",
        (guild_id, joined_id, inviter_id, code, time.time()),
    )
    db.commit()


def invite_count(guild_id: int, inviter_id: int) -> int:
    return db.execute(
        "SELECT COUNT(*) FROM invite_credits WHERE guild_id=? AND inviter_id=?", (guild_id, inviter_id)
    ).fetchone()[0]


def invite_leaderboard(guild_id: int, limit: int = 10):
    return db.execute(
        "SELECT inviter_id, COUNT(*) c FROM invite_credits WHERE guild_id=? AND inviter_id IS NOT NULL "
        "GROUP BY inviter_id ORDER BY c DESC LIMIT ?",
        (guild_id, limit),
    ).fetchall()


def claimed_tiers(inviter_id: int) -> set[int]:
    return {r[0] for r in db.execute("SELECT tier_invites FROM reward_claims WHERE inviter_id=?", (inviter_id,))}


def next_claimable_tier(guild_id: int, inviter_id: int):
    count = invite_count(guild_id, inviter_id)
    done = claimed_tiers(inviter_id)
    for tier in REWARD_TIERS:
        if tier["invites"] not in done and count >= tier["invites"]:
            return tier, count
    return None, count


def mark_claimed(inviter_id: int, tier_invites: int):
    db.execute("INSERT OR IGNORE INTO reward_claims VALUES (?,?,?)", (inviter_id, tier_invites, time.time()))
    db.commit()


def unmark_claimed(inviter_id: int, tier_invites: int):
    db.execute("DELETE FROM reward_claims WHERE inviter_id=? AND tier_invites=?", (inviter_id, tier_invites))
    db.commit()


# ---------------------------------------------------------------- boosts
def get_boosts(user_id: int) -> int:
    r = db.execute("SELECT count FROM boosts WHERE user_id=?", (user_id,)).fetchone()
    return r[0] if r else 0


def set_boosts(user_id: int, count: int, set_by: int):
    db.execute(
        "INSERT INTO boosts VALUES (?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET count=excluded.count, set_by=excluded.set_by, updated_at=excluded.updated_at",
        (user_id, count, set_by, time.time()),
    )
    db.commit()


def claimed_boost_tiers(user_id: int) -> set[int]:
    return {r[0] for r in db.execute("SELECT tier_boosts FROM boost_claims WHERE user_id=?", (user_id,))}


def mark_boost_claimed(user_id: int, tier_boosts: int):
    db.execute("INSERT OR IGNORE INTO boost_claims VALUES (?,?,?)", (user_id, tier_boosts, time.time()))
    db.commit()


def unmark_boost_claimed(user_id: int, tier_boosts: int):
    db.execute("DELETE FROM boost_claims WHERE user_id=? AND tier_boosts=?", (user_id, tier_boosts))
    db.commit()


def best_claimable_tier(guild_id: int, user_id: int):
    """The single best (highest-RAM) unclaimed tier the user currently qualifies for,
    across invite tiers and boost tiers combined. Returns (kind, tier) or (None, None)."""
    candidates = []
    inv_tier, _ = next_claimable_tier(guild_id, user_id)
    if inv_tier:
        candidates.append(("invite", inv_tier))
    b_count, b_done = get_boosts(user_id), claimed_boost_tiers(user_id)
    for t in BOOST_TIERS:
        if t["boosts"] not in b_done and b_count >= t["boosts"]:
            candidates.append(("boost", t))
    if not candidates:
        return None, None
    kind, tier = max(candidates, key=lambda c: c[1]["ram_gb"])
    return kind, tier


class CT(NamedTuple):
    vmid: int
    cluster: str
    node: str
    owner: int
    hostname: str
    ip: Optional[str]
    ram: int
    cpu: int
    disk: int
    ssh_port: Optional[int]
    tmate: Optional[str]
    tmate_at: Optional[float]
    suspended: Optional[int] = 0


def get_ct(cluster: str, vmid: int) -> Optional[CT]:
    r = db.execute(f"SELECT {COLS} FROM cts WHERE cluster=? AND vmid=?", (cluster, vmid)).fetchone()
    return CT(*r) if r else None


def _known_sql(col: str = "cluster"):
    """SQL fragment + params limiting a query to clusters that are still in config.yaml."""
    names = list(CLUSTERS)
    return f"{col} IN ({','.join('?' * len(names))})", names


def user_cts(owner: int) -> list[CT]:
    frag, names = _known_sql()
    rows = db.execute(
        f"SELECT {COLS} FROM cts WHERE owner=? AND state='ready' AND {frag} ORDER BY vmid", (owner, *names)
    ).fetchall()
    return [CT(*r) for r in rows]


def visible_cts(owner_id: int, viewer_id: int) -> list[CT]:
    """Containers `owner_id` has that `viewer_id` is allowed to see: everything if they're
    the same person or the viewer is an admin, otherwise only ones shared via share-user."""
    if owner_id == viewer_id or is_admin(viewer_id):
        return user_cts(owner_id)
    sel = ",".join(f"c.{f}" for f in CT_FIELDS)
    frag, names = _known_sql("c.cluster")
    rows = db.execute(
        f"SELECT {sel} FROM cts c JOIN ct_shares s ON s.cluster=c.cluster AND s.vmid=c.vmid "
        f"WHERE c.owner=? AND s.shared_with=? AND c.state='ready' AND {frag} ORDER BY c.vmid",
        (owner_id, viewer_id, *names),
    ).fetchall()
    return [CT(*r) for r in rows]


def share_containers(owner_id: int, target_id: int) -> int:
    rows = db.execute("SELECT cluster, vmid FROM cts WHERE owner=? AND state='ready'", (owner_id,)).fetchall()
    for cluster, vmid in rows:
        db.execute(
            "INSERT OR IGNORE INTO ct_shares VALUES (?,?,?,?,?)",
            (cluster, vmid, target_id, owner_id, time.time()),
        )
    db.commit()
    return len(rows)


def unshare_containers(owner_id: int, target_id: int) -> int:
    cur = db.execute("DELETE FROM ct_shares WHERE shared_by=? AND shared_with=?", (owner_id, target_id))
    db.commit()
    return cur.rowcount


def is_shared_with(owner_id: int, viewer_id: int) -> bool:
    return db.execute(
        "SELECT 1 FROM ct_shares WHERE shared_by=? AND shared_with=? LIMIT 1", (owner_id, viewer_id)
    ).fetchone() is not None


# ================================================================ proxmox
_SSH_POOL: dict = {}   # (host, port, user, key) -> connected paramiko client, shared by all threads
_SSH_LOCK = threading.Lock()


class Cluster:
    def __init__(self, c: dict):
        self.c = c
        self.name = c["name"]
        self.api = None
        if self.backend() == "proxmox":
            self.api = ProxmoxAPI(
                c["host"],
                user=c["user"],
                token_name=c["token_name"],
                token_value=c["token_value"],
                verify_ssl=c.get("verify_ssl", False),
                timeout=30,
            )

    # -- per-node addressing
    def _node(self, node: str) -> dict:
        return self.c.get("nodes", {}).get(node, {})

    def ssh_host(self, node: str) -> str:
        return self._node(node).get("ssh_host", self.c.get("ssh_host", self.c["host"]))

    def public_host(self, node: str) -> str:
        return self._node(node).get("public_host", self.c.get("public_host", self.c["host"]))

    def relay_kind(self) -> str:
        return self.c.get("ssh_mode", "forward")  # "forward", "tmate" or "sshx"

    def ssh_mode(self) -> str:
        """Coarse mode used across the bot: sshx is a temporary relay just like tmate."""
        m = self.relay_kind()
        return "tmate" if m in ("tmate", "sshx") else m

    def backend(self) -> str:
        return self.c.get("backend", "proxmox")  # "proxmox" or "incus"

    def nodes(self) -> list:
        """Node list in Proxmox's shape: node/status/mem/maxmem/cpu. For an Incus VPS the
        'cluster' is a single host whose stats are read over SSH."""
        if self.backend() == "proxmox":
            return self.api.nodes.get()
        name = self.c.get("node_name", self.name)
        script = (
            "awk '/MemTotal/{t=$2} /MemAvailable/{a=$2} END{print t, t-a}' /proc/meminfo; "
            "nproc; cut -d' ' -f1 /proc/loadavg"
        )
        rc, out, err = self.run(name, script, timeout=20)
        lines = out.strip().splitlines()
        if rc != 0 or len(lines) < 3:
            raise RuntimeError((err or out).strip() or "could not read host stats over SSH")
        total_kb, used_kb = (int(x) for x in lines[0].split())
        ncpu = max(int(lines[1]), 1)
        return [{
            "node": name, "status": "online",
            "maxmem": total_kb * 1024, "mem": used_kb * 1024,
            "cpu": max(0.0, min(1.0, float(lines[2]) / ncpu)),
        }]

    # -- tasks
    def wait(self, node: str, upid: str, timeout: int = 300):
        end = time.time() + timeout
        delay = 0.4
        while time.time() < end:
            st = self.api.nodes(node).tasks(upid).status.get()
            if st["status"] == "stopped":
                if st.get("exitstatus") != "OK":
                    raise RuntimeError(f"Proxmox task failed: {st.get('exitstatus')}")
                return
            time.sleep(delay)
            delay = min(delay * 1.5, 2.0)
        raise TimeoutError("Proxmox task timed out")

    # -- shell access to the node (iptables, pct exec)
    def _ssh(self, node: str, fresh: bool = False):
        """One persistent SSH connection per target host, reused by every command. Opening a new
        connection (key exchange + auth) for each command was the biggest source of latency."""
        s = self.c.get("node_ssh", {})
        host = self.ssh_host(node)
        key = (host, int(s.get("port", 22)), s.get("user", "root"), s.get("key_file"))
        with _SSH_LOCK:
            cli = _SSH_POOL.get(key)
            if cli is not None and not fresh:
                t = cli.get_transport()
                if t is not None and t.is_active():
                    return cli
            _SSH_POOL.pop(key, None)
        cli = paramiko.SSHClient()
        cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        cli.connect(
            host, port=key[1], username=key[2], key_filename=key[3],
            timeout=15, banner_timeout=15, auth_timeout=15,
        )
        cli.get_transport().set_keepalive(30)
        with _SSH_LOCK:
            _SSH_POOL[key] = cli
        return cli

    def run(self, node: str, script: str, timeout: int = 60):
        noisy = "incus " in script  # log incus commands (not the 2-minute iptables sync)
        t0 = time.time()
        if noisy:
            dbg(f"run> {script[:200]}")
        out = err = None
        for attempt in (0, 1):
            cli = self._ssh(node, fresh=bool(attempt))
            try:
                _, out, err = cli.exec_command("bash -lc " + shlex.quote(script), timeout=timeout)
                break
            except (paramiko.SSHException, EOFError, OSError):
                if attempt:  # stale pooled connection: retried once on a fresh one, now give up
                    raise
        chan = out.channel
        deadline = time.time() + timeout
        obuf, ebuf = b"", b""
        # Poll instead of recv_exit_status(): that call has no timeout, so one hung command
        # (a stalled image download, a stuck `incus exec`) froze the whole deploy forever.
        while True:
            got = False
            if chan.recv_ready():
                obuf += chan.recv(65536)
                got = True
            if chan.recv_stderr_ready():
                ebuf += chan.recv_stderr(65536)
                got = True
            if chan.exit_status_ready() and not got and not chan.recv_ready() and not chan.recv_stderr_ready():
                break
            if time.time() > deadline:
                chan.close()
                dbg(f"run! TIMEOUT after {timeout}s: {script[:200]}")
                tail = (obuf + ebuf).decode(errors="replace").strip()[-300:]
                raise TimeoutError(f"command timed out after {timeout}s" + (f" — last output: {tail}" if tail else ""))
            if not got:
                time.sleep(0.03)
        rc = chan.recv_exit_status()
        if noisy:
            dbg(f"run< rc={rc} in {time.time() - t0:.1f}s")
        return rc, obuf.decode(errors="replace"), ebuf.decode(errors="replace")

    def pct_exec(self, node: str, vmid: int, inner: str, timeout: int = 120):
        if self.backend() == "incus":
            return self.run(node, f"timeout {int(timeout)} incus exec ct{int(vmid)} -- bash -c {shlex.quote(inner)} </dev/null", timeout + 10)
        return self.run(node, f"pct exec {int(vmid)} -- bash -c {shlex.quote(inner)}", timeout)


CLUSTERS = {c["name"]: Cluster(c) for c in cfg["clusters"]}


def _scan(cl: "Cluster"):
    try:
        return cl, cl.nodes(), None
    except Exception as e:
        return cl, None, e


def pick_target(ram_mb: int, only: Optional[str] = None, notes: Optional[list] = None):
    """(cluster, node, free_bytes) for the online node with the most free RAM.
    All clusters are queried in parallel. If `notes` is a list, it's filled with
    human-readable reasons for every skipped node/cluster."""
    notes = notes if notes is not None else []
    need = ram_mb * MB * 1.15
    best = None
    cands = [cl for cl in CLUSTERS.values() if not only or cl.name.lower() == only.lower()]
    if not cands:
        notes.append("No cluster matched the name you gave.")
        return None
    with ThreadPoolExecutor(max_workers=len(cands)) as ex:
        results = list(ex.map(_scan, cands))
    for cl, nodes, exc in results:
        if exc is not None:
            notes.append(f"🔴 `{cl.name}`: could not reach the node — {str(exc)[:150]}")
            continue
        for n in nodes:
            if n.get("status") != "online":
                notes.append(f"🔴 `{cl.name}/{n['node']}`: offline")
                continue
            free = (n.get("maxmem") or 0) - (n.get("mem") or 0)
            if ENFORCE_CAPACITY and free < need:
                notes.append(
                    f"⚠️ `{cl.name}/{n['node']}`: {free / GB:.1f} GB RAM free, "
                    f"needs {need / GB:.1f} GB (requested {ram_mb / 1024:g} GB + 15% headroom)"
                )
                continue
            if best is None or free > best[2]:
                best = (cl, n["node"], free)
    return best


# ------------------------------------------------------ allocation helpers
def allocate_ip(cl: Cluster) -> str:
    used = {r[0] for r in db.execute("SELECT ip FROM cts WHERE cluster=?", (cl.name,))}
    start = int(ipaddress.ip_address(cl.c["pool_start"]))
    end = int(ipaddress.ip_address(cl.c["pool_end"]))
    for n in range(start, end + 1):
        ip = str(ipaddress.ip_address(n))
        if ip not in used:
            return ip
    raise RuntimeError(f"IP pool exhausted on {cl.name}")


def is_static(cl: Cluster) -> bool:
    """A cluster is static-IP if it defines subnet/gateway/pool; otherwise DHCP on its bridge."""
    return bool(cl.c.get("subnet"))


def allocate_port(cl: Cluster) -> int:
    used = {r[0] for r in db.execute("SELECT ssh_port FROM cts WHERE cluster=? AND ssh_port IS NOT NULL UNION SELECT public_port FROM extra_ports WHERE cluster=?", (cl.name, cl.name))}
    for p in range(int(cl.c.get("ssh_port_start", 20000)), int(cl.c.get("ssh_port_end", 29999)) + 1):
        if p not in used:
            return p
    raise RuntimeError(f"SSH port range exhausted on {cl.name}")


def gen_password(n: int = 16) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(n))


# ---------------------------------------------------- SSH port forwarding
def _rules(ip: str, port: int, iface: Optional[str]):
    i = f"-i {shlex.quote(iface)} " if iface else ""
    dnat = f"PREROUTING {i}-p tcp --dport {port} -j DNAT --to-destination {ip}:22"
    # scoped to the public interface: a container must not be able to SSH into its neighbours
    fwd = f"FORWARD {i}-p tcp -d {ip} --dport 22 -j ACCEPT"
    return dnat, fwd


def ensure_snippet(ip: str, port: int, iface: Optional[str]) -> str:
    dnat, fwd = _rules(ip, port, iface)
    # drop the old, unscoped rule if an earlier version of the bot created it
    legacy = f"iptables -D FORWARD -p tcp -d {ip} --dport 22 -j ACCEPT 2>/dev/null\n" if iface else ""
    return legacy + (
        f"iptables -t nat -C {dnat} 2>/dev/null || iptables -t nat -A {dnat}\n"
        f"iptables -C {fwd} 2>/dev/null || iptables -I {fwd}\n"
    )


def remove_snippet(ip: str, port: int, iface: Optional[str]) -> str:
    dnat, fwd = _rules(ip, port, iface)
    return (
        f"iptables -t nat -D {dnat} 2>/dev/null\niptables -D {fwd} 2>/dev/null\n"
        f"iptables -D FORWARD -p tcp -d {ip} --dport 22 -j ACCEPT 2>/dev/null\ntrue\n"
    )


def sync_rules():
    """Idempotently re-apply every forward. Survives node reboots."""
    groups: dict[tuple, list] = {}
    for cn, node, ip, port in db.execute(
        "SELECT cluster,node,ip,ssh_port FROM cts WHERE ssh_port IS NOT NULL AND ip IS NOT NULL AND state='ready'"
    ):
        groups.setdefault((cn, node), []).append((ip, port))
    for (cn, node), items in groups.items():
        cl = CLUSTERS.get(cn)
        if not cl:
            continue
        script = "".join(ensure_snippet(ip, p, cl.c.get("public_iface")) for ip, p in items)
        script += "[ -x /usr/local/sbin/botguard.sh ] && /usr/local/sbin/botguard.sh rehook >/dev/null 2>&1\ntrue\n"
        try:
            cl.run(node, script)
        except Exception as e:
            print(f"[sync] {cn}/{node}: {e}")


async def sync_loop():
    while True:
        await asyncio.to_thread(lambda: (sync_rules(), sync_extra_rules()))
        await asyncio.sleep(120)


# ------------------------------------------------------ container actions
BOOT_SSHD = (
    "command -v sshd >/dev/null 2>&1 || (apt-get update -qq && "
    "DEBIAN_FRONTEND=noninteractive apt-get install -y -qq openssh-server); "
    r"sed -i 's/^#\?PermitRootLogin.*/PermitRootLogin yes/;"
    r"s/^#\?PasswordAuthentication.*/PasswordAuthentication yes/' /etc/ssh/sshd_config; "
    "grep -q '^MaxAuthTries' /etc/ssh/sshd_config || echo 'MaxAuthTries 3' >> /etc/ssh/sshd_config; "
    "grep -q '^LoginGraceTime' /etc/ssh/sshd_config || echo 'LoginGraceTime 20' >> /etc/ssh/sshd_config; "
    "systemctl enable ssh >/dev/null 2>&1; systemctl restart ssh 2>/dev/null || systemctl restart sshd"
)


DOCKER_CMD = (
    "command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1 && { echo DOCKER_OK; exit 0; }; "
    "export DEBIAN_FRONTEND=noninteractive; "
    "apt-get update -qq >/tmp/docker-install.log 2>&1; "
    "apt-get install -y -qq docker.io >>/tmp/docker-install.log 2>&1; "
    "apt-get install -y -qq docker-compose-v2 >/dev/null 2>&1 || apt-get install -y -qq docker-compose >/dev/null 2>&1; "
    "systemctl enable --now docker >/dev/null 2>&1; sleep 3; "
    "if docker info >/dev/null 2>&1; then echo DOCKER_OK; else echo DOCKER_FAILED; "
    "tail -n 8 /tmp/docker-install.log; exit 1; fi"
)

_BG_TASKS: set = set()


def spawn(coro):
    """asyncio.create_task that keeps a reference so the task isn't garbage collected."""
    t = asyncio.create_task(coro)
    _BG_TASKS.add(t)
    t.add_done_callback(_BG_TASKS.discard)
    return t


def install_docker(cl: "Cluster", node: str, vmid: int):
    """Install Docker (+ compose) inside a container. Returns (ok, last output)."""
    rc, out, err = cl.pct_exec(node, vmid, DOCKER_CMD, timeout=420)
    return ("DOCKER_OK" in out), (out + err).strip()[-300:]


async def _docker_background(cl: "Cluster", node: str, vmid: int, host: str):
    try:
        ok, detail = await asyncio.to_thread(install_docker, cl, node, vmid)
        dbg(f"[{host}] docker {'installed' if ok else 'FAILED'}: {detail[-160:]}")
    except Exception as e:  # noqa: BLE001
        dbg(f"[{host}] docker install error: {e}")


def _next_vmid(cl: Cluster, node: str) -> int:
    """Next free container id for this cluster (Proxmox asks the cluster; Incus uses ct<id> names)."""
    if cl.backend() == "incus":
        nid = int(cl.c.get("vmid_start", 100))
        for _ in range(1000):
            taken = db.execute("SELECT 1 FROM cts WHERE cluster=? AND vmid=?", (cl.name, nid)).fetchone()
            if not taken:
                rc = cl.run(node, f"incus info ct{nid} >/dev/null 2>&1", timeout=20)[0]
                if rc != 0:  # no such instance
                    return nid
            nid += 1
        raise RuntimeError(f"No free container id on {cl.name}")
    nid = int(cl.api.cluster.nextid.get())
    for _ in range(200):
        if db.execute("SELECT 1 FROM cts WHERE cluster=? AND vmid=?", (cl.name, nid)).fetchone():
            nid += 1
            continue
        try:
            cl.api.cluster.nextid.get(vmid=nid)
        except Exception:
            nid += 1
            continue
        return nid
    raise RuntimeError("Could not find a free VMID")


def remove_instance(cl: Cluster, node: str, vmid: int):
    """Stop + delete the container itself (not the DB row)."""
    if cl.backend() == "incus":
        rc, out, err = cl.run(node, f"incus delete ct{int(vmid)} --force", timeout=120)
        text = (err + out).lower()
        if rc != 0 and "not found" not in text:
            raise RuntimeError((err or out).strip() or "incus delete failed")
        return
    api = cl.api.nodes(node).lxc(vmid)
    if api.status.current.get()["status"] == "running":
        cl.wait(node, api.status.stop.post())
    cl.wait(node, api.delete(purge=1))


# os_templates entry may carry an Incus image next to the Proxmox template file:
#   ubuntu: {label: "Ubuntu 24.04", file: ubuntu-24.04-...tar.zst, image: "images:ubuntu/24.04"}
IMAGE_BY_FILE = {v.get("file"): v.get("image") for v in OS_TEMPLATES.values() if v.get("image")}


def _parse_size(v) -> int:
    """'2048MiB' / '20GiB' / '512MB' -> bytes (0 if missing/unparseable)."""
    if not v:
        return 0
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([KMGT]?i?B?)\s*", str(v), re.I)
    if not m:
        return 0
    mult = {"": 1, "B": 1, "K": 1024, "KB": 1000, "KIB": 1024, "M": 1024 ** 2, "MB": 1000 ** 2, "MIB": 1024 ** 2,
            "G": 1024 ** 3, "GB": 1000 ** 3, "GIB": 1024 ** 3, "T": 1024 ** 4, "TB": 1000 ** 4, "TIB": 1024 ** 4}
    return int(float(m[1]) * mult.get(m[2].upper(), 1))


def provision_incus(
    cl: Cluster, node: str, vmid: int, host: str, ip: Optional[str],
    ram: int, cpu: int, disk: int, pw: str, template_file: Optional[str] = None,
):
    """Create an Incus system container on a plain VPS and set the root password."""
    c = cl.c
    image = IMAGE_BY_FILE.get(template_file) or c.get("image") or "images:debian/12"
    name = f"ct{int(vmid)}"

    def launch_cmd(quota: bool, iso: bool, ipf: bool) -> str:
        args = [
            "incus", "launch", image, name,
            "-c", f"limits.cpu={int(cpu)}",
            "-c", f"limits.memory={int(ram)}MiB",
            "-c", f"security.nesting={'true' if SEC_NESTING else 'false'}",
            "-c", "boot.autostart=true",
        ]
        if SEC_PIDS_MAX > 0:
            args += ["-c", f"limits.processes={SEC_PIDS_MAX}"]
        if c.get("storage"):
            args += ["-s", str(c["storage"])]
        if quota:
            args += ["-d", f"root,size={int(disk)}GiB"]
        if is_static(cl) and ip:
            args += ["-d", f"eth0,ipv4.address={ip}"]
        if SEC_NET_MBIT > 0:
            args += ["-d", f"eth0,limits.max={SEC_NET_MBIT}Mbit"]
        args += ["-d", "eth0,security.mac_filtering=true"]            # can't fake another MAC
        if ipf and is_static(cl) and ip:
            args += ["-d", "eth0,security.ipv4_filtering=true"]       # can't use another IPv4
        if iso:
            args += ["-d", "eth0,security.port_isolation=true"]       # can't talk to other containers
        return "timeout 840 " + " ".join(shlex.quote(a) for a in args) + " </dev/null"

    # Start with every protection on; if the host can't do one of them (plain 'dir' storage can't
    # enforce a disk size, an old kernel can't isolate ports...) drop just that one and retry.
    feats = {"quota": bool(c.get("disk_quota", True)), "iso": SEC_PORT_ISOLATION, "ipf": True}
    for attempt in range(4):
        dbg(f"[{name}] launching {image} ({int(ram)}MiB, {int(cpu)} cpu, {int(disk)}GiB, ip={ip}, {feats})")
        rc, out, err = cl.run(node, launch_cmd(**feats), timeout=900)
        dbg(f"[{name}] launch finished rc={rc}")
        if rc in (0, 124):
            break
        text = (err + out).lower()
        changed = False
        if feats["quota"] and "quota" in text:
            feats["quota"], changed = False, True
        if feats["iso"] and "isolat" in text:
            feats["iso"], changed = False, True
        if feats["ipf"] and ("filtering" in text or "ipv4" in text):
            feats["ipf"], changed = False, True
        if not changed:
            break
        dbg(f"[{name}] retrying with {feats}")
        cl.run(node, f"incus delete {name} --force >/dev/null 2>&1", timeout=60)
    if rc == 124:
        cl.run(node, f"incus delete {name} --force >/dev/null 2>&1", timeout=60)
        raise RuntimeError("incus launch timed out after 14 min (image download stalled?). Pre-cache the image on the VPS: incus image copy " + image + " local:")
    if rc != 0:
        raise RuntimeError((err or out).strip()[-600:] or "incus launch failed")

    dbg(f"[{name}] waiting for the container to accept commands")
    wait_ready(cl, node, vmid)
    dbg(f"[{name}] ready, setting root password")
    rc, _, err = cl.pct_exec(node, vmid, f"echo root:{pw} | chpasswd")
    if rc != 0:
        raise RuntimeError(err.strip() or "Could not set the root password")
    dbg(f"[{name}] provisioned")
    db.execute("UPDATE cts SET state='ready' WHERE cluster=? AND vmid=?", (cl.name, vmid))
    db.commit()


def incus_live(cl: Cluster, node: str, vmid: int):
    """Live stats for an Incus container, shaped like Proxmox's status.current."""
    name = f"ct{int(vmid)}"
    rc, out, _ = cl.run(node, f"incus list {shlex.quote('^' + name + '$')} --format json", timeout=30)
    if rc != 0:
        return None
    items = [i for i in json.loads(out or "[]") if i.get("name") == name]
    if not items:
        return None
    it = items[0]
    running = str(it.get("status", "")).lower() == "running"
    info = {"status": "running" if running else "stopped"}
    if not running:
        return info
    st = it.get("state") or {}
    cfg_ = it.get("expanded_config") or {}
    dev = (it.get("expanded_devices") or {}).get("root", {})
    mem = int((st.get("memory") or {}).get("usage") or 0)
    disk = int(((st.get("disk") or {}).get("root") or {}).get("usage") or 0)
    maxmem = _parse_size(cfg_.get("limits.memory")) or max(mem, 1)
    maxdisk = _parse_size(dev.get("size")) or max(disk, 1024 ** 3)
    vcpus = max(int(cfg_.get("limits.cpu") or 1), 1)
    load, uptime = 0.0, 0
    try:
        rc2, out2, _ = cl.pct_exec(node, vmid, "cut -d' ' -f1 /proc/loadavg; cut -d' ' -f1 /proc/uptime", timeout=15)
        vals = out2.strip().splitlines()
        if rc2 == 0 and len(vals) >= 2:
            load, uptime = float(vals[0]), int(float(vals[1]))
    except Exception:
        pass
    info.update({
        "cpu": max(0.0, min(1.0, load / vcpus)),  # load average / vCPUs (an approximation)
        "mem": mem, "maxmem": maxmem, "disk": disk, "maxdisk": maxdisk, "uptime": uptime,
    })
    return info


def reserve(cl: Cluster, node: str, owner: int, ram: int, cpu: int, disk: int):
    """Pick VMID (+ private IP if this cluster uses static IPs) + SSH port; reserve them in the DB."""
    with RES_LOCK:
        nid = _next_vmid(cl, node)
        ip = allocate_ip(cl) if is_static(cl) else None  # DHCP clusters: IP is discovered after boot
        port = allocate_port(cl) if cl.ssh_mode() != "tmate" else None  # tmate clusters need no public port
        host = f"vps{nid}-{owner}"
        db.execute(
            "INSERT INTO cts (vmid,cluster,node,owner,hostname,ip,ram,cpu,disk,created,ssh_port,state) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,'creating')",
            (nid, cl.name, node, owner, host, ip, ram, cpu, disk, time.time(), port),
        )
        db.commit()
    return nid, ip, port, host


def provision(
    cl: Cluster, node: str, vmid: int, host: str, ip: Optional[str],
    ram: int, cpu: int, disk: int, pw: str, template_file: Optional[str] = None,
):
    if cl.backend() == "incus":
        return provision_incus(cl, node, vmid, host, ip, ram, cpu, disk, pw, template_file)
    c = cl.c
    file = template_file or c.get("template")
    if not file:
        raise RuntimeError(f"No OS template specified for cluster '{cl.name}' (set os_templates or a cluster-level template)")
    if is_static(cl):
        prefix = ipaddress.ip_network(c["subnet"]).prefixlen
        net_opts = f"ip={ip}/{prefix},gw={c['gateway']}"
    else:
        net_opts = "ip=dhcp"
    if SEC_NET_MBIT > 0:
        net_opts += f",rate={max(1, -(-SEC_NET_MBIT // 8))}"  # Proxmox 'rate' is in MB/s
    kw = dict(
        vmid=vmid,
        hostname=host,
        ostemplate=f"{c.get('template_storage', 'local')}:vztmpl/{file}",
        password=pw,
        cores=cpu,
        memory=ram,
        swap=0,
        rootfs=f"{c['storage']}:{disk}",
        net0=f"name=eth0,bridge={c['bridge']},{net_opts}",
        nameserver=c.get("dns", "1.1.1.1"),
        unprivileged=1,
        onboot=1,
        start=0,  # started below, after the process cap is written into the config
    )
    if SEC_NESTING:
        kw["features"] = "nesting=1"  # Docker in an unprivileged LXC needs both
    upid = cl.api.nodes(node).lxc.create(**kw)
    cl.wait(node, upid, timeout=600); (cl.run(node, f"sed -i 's/^features:.*/features: keyctl=1,nesting=1/' /etc/pve/lxc/{int(vmid)}.conf", timeout=30) if SEC_DOCKER else None)

    conf = f"/etc/pve/lxc/{int(vmid)}.conf"
    pids_line = f"lxc.cgroup2.pids.max: {SEC_PIDS_MAX}"
    if SEC_PIDS_MAX > 0:
        try:
            cl.run(node, f"grep -q '^lxc.cgroup2.pids.max' {conf} || echo '{pids_line}' >> {conf}", timeout=30)
        except Exception as e:
            dbg(f"[ct{vmid}] could not set the process cap: {e}")
    start = cl.api.nodes(node).lxc(vmid).status.start
    try:
        cl.wait(node, start.post(), timeout=180)
    except Exception:
        if SEC_PIDS_MAX <= 0:
            raise
        # the raw cgroup key may be unsupported on this host: remove it and start without the cap
        dbg(f"[ct{vmid}] start failed with the process cap, retrying without it")
        cl.run(node, f"sed -i '/^lxc.cgroup2.pids.max/d' {conf}", timeout=30)
        cl.wait(node, start.post(), timeout=180)
    db.execute("UPDATE cts SET state='ready' WHERE cluster=? AND vmid=?", (cl.name, vmid))
    db.commit()


def wait_ready(cl: Cluster, node: str, vmid: int, timeout: int = 120):
    end = time.time() + timeout
    while time.time() < end:
        if cl.pct_exec(node, vmid, "true", timeout=20)[0] == 0:
            return
        time.sleep(0.8)
    raise RuntimeError("Container did not become ready in time")


def discover_ip(cl: Cluster, node: str, vmid: int, timeout: int = 90) -> str:
    """Poll inside the container for the DHCP-assigned address on eth0."""
    cmd = "ip -4 -o addr show eth0 scope global | awk '{print $4}' | cut -d/ -f1 | head -n1"
    end = time.time() + timeout
    while time.time() < end:
        rc, out, _ = cl.pct_exec(node, vmid, cmd, timeout=15)
        addr = out.strip()
        if rc == 0 and addr:
            return addr
        time.sleep(0.8)
    raise RuntimeError("Container did not get a DHCP address in time")


def setup_ssh(cl: Cluster, node: str, vmid: int, ip: Optional[str], port: int) -> str:
    """Wait for the container to boot, resolve its IP (DHCP clusters), open the SSH
    forward, and make sure sshd is installed and allows root login. Returns the IP used."""
    wait_ready(cl, node, vmid)
    if ip is None:
        ip = discover_ip(cl, node, vmid)
        db.execute("UPDATE cts SET ip=? WHERE cluster=? AND vmid=?", (ip, cl.name, vmid))
        db.commit()
    cl.run(node, ensure_snippet(ip, port, cl.c.get("public_iface")))
    rc, _, err = cl.pct_exec(node, vmid, BOOT_SSHD, timeout=240)
    if rc != 0:
        raise RuntimeError(err.strip() or "sshd setup failed")
    return ip


# NOTE: the old version used `pkill -f 'tmate -S /tmp/bot.tmate'`. Because this whole script runs as
# `bash -c '<script>'`, that pattern matched the script's own bash process, so it killed itself with
# SIGTERM (exit 143, no output). `tmate kill-server` only touches the tmate server on our socket.
TMATE_CMD = (
    "export HOME=/root TERM=xterm; "
    "if ! command -v tmate >/dev/null 2>&1; then "
    "echo '--apt-update--'; apt-get update -qq 2>&1 | tail -n 8; "
    "echo '--apt-install--'; DEBIAN_FRONTEND=noninteractive apt-get install -y -qq tmate 2>&1 | tail -n 8; "
    "fi; "
    "command -v tmate >/dev/null 2>&1 || { echo 'TMATE_NOT_INSTALLED (no internet/DNS inside the container?)'; exit 1; }; "
    "tmate -S /tmp/bot.tmate kill-server >/dev/null 2>&1; rm -f /tmp/bot.tmate; "
    "echo '--tmate-new-session--'; tmate -S /tmp/bot.tmate new-session -d 2>&1; "
    "echo '--tmate-wait-ready--'; "
    "timeout 60 tmate -S /tmp/bot.tmate wait tmate-ready 2>&1 || { echo 'TMATE_TIMEOUT (cannot reach the tmate relay)'; exit 1; }; "
    "echo '--tmate-display--'; tmate -S /tmp/bot.tmate display -p '#{tmate_ssh}'"
)


SSHX_CMD = (
    "export HOME=/root TERM=xterm; "
    "command -v curl >/dev/null 2>&1 || { apt-get update -qq >/dev/null 2>&1; "
    "DEBIAN_FRONTEND=noninteractive apt-get install -y -qq curl ca-certificates >/dev/null 2>&1; }; "
    "command -v curl >/dev/null 2>&1 || { echo 'CURL_NOT_INSTALLED (no internet/DNS inside the container?)'; exit 1; }; "
    "getent hosts sshx.io >/dev/null 2>&1 || { echo 'DNS_FAIL: cannot resolve sshx.io'; exit 1; }; "
    "pkill -x sshx >/dev/null 2>&1; rm -f /tmp/sshx.log; "
    "setsid nohup sh -c 'curl -sSf https://sshx.io/get | sh -s run' >/tmp/sshx.log 2>&1 </dev/null & "
    "for i in $(seq 1 40); do "
    "url=$(sed 's/\\x1b\\[[0-9;]*m//g' /tmp/sshx.log 2>/dev/null | grep -oE 'https://[^[:space:]]+#[^[:space:]]+' | head -n1); "
    "[ -n \"$url\" ] && { echo \"$url\"; exit 0; }; sleep 1; done; "
    "echo 'SSHX_TIMEOUT'; tail -n 10 /tmp/sshx.log; exit 1"
)


def start_tmate(cl: Cluster, node: str, vmid: int, timeout: int = 150) -> str:
    """Start a temporary relay session inside the container (tmate or sshx, per cluster config)
    and return the connection line: `ssh user@relay` for tmate, an https:// link for sshx.
    Works with no public IP on the Proxmox host."""
    kind = cl.relay_kind()
    script = SSHX_CMD if kind == "sshx" else TMATE_CMD
    prefix = "https://" if kind == "sshx" else "ssh "
    rc, out, err = cl.pct_exec(node, vmid, script, timeout=timeout)
    line = next((ln.strip() for ln in reversed(out.strip().splitlines()) if ln.strip().startswith(prefix)), "")
    if not line:
        detail = ((out.strip() + "\n" + err.strip()).strip() or f"exit code {rc}, no output")[-600:]
        raise RuntimeError(f"{kind} produced no session (exit {rc}):\n{detail}")
    return line


def setup_tmate(cl: Cluster, node: str, vmid: int, ip: Optional[str]) -> tuple[str, str]:
    """Wait for boot, resolve IP if needed, start a fresh tmate session, and cache it. Returns (ip, ssh_line)."""
    wait_ready(cl, node, vmid)
    if ip is None:
        ip = discover_ip(cl, node, vmid)
        db.execute("UPDATE cts SET ip=? WHERE cluster=? AND vmid=?", (ip, cl.name, vmid))
        db.commit()
    line = start_tmate(cl, node, vmid)
    db.execute("UPDATE cts SET tmate=?, tmate_at=? WHERE cluster=? AND vmid=?", (line, time.time(), cl.name, vmid))
    db.commit()
    return ip, line


def enable_ssh(ct: CT) -> CT:
    """For containers created before SSH support existed."""
    cl = CLUSTERS[ct.cluster]
    with RES_LOCK:
        port = allocate_port(cl)
        db.execute("UPDATE cts SET ssh_port=? WHERE cluster=? AND vmid=?", (port, ct.cluster, ct.vmid))
        db.commit()
    setup_ssh(cl, ct.node, ct.vmid, ct.ip, port)
    return get_ct(ct.cluster, ct.vmid)


def set_password(ct: CT, pw: str):
    rc, _, err = CLUSTERS[ct.cluster].pct_exec(ct.node, ct.vmid, f"echo root:{pw} | chpasswd")
    if rc != 0:
        raise RuntimeError(err.strip() or "Could not set password (is the container running?)")


def add_key(ct: CT, key: str):
    k = shlex.quote(key)
    inner = (
        "mkdir -p /root/.ssh && chmod 700 /root/.ssh && touch /root/.ssh/authorized_keys && "
        f"(grep -qxF {k} /root/.ssh/authorized_keys || echo {k} >> /root/.ssh/authorized_keys) && "
        "chmod 600 /root/.ssh/authorized_keys"
    )
    rc, _, err = CLUSTERS[ct.cluster].pct_exec(ct.node, ct.vmid, inner)
    if rc != 0:
        raise RuntimeError(err.strip() or "Could not add key (is the container running?)")


def live(cl: Optional[Cluster], node: str, vmid: int):
    if cl is None:
        return None
    try:
        if cl.backend() == "incus":
            return incus_live(cl, node, vmid)
        return cl.api.nodes(node).lxc(vmid).status.current.get()
    except Exception:
        return None


def power(cl: Cluster, node: str, vmid: int, action: str):
    if cl.backend() == "incus":
        name = f"ct{int(vmid)}"
        cmd = {
            "start": f"incus start {name}",
            "stop": f"incus stop {name} --timeout 30 || incus stop {name} --force",
            "reboot": f"incus restart {name}",
        }[action]
        rc, out, err = cl.run(node, cmd, timeout=120)
        if rc != 0:
            raise RuntimeError((err or out).strip() or f"incus {action} failed")
        return
    st = cl.api.nodes(node).lxc(vmid).status
    upid = {
        "start": lambda: st.start.post(),
        "stop": lambda: st.shutdown.post(forceStop=1, timeout=30),
        "reboot": lambda: st.reboot.post(),
    }[action]()
    cl.wait(node, upid)


def destroy(ct: CT):
    cl = CLUSTERS[ct.cluster]
    if ct.ssh_port and ct.ip:
        try:
            cl.run(ct.node, remove_snippet(ct.ip, ct.ssh_port, cl.c.get("public_iface")))
        except Exception as e:
            print(f"[destroy] could not remove forward: {e}")
    remove_instance(cl, ct.node, ct.vmid)
    db.execute("DELETE FROM cts WHERE cluster=? AND vmid=?", (ct.cluster, ct.vmid))
    clear_extra_ports(ct); db.execute("DELETE FROM ct_shares WHERE cluster=? AND vmid=?", (ct.cluster, ct.vmid))
    db.commit()


def reinstall_provision(cl: Cluster, ct: CT, template_file: str, pw: str, on_step=None):
    """Wipe an existing container and recreate it with the same vmid/hostname/specs
    but a fresh OS. The private IP is re-discovered for DHCP clusters (it may change);
    static-IP clusters keep the same address. The SSH port reservation is untouched."""
    if ct.ssh_port and ct.ip:
        try:
            cl.run(ct.node, remove_snippet(ct.ip, ct.ssh_port, cl.c.get("public_iface")))
        except Exception as e:
            print(f"[reinstall] could not remove old forward: {e}")
    remove_instance(cl, ct.node, ct.vmid)
    if on_step:
        on_step(1)

    keep_ip = ct.ip if is_static(cl) else None
    db.execute(
        "UPDATE cts SET ip=?, tmate=NULL, tmate_at=NULL, state='creating' WHERE cluster=? AND vmid=?",
        (keep_ip, cl.name, ct.vmid),
    )
    db.commit()
    try:
        provision(cl, ct.node, ct.vmid, ct.hostname, keep_ip, ct.ram, ct.cpu, ct.disk, pw, template_file)
    except Exception:
        # Container is gone and recreation failed: mark it ready anyway so it isn't
        # permanently hidden from `manage`, even though it no longer actually exists.
        db.execute("UPDATE cts SET state='ready' WHERE cluster=? AND vmid=?", (cl.name, ct.vmid))
        db.commit()
        raise


# ================================================================ helpers
def parse_ram(s: str) -> Optional[int]:
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(mb|m|gb|g)?", s.lower())
    if not m:
        return None
    val, unit = float(m[1]), m[2]
    if unit is None:
        unit = "g" if val < 128 else "m"
    return int(val * 1024) if unit in ("g", "gb") else int(val)


def parse_disk(s: str) -> Optional[int]:
    m = re.fullmatch(r"(\d+)\s*(gb|g)?", s.lower())
    return int(m[1]) if m else None


def fmt_ram(mb: int) -> str:
    return f"{mb / 1024:g} GB" if mb >= 1024 else f"{mb} MB"


def fmt_uptime(sec: int) -> str:
    d, r = divmod(int(sec), 86400)
    h, r = divmod(r, 3600)
    return f"{d}d {h}h {r // 60}m" if d else f"{h}h {r // 60}m"


def bar(frac: float, n: int = 12) -> str:
    frac = max(0.0, min(1.0, frac))
    f = round(frac * n)
    return "▰" * f + "▱" * (n - f)


def err_text(e, n: int = 1500) -> str:
    """Make an exception safe to drop inside a ``` code block."""
    return str(e).replace("```", "'''")[:n]


def ssh_host_port(ct: CT):
    return CLUSTERS[ct.cluster].public_host(ct.node), ct.ssh_port


def relay_block(line: Optional[str], empty: str = "— none —") -> str:
    """tmate `ssh ...` line -> code block; sshx https link -> plain clickable link."""
    if not line:
        return f"```bash\n{empty}\n```"
    if line.startswith("https://"):
        return f"🔗 {line}"
    return f"```bash\n{line}\n```"


KEY_RE = re.compile(
    r"^(ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp(?:256|384|521)|sk-ssh-ed25519@openssh\.com) "
    r"[A-Za-z0-9+/=]+(?: [\w@.\-]+)?$"
)

# <@123>, <@!123> or a bare 15-20 digit Discord ID (handy in DMs where you can't @mention).
USER_TOKEN_RE = re.compile(r"<@!?(\d{15,20})>|(?<![\d<@!])(\d{15,20})(?!\d)")


# ============================================================ UI building
def card(title: str, body: str, colour=BLUE, thumb: Optional[str] = None, link: Optional[tuple[str, str]] = None) -> ui.LayoutView:
    view = ui.LayoutView()
    box = ui.Container(accent_colour=colour)
    head = ui.TextDisplay(f"## {title}")
    if thumb:
        box.add_item(ui.Section(head, ui.TextDisplay(body), accessory=ui.Thumbnail(thumb)))
    else:
        box.add_item(head)
        box.add_item(ui.Separator())
        box.add_item(ui.TextDisplay(body))
    if link:
        url, label = link
        row = ui.ActionRow()
        row.add_item(ui.Button(label=label, style=discord.ButtonStyle.link, url=url, emoji="🌐"))
        box.add_item(row)
    box.add_item(ui.TextDisplay(f"-# {BRAND}"))
    view.add_item(box)
    return view


def progress_card(steps: list[str], idx: int, target: str) -> ui.LayoutView:
    lines = []
    for n, s in enumerate(steps):
        if n < idx:
            lines.append(f"✅  {s}")
        elif n == idx:
            lines.append(f"⏳  **{s}…**")
        else:
            lines.append(f"▫️  {s}")
    return card("🚀 Deploying container", f"Provisioning for {target}\n\n" + "\n".join(lines), GOLD)


# Components V2 has no native animation, so "loading" = one message edited every ~1.7 s:
# spinner frame, a progress bar that creeps forward, rotating suspense text and an elapsed timer.
SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
DEPLOY_FLAVOR = [
    ["Scanning nodes for free capacity", "Checking who has room to spare", "Picking the best node"],
    ["Reserving an ID, an address and a slot", "Claiming resources", "Marking out your territory"],
    ["Carving out CPU and RAM", "Unpacking a fresh OS", "Building your container", "Laying down the filesystem"],
    ["Wiring up network access", "Opening a door to your container", "Final touches"],
]
REINSTALL_FLAVOR = [
    ["Wiping the old container", "Sweeping the floors", "Clearing everything out"],
    ["Unpacking a fresh OS", "Installing the new system", "Laying down the filesystem"],
    ["Wiring up network access", "Opening a door to your container", "Final touches"],
]


def loading_card(title: str, text: str = "One moment…", frame: int = 0) -> ui.LayoutView:
    return card(f"{SPINNER[frame % len(SPINNER)]} {title}", text, GOLD)


class Loader:
    """Animated multi-step loading card. start() -> set(step) as work progresses -> `await stop()`
    BEFORE posting the final card (stop waits for any in-flight edit, so it can't overwrite it)."""

    def __init__(self, msg, title, steps, target, expect=None, flavor=None, icon="🚀", interval=1.7):
        self.msg, self.title, self.steps, self.target = msg, title, steps, target
        self.expect = expect or [5] * len(steps)
        self.flavor = flavor or [["Working on it"]] * len(steps)
        self.icon, self.interval = icon, interval
        self.t0 = self.step_t0 = time.time()
        self.idx = self.tick = 0
        self.colour = GOLD
        self._wake = asyncio.Event()
        self._stop = False
        self._task = None
        self._loop = asyncio.get_running_loop()

    def start(self):
        self._task = asyncio.create_task(self._run())

    def set(self, idx: int):
        self.idx, self.step_t0 = idx, time.time()
        self._wake.set()

    def set_threadsafe(self, idx: int):  # for callbacks coming from worker threads
        self._loop.call_soon_threadsafe(self.set, idx)

    def elapsed(self) -> float:
        return time.time() - self.t0

    def _view(self) -> ui.LayoutView:
        now = time.time()
        i = min(self.idx, len(self.steps) - 1)
        creep = min(0.92, (now - self.step_t0) / max(self.expect[min(i, len(self.expect) - 1)], 1))
        frac = min(0.98, (self.idx + creep) / len(self.steps))
        frame = SPINNER[self.tick % len(SPINNER)]
        lines = []
        for n, st in enumerate(self.steps):
            if n < self.idx:
                lines.append(f"✅  {st}")
            elif n == self.idx:
                lines.append(f"{frame}  **{st}…**")
            else:
                lines.append(f"▫️  {st}")
        fl = self.flavor[min(i, len(self.flavor) - 1)]
        text = fl[(self.tick // 2) % len(fl)]
        body = (
            f"{self.target}\n\n" + "\n".join(lines)
            + f"\n\n`{bar(frac, 16)}` **{int(frac * 100)}%**\n-# {text}… · {int(now - self.t0)}s"
        )
        return card(f"{self.icon} {self.title}", body, self.colour)

    async def _run(self):
        while not self._stop:
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self.interval)
            except asyncio.TimeoutError:
                pass
            self._wake.clear()
            if self._stop:
                break
            self.tick += 1
            try:
                await self.msg.edit(view=self._view())
            except discord.HTTPException:
                pass

    async def stop(self):
        self._stop = True
        self._wake.set()
        if self._task is not None:
            try:
                await self._task
            except Exception:
                pass
            self._task = None


def ssh_card(ct: CT) -> ui.LayoutView:
    host, port = ssh_host_port(ct)
    body = (
        f"```bash\nssh root@{host} -p {port}\n```\n"
        f"**Host** `{host}` · **Port** `{port}` · **User** `root`\n\n"
        f"**Optional `~/.ssh/config`**\n```\nHost {ct.hostname}\n  HostName {host}\n  Port {port}\n  User root\n```\n"
        "Log in with your password, or add a public key using **Add SSH key**."
    )
    return card("🔐 SSH access", body, GREEN)


def tmate_card(ct: CT) -> ui.LayoutView:
    is_web = (ct.tmate or "").startswith("https://")
    body = (
        f"{relay_block(ct.tmate)}\n"
        + ("Tap the button below to open a terminal in your browser.\n" if is_web else "")
        + "Works with no public IPv4 on the Proxmox host — this connects through a temporary relay.\n"
        "-# Anyone with this link/command gets a root shell, so don't share it publicly. "
        "It ends when the container restarts. Press **SSH** again for a fresh one (the old one stops working)."
    )
    link = (ct.tmate, "Open Web Terminal") if is_web and ct.tmate else None
    return card("🔐 Temporary access", body, GREEN, link=link)


# ============================================================= manage UI
class KeyModal(ui.Modal, title="Add SSH public key"):
    key = ui.TextInput(
        label="Public key",
        style=discord.TextStyle.paragraph,
        placeholder="ssh-ed25519 AAAA… you@laptop",
        max_length=800,
    )

    def __init__(self, ct: CT):
        super().__init__()
        self.ct = ct

    async def on_submit(self, i: discord.Interaction):
        k = " ".join(str(self.key).split())
        if not KEY_RE.match(k):
            return await i.response.send_message(
                view=card("Invalid key", "Paste a **public** key (`ssh-ed25519`, `ssh-rsa` or `ecdsa`).", RED),
                ephemeral=True,
            )
        await i.response.defer(ephemeral=True)
        try:
            await asyncio.to_thread(add_key, self.ct, k)
            where = f"port `{self.ct.ssh_port}`" if self.ct.ssh_port else "the relay session"
            v = card("🔑 Key added", f"You can now log in with that key ({where}).", GREEN)
        except Exception as e:
            v = card("Could not add key", f"```{err_text(e)}```", RED)
        await i.followup.send(view=v, ephemeral=True)


class ManageView(ui.LayoutView):
    def __init__(self, viewer_id: int, owner: discord.abc.User):
        super().__init__(timeout=300)
        self.viewer_id = viewer_id
        self.owner = owner
        self.selected: Optional[tuple[str, int]] = None
        self.confirming = False
        self.note: Optional[str] = None
        self.message: Optional[discord.Message] = None
        self._cts: list[CT] = []
        self.busy: Optional[str] = None            # e.g. "Restarting container" -> shows a loading line
        self._info_cache: dict = {}                # last live stats, reused while busy so the UI reacts instantly

    async def interaction_check(self, i: discord.Interaction) -> bool:
        if i.user.id != self.viewer_id:
            await i.response.send_message("This panel belongs to someone else. Type `manage` for your own.", ephemeral=True)
            return False
        return True

    async def on_timeout(self):
        self.clear_items()
        self.add_item(
            ui.Container(
                ui.TextDisplay("### ⌛ Session expired\nType `manage` to open a fresh panel."),
                accent_colour=GREY,
            )
        )
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass

    def current(self) -> CT:
        return next(c for c in self._cts if (c.cluster, c.vmid) == self.selected)

    @staticmethod
    def _btn(label, style, cb, disabled=False, emoji=None):
        b = ui.Button(label=label, style=style, disabled=disabled, emoji=emoji)
        b.callback = cb
        return b

    async def render(self, fresh: bool = True):
        self.clear_items()
        self._cts = await asyncio.to_thread(visible_cts, self.owner.id, self.viewer_id)
        avatar = self.owner.display_avatar.url

        if not self._cts:
            self.add_item(
                ui.Container(
                    ui.Section(
                        ui.TextDisplay(f"## 🖥️ Containers\n{self.owner.mention} has no containers yet.\n-# {BRAND}"),
                        accessory=ui.Thumbnail(avatar),
                    ),
                    accent_colour=GREY,
                )
            )
            return

        if self.selected not in {(c.cluster, c.vmid) for c in self._cts}:
            self.selected = (self._cts[0].cluster, self._cts[0].vmid)
        ct = self.current()

        ckey = (ct.cluster, ct.vmid)
        if not fresh and ckey in self._info_cache:
            info = self._info_cache[ckey]
        else:
            info = await asyncio.to_thread(live, CLUSTERS.get(ct.cluster), ct.node, ct.vmid)
            self._info_cache[ckey] = info
        running = bool(info) and info.get("status") == "running"
        if info is None:
            badge, colour = "⚪ Unreachable", GOLD
        elif running:
            badge, colour = "🟢 Running", GREEN
        else:
            badge, colour = "🔴 Stopped", RED
        if ct.suspended:
            badge, colour = "⛔ Suspended", RED

        box = ui.Container(accent_colour=colour)
        box.add_item(
            ui.Section(
                ui.TextDisplay(f"## 🖥️ {ct.hostname}"),
                ui.TextDisplay(f"{badge}  ·  CT `{ct.vmid}`  ·  `{ct.cluster}/{ct.node}`\nOwner {self.owner.mention}"),
                accessory=ui.Thumbnail(avatar),
            )
        )

        sel = ui.Select(
            placeholder=f"Switch container ({len(self._cts)})",
            options=[
                discord.SelectOption(
                    label=f"{c.hostname} · CT {c.vmid}",
                    description=f"{c.cluster} · {c.ip or 'no IP yet'}",
                    value=f"{c.cluster}|{c.vmid}",
                    emoji="🖥️",
                    default=(c.cluster, c.vmid) == self.selected,
                )
                for c in self._cts[:25]
            ],
        )
        sel.callback = self.on_select
        pick = ui.ActionRow()
        pick.add_item(sel)
        box.add_item(pick)
        box.add_item(ui.Separator())

        if running:
            mem, maxmem = info["mem"] / MB, max(info["maxmem"] / MB, 1)
            d, maxd = info.get("disk", 0) / GB, max(info.get("maxdisk", 1) / GB, 0.001)
            res = (
                "### 📊 Resources\n```\n"
                f"CPU   {bar(info['cpu'])}  {info['cpu'] * 100:4.1f}%\n"
                f"RAM   {bar(mem / maxmem)}  {mem:.0f}/{maxmem:.0f} MB\n"
                f"Disk  {bar(d / maxd)}  {d:.1f}/{maxd:.0f} GB\n"
                f"```\n-# Uptime {fmt_uptime(info['uptime'])}"
            )
        else:
            res = (
                "### 📊 Resources\n"
                f"**{fmt_ram(ct.ram)}** RAM · **{ct.cpu}** vCPU · **{ct.disk} GB** disk\n"
                "-# Live stats appear when the container is running."
            )
        box.add_item(ui.TextDisplay(res))

        net = f"### 🌐 Network\n**Private IP** `{ct.ip}`" if ct.ip else "### 🌐 Network\n*IP not resolved yet — press **SSH** to retry.*"
        if CLUSTERS[ct.cluster].ssh_mode() == "tmate":
            if ct.tmate:
                age = fmt_uptime(time.time() - ct.tmate_at) if ct.tmate_at else "?"
                net += f"\n{relay_block(ct.tmate)}\n-# Temporary relay session, started {age} ago · press **SSH** for a fresh one"
            else:
                net += "\nNo public IPv4 on this host — SSH works via a temporary relay. Press **SSH** to start a session."
        elif ct.ssh_port:
            host, port = ssh_host_port(ct)
            net += f"\n```bash\nssh root@{host} -p {port}\n```"
        else:
            net += (f"\nConnect: `{ssh_cmd(ct)}` and enter your token (`{PREFIX}token`)." if GW_ON else "\nSSH is not enabled yet. Press **SSH** to set it up.")
        box.add_item(ui.TextDisplay(net))
        if self.note:
            box.add_item(ui.TextDisplay(self.note))
        box.add_item(ui.Separator())

        if self.busy:
            box.add_item(ui.TextDisplay(f"{SPINNER[0]} **{self.busy}…** this takes a few seconds"))
            box.add_item(ui.TextDisplay(f"-# {BRAND} · only you can use this panel"))
            self.add_item(box)
            return

        can_delete = is_admin(self.viewer_id)  # delete is admin-only — owners can't delete their own containers

        if self.confirming and can_delete:
            box.add_item(ui.TextDisplay("⚠️ **Delete this container permanently?** All data will be lost."))
            row = ui.ActionRow()
            row.add_item(self._btn("Yes, delete", discord.ButtonStyle.danger, self.on_confirm, emoji="🗑️"))
            row.add_item(self._btn("Cancel", discord.ButtonStyle.secondary, self.on_cancel))
            box.add_item(row)
        else:
            power_row = ui.ActionRow()
            power_row.add_item(self._btn("Start", discord.ButtonStyle.success, lambda i: self.act(i, "start"), running, "▶️"))
            power_row.add_item(self._btn("Stop", discord.ButtonStyle.secondary, lambda i: self.act(i, "stop"), not running, "⏹️"))
            power_row.add_item(self._btn("Restart", discord.ButtonStyle.primary, lambda i: self.act(i, "reboot"), not running, "🔄"))
            box.add_item(power_row)
            tool_row = ui.ActionRow()
            tool_row.add_item(self._btn("SSH", discord.ButtonStyle.primary, self.on_ssh, emoji="🔐"))
            tool_row.add_item(self._btn("Reset password", discord.ButtonStyle.secondary, self.on_reset, emoji="🔁"))
            tool_row.add_item(self._btn("Add SSH key", discord.ButtonStyle.secondary, self.on_key, emoji="🔑"))
            if can_delete:
                tool_row.add_item(self._btn("Reinstall", discord.ButtonStyle.secondary, self.on_reinstall, emoji="♻️"))
                tool_row.add_item(self._btn("Delete", discord.ButtonStyle.danger, self.on_delete, emoji="🗑️"))
            box.add_item(tool_row)

        box.add_item(ui.TextDisplay(f"-# {BRAND} · only you can use this panel"))
        self.add_item(box)

    async def _refresh(self, i: discord.Interaction):
        await self.render()
        await i.edit_original_response(view=self)

    # ---- callbacks
    async def on_select(self, i: discord.Interaction):
        cn, vmid = i.data["values"][0].split("|")
        self.selected, self.confirming, self.note = (cn, int(vmid)), False, None
        await i.response.defer()
        await self._refresh(i)

    async def act(self, i: discord.Interaction, action: str):
        if self.current().suspended and action in ("start", "reboot") and not is_admin(self.viewer_id):
            return await i.response.send_message("⛔ This container is suspended by an admin.", ephemeral=True)
        await i.response.defer()
        ct = self.current()
        self.busy = {"start": "Starting", "stop": "Stopping", "reboot": "Restarting"}[action] + " container"
        self.note = None
        await self.render(fresh=False)           # instant: no API call, just shows the loading line
        await i.edit_original_response(view=self)
        try:
            await asyncio.to_thread(power, CLUSTERS[ct.cluster], ct.node, ct.vmid, action)
            self.note = f"✅ `{action}` completed."
        except Exception as e:
            self.note = f"❌ {err_text(e, 300)}"
        finally:
            self.busy = None
        await self._refresh(i)

    async def on_ssh(self, i: discord.Interaction):
        cur = self.current()
        if cur.suspended and not is_admin(self.viewer_id):
            return await i.response.send_message("⛔ This container is suspended by an admin.", ephemeral=True)
        key, now = (i.user.id, cur.cluster, cur.vmid), time.time()
        if now - _SSH_LAST.get(key, 0) < 8:
            return await i.response.send_message("⏳ Easy there - wait a few seconds before asking for access again.", ephemeral=True)
        _SSH_LAST[key] = now
        await i.response.defer(ephemeral=True)
        ct = self.current()
        cl = CLUSTERS[ct.cluster]
        ok = True
        try:
            if cl.ssh_mode() == "tmate":
                await asyncio.to_thread(setup_tmate, cl, ct.node, ct.vmid, ct.ip)
                ct = get_ct(ct.cluster, ct.vmid)
                v = tmate_card(ct)
            else:
                if ct.ssh_port is None:
                    ct = await asyncio.to_thread(enable_ssh, ct)
                else:
                    # make sure the forward + sshd are actually in place (also fixes IP not resolved yet)
                    await asyncio.to_thread(setup_ssh, cl, ct.node, ct.vmid, ct.ip, ct.ssh_port)
                    ct = get_ct(ct.cluster, ct.vmid)
                v = ssh_card(ct)
        except Exception as e:
            v = card("SSH setup failed", f"```{err_text(e)}```", RED)
            ok = False
        await i.followup.send(view=v, ephemeral=True)
        if ok and ct.owner != i.user.id:
            try:
                owner_user = client.get_user(ct.owner) or await client.fetch_user(ct.owner)
                await owner_user.send(view=v)
            except discord.HTTPException:
                pass
        if self.message:
            try:
                await self.render()
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass

    async def on_reset(self, i: discord.Interaction):
        await i.response.defer(ephemeral=True)
        ct, pw = self.current(), gen_password()
        try:
            await asyncio.to_thread(set_password, ct, pw)
            v = card("🔁 Password reset", f"**User** `root`\n**New password** ||{pw}||", GREEN)
        except Exception as e:
            v = card("Reset failed", f"```{err_text(e)}```", RED)
        await i.followup.send(view=v, ephemeral=True)

    async def on_key(self, i: discord.Interaction):
        await i.response.send_modal(KeyModal(self.current()))

    async def on_delete(self, i: discord.Interaction):
        if not is_admin(self.viewer_id):
            return await i.response.send_message("Only an admin can delete this container.", ephemeral=True)
        self.confirming = True
        await i.response.defer()
        await self._refresh(i)

    async def on_reinstall(self, i: discord.Interaction):
        if not (self.viewer_id == self.owner.id or is_admin(self.viewer_id)):
            return await i.response.send_message("Only the owner or an admin can reinstall this container.", ephemeral=True)
        ct = self.current()
        await i.response.send_message(
            f"Run `reinstall {ct.vmid} {ct.cluster}` to pick an OS and reinstall — that walks you through it step by step.",
            ephemeral=True,
        )

    async def on_cancel(self, i: discord.Interaction):
        self.confirming = False
        await i.response.defer()
        await self._refresh(i)

    async def on_confirm(self, i: discord.Interaction):
        if not is_admin(self.viewer_id):
            return await i.response.send_message("Only an admin can delete this container.", ephemeral=True)
        await i.response.defer()
        ct = self.current()
        self.confirming = False
        self.busy = "Deleting container"
        await self.render(fresh=False)
        await i.edit_original_response(view=self)
        try:
            await asyncio.to_thread(destroy, ct)
            self.note = f"🗑️ `{ct.hostname}` deleted."
            self.selected = None
        except Exception as e:
            self.note = f"❌ {err_text(e, 300)}"
        finally:
            self.busy = None
        await self._refresh(i)


# ================================================================== bot
class Bot(discord.Client):
    async def setup_hook(self):
        asyncio.create_task(sync_loop())


intents = discord.Intents.default()  # includes guilds + guild_messages + dm_messages
intents.message_content = True  # enable "Message Content Intent" in the developer portal
intents.members = INVITES_ENABLED  # enable "Server Members Intent" too if invite tracking is on
client = Bot(intents=intents)

CREATE_RE = re.compile(
    r"^create\s+(\S+)\s+(\d+)\s+(\S+)\s+(?:<@!?(\d+)>|(\d{15,20}))(?:\s+(\S+))?\s*$", re.I
)
USAGE = (
    "`create <ram> <cpu> <disk> @user [cluster]`\n"
    "Example: `create 2g 2 20 @user` or `create 2048 2 20 @user mumbai`\n"
    "In DMs use the user's ID instead of @: `create 2g 2 20 123456789012345678`\n"
    "RAM: MB (or `2g` for GB) · CPU: cores · Disk: GB"
)

GUILD_ONLY = {"invites", "leaderboard", "claim-reward"}  # these need a server (invite/boost tracking)


async def reply(m: discord.Message, view: ui.LayoutView) -> discord.Message:
    return await m.reply(view=view, mention_author=False)


async def resolve_user(uid: int, known: Optional[dict] = None) -> Optional[discord.abc.User]:
    if known and uid in known:
        return known[uid]
    u = client.get_user(uid)
    if u:
        return u
    try:
        return await client.fetch_user(uid)
    except discord.HTTPException:
        return None


async def mentioned_users(m: discord.Message) -> list[discord.abc.User]:
    """Users referenced in the message, in order: @mentions and/or raw IDs. Works in DMs."""
    known = {u.id: u for u in m.mentions}
    out, seen = [], set()
    for a, b in USER_TOKEN_RE.findall(m.content):
        uid = int(a or b)
        if uid in seen:
            continue
        seen.add(uid)
        u = await resolve_user(uid, known)
        if u:
            out.append(u)
    return out


class OSChoiceView(ui.LayoutView):
    """A row of buttons letting the requester pick an OS before deploy proceeds."""

    def __init__(self, viewer_id: int, spec_line: str, options: list[tuple[str, str]]):
        super().__init__(timeout=120)
        self.viewer_id = viewer_id
        self.chosen: Optional[str] = None
        box = ui.Container(accent_colour=BLUE)
        box.add_item(ui.TextDisplay(f"## 💿 Choose an OS\n{spec_line}"))
        box.add_item(ui.Separator())
        row = ui.ActionRow()
        for key, label in options[:5]:
            row.add_item(self._btn(key, label))
        box.add_item(row)
        box.add_item(ui.TextDisplay(f"-# {BRAND} · only you can pick"))
        self.add_item(box)

    def _btn(self, key: str, label: str) -> ui.Button:
        b = ui.Button(label=label, style=discord.ButtonStyle.primary)

        async def cb(i: discord.Interaction):
            self.chosen = key
            await i.response.defer()
            self.stop()

        b.callback = cb
        return b

    async def interaction_check(self, i: discord.Interaction) -> bool:
        if i.user.id != self.viewer_id:
            await i.response.send_message("This prompt isn't yours.", ephemeral=True)
            return False
        return True


async def ask_os(m: discord.Message, spec_line: str) -> Optional[tuple[discord.Message, Optional[str]]]:
    """If os_templates is configured, prompt for a choice and block until picked.
    Returns (message, template_file). template_file is None if OS_TEMPLATES is empty (skip picker).
    Returns None entirely if the prompt timed out / nothing was chosen."""
    if not OS_TEMPLATES:
        return await reply(m, progress_card(DEPLOY_STEPS, 0, spec_line)), None
    opts = [(k, v.get("label", k)) for k, v in OS_TEMPLATES.items()]
    view = OSChoiceView(m.author.id, spec_line, opts)
    msg = await reply(m, view)
    timed_out = await view.wait()
    if timed_out or not view.chosen:
        await msg.edit(view=card("Cancelled", "No OS selected in time — run the command again.", GOLD))
        return None
    await msg.edit(view=progress_card(DEPLOY_STEPS, 0, spec_line))
    return msg, OS_TEMPLATES[view.chosen]["file"]


def check_spec_limits(cpu: int, ram: int, disk: int) -> Optional[str]:
    if not (1 <= cpu <= MAX_CPU and 128 <= ram <= MAX_RAM_MB and 1 <= disk <= MAX_DISK_GB):
        return f"CPU 1-{MAX_CPU} · RAM 128 MB-{fmt_ram(MAX_RAM_MB)} · Disk 1-{MAX_DISK_GB} GB"
    return None


DEPLOY_STEPS = ["Selecting best node", "Reserving resources", "Creating container", "Configuring SSH access"]


async def handle_create(m: discord.Message):
    if not is_admin(m.author.id):
        return await reply(m, card("No permission", "Only admins can deploy containers.", RED))

    match = CREATE_RE.match(m.content.strip())
    if not match:
        return await reply(m, card("Usage", USAGE, GOLD))

    ram, cpu, disk = parse_ram(match[1]), int(match[2]), parse_disk(match[3])
    uid = int(match[4] or match[5])
    cname = match[6]
    user = await resolve_user(uid, {u.id: u for u in m.mentions})
    if user is None:
        return await reply(m, card("User not found", f"Couldn't find a Discord user with ID `{uid}`.", RED))

    if ram is None or disk is None:
        return await reply(m, card("Invalid values", USAGE, RED))
    err = check_spec_limits(cpu, ram, disk)
    if err:
        return await reply(m, card("Out of limits", err, RED))
    if cname and cname.lower() not in {n.lower() for n in CLUSTERS}:
        return await reply(m, card("Unknown cluster", "Available: " + ", ".join(f"`{n}`" for n in CLUSTERS), RED))

    owned = db.execute("SELECT COUNT(*) FROM cts WHERE owner=?", (user.id,)).fetchone()[0]
    if owned >= MAX_PER_USER:
        return await reply(m, card("Limit reached", f"{user.mention} already has {owned}/{MAX_PER_USER} containers.", RED))

    picked = await ask_os(m, f"{user.mention}\n**{fmt_ram(ram)}** RAM · **{cpu}** vCPU · **{disk} GB** disk")
    if picked is None:
        return
    status, template_file = picked
    await deploy_container(status, user, m.author, ram, cpu, disk, cname, template_file=template_file)


async def deploy_container(
    status: discord.Message,
    user: discord.abc.User,
    requester: discord.abc.User,
    ram: int,
    cpu: int,
    disk: int,
    cname: Optional[str],
    template_file: Optional[str] = None,
    extra_note: str = "",
) -> bool:
    """Shared deploy flow: picks a node, reserves resources, provisions the CT and DMs credentials.
    Used by both `create` and `claim-reward`. Returns True if the container was created."""
    loader = Loader(status, "Deploying container", DEPLOY_STEPS, user.mention,
                    expect=[2, 3, 30, 25], flavor=DEPLOY_FLAVOR)
    loader.start()
    try:
        notes: list[str] = []
        target = await asyncio.to_thread(pick_target, ram, cname, notes)
        if not target:
            await loader.stop()
            detail = "\n".join(notes) or "No clusters are configured."
            title = "No capacity" if ENFORCE_CAPACITY else "No node available"
            await status.edit(view=card(title, f"Couldn't find an online node to deploy on:\n{detail}", RED))
            return False
        cl, node, _ = target

        loader.set(1)
        vmid, ip, port, host = await asyncio.to_thread(reserve, cl, node, user.id, ram, cpu, disk)
        pw = gen_password()

        loader.set(2)
        try:
            await asyncio.to_thread(provision, cl, node, vmid, host, ip, ram, cpu, disk, pw, template_file)
        except Exception:
            db.execute("DELETE FROM cts WHERE cluster=? AND vmid=?", (cl.name, vmid))
            db.commit()
            raise
    except Exception as e:
        await loader.stop()
        await status.edit(view=card("Deploy failed", f"```{err_text(e)}```", RED))
        return False

    loader.set(3)
    tmate_mode = cl.ssh_mode() == "tmate"
    ssh_note = ""
    tmate_line = None
    try:
        if tmate_mode:
            _, tmate_line = await asyncio.to_thread(setup_tmate, cl, node, vmid, ip)
        else:
            await asyncio.to_thread(setup_ssh, cl, node, vmid, ip, port)
    except Exception as e:
        ssh_note = f"\n⚠️ SSH setup needs attention: `{err_text(e, 180)}`. Press **SSH** in `manage` to retry."

    ct = get_ct(cl.name, vmid)
    ip_line = f"**Private IP** `{ct.ip}`\n\n" if ct.ip else ""
    if tmate_mode:
        # Never show the actual relay link/command in the public channel — it's an
        # unauthenticated root shell. Point them at `manage`/DM instead.
        ssh_block = (
            "### 🔐 SSH access\n"
            "No public IPv4 on this host, so access is via a temporary relay.\n"
            f"**Run `manage` or `{PREFIX}manage` → press SSH** to get it (also sent by DM, never posted here).\n\n"
        )
    else:
        host_pub, _ = ssh_host_port(ct)
        ssh_block = f"### 🔐 SSH\n```bash\nssh root@{host_pub} -p {port}\n```\n"
    await loader.stop()
    took = int(loader.elapsed()); asyncio.create_task(give_vps_role(status, user))
    docker_note = ""
    if SEC_DOCKER:
        docker_note = "\n🐳 Docker is installing in the background - ready in about 1-2 minutes (`docker ps`)."
        spawn(_docker_background(cl, node, vmid, host))
    await status.edit(
        view=card(
            "✅ Container deployed",
            f"**Owner** {user.mention}\n"
            f"**Container** `{host}` · CT `{vmid}`\n"
            f"**Location** `{cl.name}/{node}`\n"
            f"**Specs** {fmt_ram(ram)} RAM · {cpu} vCPU · {disk} GB disk\n"
            f"{ip_line}"
            f"{ssh_block}"
            f"Login details were sent by DM. Use `manage` to control it.{ssh_note}{extra_note}{docker_note}\n-# ⚡ Deployed in {took}s",
            GREEN,
            thumb=user.display_avatar.url,
        )
    )

    if tmate_mode:
        login = tmate_card(ct)
    else:
        host_pub, _ = ssh_host_port(ct)
        login = card(
            "🔑 Your container is ready",
            f"```bash\nssh root@{host_pub} -p {port}\n```\n"
            f"**User** `root`\n**Password** ||{pw}||\n\n"
            "Change the password after first login, or add an SSH key with `manage`.",
            GREEN,
        )
    for dest in (user, requester):
        try:
            await dest.send(view=login)
            if tmate_mode:
                try:
                    await dest.send(view=card("🔑 Root password", f"||{pw}|| (useful once you're in)", GREEN))
                except discord.HTTPException:
                    pass
            break
        except discord.HTTPException:  # Forbidden (DMs closed) or any other send error
            continue
    return True


async def handle_manage(m: discord.Message):
    users = await mentioned_users(m)
    target = users[0] if users else m.author
    if target.id != m.author.id and not is_admin(m.author.id) and not is_shared_with(target.id, m.author.id):
        return await reply(m, card("No permission", "You can only manage your own or shared containers.", RED))
    msg = await reply(m, loading_card("Opening control panel", "Fetching your containers…"))  # instant feedback
    view = ManageView(m.author.id, target)
    try:
        await view.render()
    except Exception as e:
        return await msg.edit(view=card("Could not open the panel", f"```{err_text(e)}```", RED))
    view.message = msg
    await msg.edit(view=view)


async def handle_reinstall(m: discord.Message):
    parts = m.content.strip().split()
    if len(parts) < 2 or not parts[1].isdigit():
        return await reply(
            m, card("Usage", "`reinstall <vmid> [cluster]`\nWipes CT `<vmid>` and installs a fresh OS. Specs, private IP/SSH port and hostname stay the same — only the OS and password change.", GOLD)
        )
    vmid = int(parts[1])
    cname = parts[2] if len(parts) > 2 else None
    rows = [r for r in db.execute("SELECT cluster FROM cts WHERE vmid=? AND state='ready'", (vmid,)).fetchall() if r[0] in CLUSTERS]
    if cname:
        rows = [r for r in rows if r[0].lower() == cname.lower()]
    if not rows:
        return await reply(m, card("Not found", f"No container with CT `{vmid}`.", RED))
    if len(rows) > 1:
        names = ", ".join(f"`{r[0]}`" for r in rows)
        return await reply(m, card("Which cluster?", f"CT `{vmid}` exists on several clusters ({names}). Run `reinstall {vmid} <cluster>`.", GOLD))
    ct = get_ct(rows[0][0], vmid)
    if ct.owner != m.author.id and not is_admin(m.author.id) and not is_shared_with(ct.owner, m.author.id):
        return await reply(m, card("No permission", "You can only reinstall your own or shared containers.", RED))
    cl = CLUSTERS[ct.cluster]

    picked = await ask_os(m, f"CT `{vmid}` · reinstall")
    if picked is None:
        return
    status, template_file = picked
    loader = Loader(status, "Reinstalling container",
                    ["Wiping the old container", "Installing a fresh OS", "Configuring access"],
                    f"CT `{vmid}`", expect=[8, 30, 25], flavor=REINSTALL_FLAVOR, icon="♻️")
    loader.start()

    pw = gen_password()
    try:
        await asyncio.to_thread(reinstall_provision, cl, ct, template_file, pw, loader.set_threadsafe)
    except Exception as e:
        await loader.stop()
        return await status.edit(view=card("Reinstall failed", f"```{err_text(e)}```\nThe container may currently be missing — check `manage` and retry if needed.", RED))

    loader.set(2)
    tmate_mode = cl.ssh_mode() == "tmate"
    ip_for_setup = ct.ip if is_static(cl) else None
    ssh_note = ""
    try:
        if tmate_mode:
            await asyncio.to_thread(setup_tmate, cl, ct.node, ct.vmid, ip_for_setup)
        elif ct.ssh_port is None:
            await asyncio.to_thread(enable_ssh, ct)
        else:
            await asyncio.to_thread(setup_ssh, cl, ct.node, ct.vmid, ip_for_setup, ct.ssh_port)
    except Exception as e:
        ssh_note = f"\n⚠️ SSH setup needs attention: `{err_text(e, 180)}`. Press **SSH** in `manage` to retry."

    new_ct = get_ct(ct.cluster, ct.vmid)
    ip_line = f"**Private IP** `{new_ct.ip}`\n\n" if new_ct.ip else ""
    if tmate_mode:
        ssh_block = (
            "### 🔐 SSH access\n"
            "No public IPv4 on this host, so access is via a temporary relay.\n"
            f"**Run `manage` or `{PREFIX}manage` → press SSH** to get it (also sent by DM, never posted here).\n\n"
        )
    else:
        host_pub, port_pub = ssh_host_port(new_ct)
        ssh_block = f"### 🔐 SSH\n```bash\n{ssh_cmd(new_ct)}\n```\n"

    await loader.stop()
    await status.edit(
        view=card(
            "✅ Reinstalled",
            f"**CT** `{vmid}` (`{ct.hostname}`)\n"
            f"**Location** `{ct.cluster}/{ct.node}`\n"
            f"{ip_line}"
            f"{ssh_block}"
            f"New password sent by DM.{ssh_note}",
            GREEN,
        )
    )
    if SEC_DOCKER:
        spawn(_docker_background(cl, ct.node, ct.vmid, ct.hostname))
    if tmate_mode:
        login = tmate_card(new_ct)
    else:
        host_pub, port_pub = ssh_host_port(new_ct)
        login = card(
            "🔑 Reinstalled — new access",
            f"```bash\n{ssh_cmd(new_ct)}\n```\n**Root password** ||{pw}||\n\nAny previous password/SSH key on this container no longer applies.",
            GREEN,
        )
    dest_list = []
    try:
        owner_user = client.get_user(ct.owner) or await client.fetch_user(ct.owner)
        dest_list.append(owner_user)
    except discord.HTTPException:
        pass
    dest_list.append(m.author)
    for dest in dest_list:
        try:
            await dest.send(view=login)
            if tmate_mode:
                try:
                    await dest.send(view=card("🔑 Root password", f"||{pw}|| (useful once you're in)\n\nAny previous password/SSH key no longer applies.", GREEN))
                except discord.HTTPException:
                    pass
            break
        except discord.HTTPException:
            continue


def cluster_overview():
    def one(cl):
        try:
            return (cl.name, cl.nodes())
        except Exception:
            return (cl.name, None)

    with ThreadPoolExecutor(max_workers=max(len(CLUSTERS), 1)) as ex:
        return list(ex.map(one, CLUSTERS.values()))


async def handle_status(m: discord.Message):
    if not is_admin(m.author.id):
        return await reply(m, card("No permission", "Admins only.", RED))
    msg = await reply(m, loading_card("Checking infrastructure", "Querying every node…"))
    data = await asyncio.to_thread(cluster_overview)
    box = ui.Container(accent_colour=BLUE)
    box.add_item(ui.TextDisplay("## 📡 Infrastructure status"))
    for cname, nodes in data:
        box.add_item(ui.Separator())
        if nodes is None:
            box.add_item(ui.TextDisplay(f"### 🌐 {cname}\n🔴 API unreachable"))
            continue
        lines = [f"### 🌐 {cname}"]
        for n in sorted(nodes, key=lambda x: x["node"]):
            cts = db.execute("SELECT COUNT(*) FROM cts WHERE cluster=? AND node=?", (cname, n["node"])).fetchone()[0]
            if n.get("status") != "online":
                lines.append(f"🔴 **{n['node']}** offline")
                continue
            lines.append(
                f"🟢 **{n['node']}** · CPU `{n['cpu'] * 100:.0f}%` · "
                f"RAM `{bar(n['mem'] / n['maxmem'], 8)}` {n['mem'] / GB:.1f}/{n['maxmem'] / GB:.0f} GB · {cts} CTs"
            )
        box.add_item(ui.TextDisplay("\n".join(lines)))
    box.add_item(ui.TextDisplay(f"-# {BRAND}"))
    view = ui.LayoutView()
    view.add_item(box)
    await msg.edit(view=view)


async def handle_ct_list(m: discord.Message):
    if not is_admin(m.author.id):
        return await reply(m, card("No permission", "Admins only.", RED))
    parts = m.content.strip().split()
    cname = parts[1] if len(parts) > 1 else None
    if cname and cname.lower() not in {n.lower() for n in CLUSTERS}:
        return await reply(m, card("Unknown cluster", "Available: " + ", ".join(f"`{n}`" for n in CLUSTERS), RED))

    frag, names = _known_sql()
    sql = f"SELECT vmid,cluster,node,owner,hostname,ram,cpu,disk FROM cts WHERE state='ready' AND {frag}"
    args: tuple = tuple(names)
    if cname:
        sql += " AND cluster=?"
        args = (*names, cname)
    sql += " ORDER BY cluster, vmid"
    rows = db.execute(sql, args).fetchall()

    if not rows:
        suffix = f" on `{cname}`" if cname else ""
        return await reply(m, card("📋 Containers", f"No containers found{suffix}.", GOLD))

    by_cluster: dict[str, list] = {}
    for r in rows:
        by_cluster.setdefault(r[1], []).append(r)

    box = ui.Container(accent_colour=BLUE)
    box.add_item(ui.TextDisplay(f"## 📋 Containers ({len(rows)})"))
    for cn, items in by_cluster.items():
        box.add_item(ui.Separator())
        lines = [f"### 🌐 {cn} ({len(items)})"]
        for vmid, cluster, node, owner, hostname, ram, cpu, disk in items[:30]:
            lines.append(f"`{vmid}` **{hostname}** · <@{owner}> · {fmt_ram(ram)} · {cpu} vCPU · {disk} GB · `{node}`")
        if len(items) > 30:
            lines.append(f"-# +{len(items) - 30} more on this cluster")
        box.add_item(ui.TextDisplay("\n".join(lines)))
    box.add_item(ui.TextDisplay(f"-# {BRAND} · `ct-list <cluster>` to filter"))
    view = ui.LayoutView()
    view.add_item(box)
    await reply(m, view)


async def handle_help(m: discord.Message):
    body = (
        "**`create <ram> <cpu> <disk> @user [cluster]`**\nDeploy a container with a private IP and SSH access. *(admin)*\n\n"
        "**`manage [@user]`**\nOpen the control panel: power, SSH info, password reset, SSH keys, delete.\n\n"
        "**`reinstall <vmid> [cluster]`**\nWipe a container and install a fresh OS. Specs, IP/port and hostname stay the same.\n\n"
        "**`share-user @user…`** · **`unshare-user @user…`**\nLet others manage your containers via `manage @you`.\n\n"
        "**`status`**\nCluster and node overview. *(admin)*\n\n" "**`assign-ports @user|vmid <n>`**\nGive a container extra public ports on the shared IPv4. *(admin)*\n\n" "**`port forward <vm port> [tcp|udp|both] [vmid]`** · **`port list`** · **`port remove <public port>`**\nPoint an assigned public port at a port inside your container.\n\n"
        "**`ct-list [cluster]`**\nList every container across all clusters (or one), with owner and specs. *(admin)*\n\n"
        "**`suspend <vmid> [cluster] [reason]`** · **`unsuspend <vmid> [cluster]`**\nStop an abusive container and stop its owner from starting it. *(admin)*\n\n"
        "**`admin-add @user`** · **`admin-remove @user`** · **`admins`**\nManage who can use admin commands. *(admin)*\n\n"
        "**`invites [@user]`** · **`leaderboard`**\nCheck tracked invites and see the top inviters. *(server only)*\n\n"
        "**`claim-reward`**\nClaim a free container once you've invited enough members or boosted enough. *(server only)*\n\n"
        "**`boost-set @user <count>`**\nSet a member's boost count for boost reward tiers. *(admin)*\n\n"
        f"-# RAM: MB or `2g` · Disk: GB · Works in DMs too (use a user ID instead of @) · `{PREFIX}` prefix is optional"
    )
    await reply(m, card("📖 Commands", body, BLUE))


async def handle_share(m: discord.Message):
    users = await mentioned_users(m)
    if not users:
        return await reply(m, card("Usage", "`share-user @user1 [@user2 ...]`\nGives them `manage @you` access to all your current containers.", GOLD))
    owned = db.execute("SELECT COUNT(*) FROM cts WHERE owner=? AND state='ready'", (m.author.id,)).fetchone()[0]
    if owned == 0:
        return await reply(m, card("Nothing to share", "You don't have any containers yet.", GOLD))
    lines = []
    for u in users:
        if u.id == m.author.id:
            continue
        await asyncio.to_thread(share_containers, m.author.id, u.id)
        lines.append(f"• {u.mention} — access to **{owned}** container(s)")
    body = "\n".join(lines) if lines else "Nothing to do."
    body += f"\n\n-# They run `manage @{m.author.display_name}` to control them. New containers you create later aren't shared automatically — run `share-user` again."
    await reply(m, card("🤝 Shared", body, GREEN))


async def handle_unshare(m: discord.Message):
    users = await mentioned_users(m)
    if not users:
        return await reply(m, card("Usage", "`unshare-user @user1 [@user2 ...]`", GOLD))
    lines = []
    for u in users:
        n = await asyncio.to_thread(unshare_containers, m.author.id, u.id)
        lines.append(f"• {u.mention} — access removed" if n else f"• {u.mention} — wasn't shared with them")
    await reply(m, card("🤝 Access updated", "\n".join(lines), BLUE))


async def handle_admin_add(m: discord.Message):
    if not is_admin(m.author.id):
        return await reply(m, card("No permission", "Only admins can add other admins.", RED))
    users = await mentioned_users(m)
    if not users:
        return await reply(m, card("Usage", "`admin-add @user`", GOLD))
    user = users[0]
    if not add_admin(user.id, m.author.id):
        return await reply(m, card("Already an admin", f"{user.mention} already has admin access.", GOLD))
    await reply(m, card("✅ Admin added", f"{user.mention} can now use admin commands (`create`, `status`, `admin-add`, `admin-remove`).", GREEN))


async def handle_admin_remove(m: discord.Message):
    if not is_admin(m.author.id):
        return await reply(m, card("No permission", "Only admins can remove other admins.", RED))
    users = await mentioned_users(m)
    if not users:
        return await reply(m, card("Usage", "`admin-remove @user`", GOLD))
    user = users[0]
    result = remove_admin(user.id)
    if result == "root":
        return await reply(m, card("Can't remove", f"{user.mention} is a root admin set in `config.yaml` and can't be removed here.", RED))
    if result == "not_admin":
        return await reply(m, card("Not an admin", f"{user.mention} doesn't have admin access.", GOLD))
    await reply(m, card("✅ Admin removed", f"{user.mention} no longer has admin access.", GREEN))


async def handle_admins_list(m: discord.Message):
    if not is_admin(m.author.id):
        return await reply(m, card("No permission", "Admins only.", RED))
    root = "\n".join(f"• <@{u}> `root`" for u in ROOT_ADMINS) or "-# none configured"
    added = "\n".join(f"• <@{u}>" for u in bot_admins()) or "-# none"
    body = f"### From config.yaml\n{root}\n\n### Added via `admin-add`\n{added}"
    await reply(m, card("👑 Admins", body, BLUE))


def tier_line(tier: dict, done: bool, reached: bool) -> str:
    mark = "✅" if done else ("🟢" if reached else "▫️")
    return f"{mark} **{tier['invites']} invites** → {tier['ram_gb']} GB RAM · {tier['cpu']} vCPU · {tier['disk_gb']} GB disk"


def boost_tier_line(tier: dict, done: bool, reached: bool) -> str:
    mark = "✅" if done else ("🟢" if reached else "▫️")
    return f"{mark} **{tier['boosts']} boosts** → {tier['ram_gb']} GB RAM · {tier['cpu']} vCPU · {tier['disk_gb']} GB disk"


async def handle_invites(m: discord.Message):
    if not INVITES_ENABLED:
        return await reply(m, card("Not enabled", "Invite tracking isn't turned on for this server.", GOLD))
    users = await mentioned_users(m)
    target = users[0] if users else m.author
    count = invite_count(m.guild.id, target.id)
    body = f"{target.mention} has invited **{count}** {'member' if count == 1 else 'members'} who joined this server."
    if REWARD_TIERS:
        done = claimed_tiers(target.id)
        body += "\n\n### 🎁 Reward tiers\n" + "\n".join(
            tier_line(t, t["invites"] in done, count >= t["invites"]) for t in REWARD_TIERS
        )
        if target.id == m.author.id:
            body += "\n\nRun `claim-reward` once you qualify for an unclaimed tier."
    await reply(m, card("📨 Invites", body, BLUE, thumb=target.display_avatar.url))


async def handle_leaderboard(m: discord.Message):
    if not INVITES_ENABLED:
        return await reply(m, card("Not enabled", "Invite tracking isn't turned on for this server.", GOLD))
    rows = await asyncio.to_thread(invite_leaderboard, m.guild.id, 10)
    if not rows:
        return await reply(m, card("🏆 Invite leaderboard", "No tracked invites yet.", BLUE))
    medals = {0: "🥇", 1: "🥈", 2: "🥉"}
    lines = [f"{medals.get(i, f'`#{i+1}`')} <@{uid}> — **{c}**" for i, (uid, c) in enumerate(rows)]
    await reply(m, card("🏆 Invite leaderboard", "\n".join(lines), GOLD))


async def handle_claim_reward(m: discord.Message):
    if not (REWARD_TIERS or BOOST_TIERS):
        return await reply(m, card("Not enabled", "No reward tiers are configured for this server.", GOLD))
    owned = db.execute("SELECT COUNT(*) FROM cts WHERE owner=?", (m.author.id,)).fetchone()[0]
    if owned >= MAX_PER_USER:
        return await reply(m, card("Limit reached", f"You already have {owned}/{MAX_PER_USER} containers.", RED))

    kind, tier = (None, None)
    if INVITES_ENABLED and REWARD_TIERS:
        kind, tier = await asyncio.to_thread(best_claimable_tier, m.guild.id, m.author.id)
    elif BOOST_TIERS:  # invites off but boosts configured
        b_count, b_done = get_boosts(m.author.id), claimed_boost_tiers(m.author.id)
        for t in BOOST_TIERS:
            if t["boosts"] not in b_done and b_count >= t["boosts"]:
                kind, tier = "boost", t

    if not tier:
        parts = []
        if INVITES_ENABLED and REWARD_TIERS:
            inv_count = invite_count(m.guild.id, m.author.id)
            inv_done = claimed_tiers(m.author.id)
            parts.append(
                f"**Invites: {inv_count}**\n" + "\n".join(tier_line(t, t["invites"] in inv_done, inv_count >= t["invites"]) for t in REWARD_TIERS)
            )
        if BOOST_TIERS:
            b_count = get_boosts(m.author.id)
            b_done = claimed_boost_tiers(m.author.id)
            parts.append(
                f"**Boosts: {b_count}** (set by an admin with `boost-set`)\n"
                + "\n".join(boost_tier_line(t, t["boosts"] in b_done, b_count >= t["boosts"]) for t in BOOST_TIERS)
            )
        body = "\n\n".join(parts) if parts else "Nothing to show."
        return await reply(m, card("🎁 Claim reward", body, GOLD))

    ram = tier["ram_gb"] * 1024
    err = check_spec_limits(tier["cpu"], ram, tier["disk_gb"])
    if err:
        return await reply(m, card("Tier misconfigured", f"This reward tier exceeds server limits: {err}", RED))

    label = f"{tier['invites']} invites" if kind == "invite" else f"{tier['boosts']} boosts"
    note = f"\n\n🎁 Reward for **{label}** — claimed by {m.author.mention}."
    picked = await ask_os(m, m.author.mention)
    if picked is None:
        return
    status, template_file = picked
    # mark first so a double-run can't claim twice; undo if the deploy fails
    if kind == "invite":
        mark_claimed(m.author.id, tier["invites"])
    else:
        mark_boost_claimed(m.author.id, tier["boosts"])
    ok = await deploy_container(
        status, m.author, m.author, ram, tier["cpu"], tier["disk_gb"], INVITE_CLUSTER,
        template_file=template_file, extra_note=note,
    )
    if not ok:
        if kind == "invite":
            unmark_claimed(m.author.id, tier["invites"])
        else:
            unmark_boost_claimed(m.author.id, tier["boosts"])


async def handle_boost_set(m: discord.Message):
    if not is_admin(m.author.id):
        return await reply(m, card("No permission", "Only admins can set boost counts.", RED))
    parts = m.content.strip().split()
    users = await mentioned_users(m)
    if not users or len(parts) < 3 or not parts[-1].isdigit():
        return await reply(m, card("Usage", "`boost-set @user <count>`\nExample: `boost-set @user 2`", GOLD))
    user, count = users[0], int(parts[-1])
    set_boosts(user.id, count, m.author.id)
    body = f"{user.mention} now has **{count}** {'boost' if count == 1 else 'boosts'} on record."
    if BOOST_TIERS:
        done = claimed_boost_tiers(user.id)
        body += "\n\n" + "\n".join(boost_tier_line(t, t["boosts"] in done, count >= t["boosts"]) for t in BOOST_TIERS)
    await reply(m, card("🚀 Boosts updated", body, GREEN))


async def _resolve_ct(m: discord.Message, parts: list):
    """Parse `<cmd> <vmid> [cluster] ...` -> (ct, index of the first free-text word). Replies on errors."""
    if len(parts) < 2 or not parts[1].isdigit():
        await reply(m, card("Usage", f"`{parts[0]} <vmid> [cluster]`", GOLD))
        return None, None
    vmid, idx, cname = int(parts[1]), 2, None
    if len(parts) > 2 and parts[2].lower() in {n.lower() for n in CLUSTERS}:
        cname, idx = parts[2], 3
    rows = [r for r in db.execute("SELECT cluster FROM cts WHERE vmid=? AND state='ready'", (vmid,)).fetchall() if r[0] in CLUSTERS]
    if cname:
        rows = [r for r in rows if r[0].lower() == cname.lower()]
    if not rows:
        await reply(m, card("Not found", f"No container with CT `{vmid}`.", RED))
        return None, None
    if len(rows) > 1:
        names = ", ".join(f"`{r[0]}`" for r in rows)
        await reply(m, card("Which cluster?", f"CT `{vmid}` exists on several clusters ({names}). Add the cluster name.", GOLD))
        return None, None
    return get_ct(rows[0][0], vmid), idx


async def handle_suspend(m: discord.Message):
    if not is_admin(m.author.id):
        return await reply(m, card("No permission", "Admins only.", RED))
    parts = m.content.strip().split()
    ct, idx = await _resolve_ct(m, parts)
    if ct is None:
        return
    reason = " ".join(parts[idx:])[:200] or "no reason given"
    msg = await reply(m, loading_card("Suspending container", f"CT `{ct.vmid}` · `{ct.cluster}`"))
    stop_note = ""
    try:
        await asyncio.to_thread(power, CLUSTERS[ct.cluster], ct.node, ct.vmid, "stop")
    except Exception as e:  # already stopped is fine
        stop_note = f"\n-# stop said: {err_text(e, 150)}"
    db.execute("UPDATE cts SET suspended=1 WHERE cluster=? AND vmid=?", (ct.cluster, ct.vmid))
    db.commit()
    await msg.edit(view=card(
        "⛔ Suspended",
        f"CT `{ct.vmid}` (`{ct.hostname}`) owned by <@{ct.owner}> is stopped and can't be started by its owner.\n"
        f"**Reason:** {reason}{stop_note}\nUse `unsuspend {ct.vmid}` to give it back.",
        RED,
    ))
    owner = await resolve_user(ct.owner)
    if owner:
        try:
            await owner.send(view=card("⛔ Container suspended",
                                       f"Your container `{ct.hostname}` (CT `{ct.vmid}`) was suspended by an admin.\n**Reason:** {reason}", RED))
        except discord.HTTPException:
            pass


async def handle_unsuspend(m: discord.Message):
    if not is_admin(m.author.id):
        return await reply(m, card("No permission", "Admins only.", RED))
    parts = m.content.strip().split()
    ct, _ = await _resolve_ct(m, parts)
    if ct is None:
        return
    msg = await reply(m, loading_card("Restoring container", f"CT `{ct.vmid}` · `{ct.cluster}`"))
    db.execute("UPDATE cts SET suspended=0 WHERE cluster=? AND vmid=?", (ct.cluster, ct.vmid))
    db.commit()
    note = ""
    try:
        await asyncio.to_thread(power, CLUSTERS[ct.cluster], ct.node, ct.vmid, "start")
    except Exception as e:
        note = f"\n-# could not start it: {err_text(e, 150)} (use `manage` to start it)"
    await msg.edit(view=card("✅ Restored", f"CT `{ct.vmid}` (`{ct.hostname}`) is no longer suspended.{note}", GREEN))


PREFIX = "cb?"

# ------------------------------------------------ extra public ports (shared IPv4)
MAX_EXTRA_PORTS = int(cfg.get("max_extra_ports_per_ct", 50))
PORT_PROTOS = {"tcp": ("tcp",), "udp": ("udp",), "both": ("tcp", "udp")}
PORT_ASSIGN_USAGE = (
    "`assign-ports @user <count>` or `assign-ports <vmid> <count>`\n"
    "Reserves <count> extra public ports (1-20 at a time) for that container. They share the node's "
    "public IPv4, so each one is a different port number."
)
PORT_USAGE = (
    "`port forward <port on the VM> [tcp|udp|both] [vmid]`\n"
    "Opens one of your assigned public ports and points it at that port inside your container.\n\n"
    "`port list [vmid]` - show your forwards\n"
    "`port remove <public port>` - stop forwarding it (the port stays assigned to you)\n\n"
    "Need more ports? Ask an admin for `assign-ports`."
)


def _xrules(ip: str, pub: int, tgt: int, proto: str, iface: Optional[str]):
    i = f"-i {shlex.quote(iface)} " if iface else ""
    ipq = shlex.quote(ip)
    dnat = f"PREROUTING {i}-p {proto} --dport {int(pub)} -j DNAT --to-destination {ipq}:{int(tgt)}"
    fwd = f"FORWARD {i}-p {proto} -d {ipq} --dport {int(tgt)} -j ACCEPT"
    return dnat, fwd


def extra_ensure(ip, pub, tgt, proto, iface) -> str:
    out = ""
    for p in PORT_PROTOS.get(proto, ("tcp",)):
        dnat, fwd = _xrules(ip, pub, tgt, p, iface)
        out += (
            f"iptables -t nat -C {dnat} 2>/dev/null || iptables -t nat -A {dnat}\n"
            f"iptables -C {fwd} 2>/dev/null || iptables -I {fwd}\n"
        )
    return out


def extra_remove(ip, pub, tgt, proto, iface) -> str:
    out = ""
    for p in PORT_PROTOS.get(proto, ("tcp",)):
        dnat, fwd = _xrules(ip, pub, tgt, p, iface)
        out += f"iptables -t nat -D {dnat} 2>/dev/null\niptables -D {fwd} 2>/dev/null\n"
    return out + "true\n"


def extra_rows(cluster: str, vmid: int):
    return db.execute(
        "SELECT public_port, target_port, proto FROM extra_ports WHERE cluster=? AND vmid=? ORDER BY public_port",
        (cluster, vmid),
    ).fetchall()


def can_use_ct(user_id: int, ct: CT) -> bool:
    return ct.owner == user_id or is_admin(user_id) or is_shared_with(ct.owner, user_id)


def find_ct(owner_id: Optional[int], vm: Optional[int]):
    """Returns (CT, None) or (None, error text). owner_id and/or vm may be None."""
    if owner_id is not None:
        cts = user_cts(owner_id)
        if vm is not None:
            cts = [c for c in cts if c.vmid == vm]
        if not cts:
            return None, "No matching container found."
        if len(cts) > 1:
            return None, "Several containers match, add the vmid: " + ", ".join(f"`{c.vmid}`" for c in cts)
        return cts[0], None
    if vm is None:
        return None, "Give a vmid or @user."
    names = [r[0] for r in db.execute("SELECT cluster FROM cts WHERE vmid=? AND state='ready'", (vm,)).fetchall() if r[0] in CLUSTERS]
    if not names:
        return None, f"No container with CT `{vm}`."
    if len(names) > 1:
        return None, f"CT `{vm}` exists on several clusters."
    return get_ct(names[0], vm), None


def allocate_extra_ports(cl: Cluster, ct: CT, n: int, by: int):
    with RES_LOCK:
        have = db.execute("SELECT COUNT(*) FROM extra_ports WHERE cluster=? AND vmid=?", (cl.name, ct.vmid)).fetchone()[0]
        if have + n > MAX_EXTRA_PORTS:
            raise RuntimeError(f"Limit is {MAX_EXTRA_PORTS} extra ports per container (it already has {have}).")
        used = {r[0] for r in db.execute("SELECT ssh_port FROM cts WHERE cluster=? AND ssh_port IS NOT NULL", (cl.name,))}
        used |= {r[0] for r in db.execute("SELECT public_port FROM extra_ports WHERE cluster=?", (cl.name,))}
        got = []
        for p in range(int(cl.c.get("ssh_port_start", 20000)), int(cl.c.get("ssh_port_end", 29999)) + 1):
            if p not in used:
                got.append(p)
                if len(got) == n:
                    break
        if len(got) < n:
            raise RuntimeError(f"Port range exhausted on {cl.name}")
        for p in got:
            db.execute(
                "INSERT INTO extra_ports (cluster,vmid,public_port,assigned_by,assigned_at) VALUES (?,?,?,?,?)",
                (cl.name, ct.vmid, p, by, time.time()),
            )
        db.commit()
    return got, have + n


def _do_forward(cl: Cluster, ct: CT, tgt: int, proto: str):
    """Returns (public_port, None) or (None, error text)."""
    with RES_LOCK:
        dup = db.execute(
            "SELECT public_port FROM extra_ports WHERE cluster=? AND vmid=? AND target_port=?", (cl.name, ct.vmid, tgt)
        ).fetchone()
        if dup:
            return None, f"VM port {tgt} is already forwarded on public port {dup[0]}. Use `port remove {dup[0]}` first."
        row = db.execute(
            "SELECT public_port FROM extra_ports WHERE cluster=? AND vmid=? AND target_port IS NULL ORDER BY public_port LIMIT 1",
            (cl.name, ct.vmid),
        ).fetchone()
        if not row:
            return None, "This container has no free assigned ports. Ask an admin to run `assign-ports`."
        pub = row[0]
        db.execute("UPDATE extra_ports SET target_port=?, proto=? WHERE cluster=? AND public_port=?", (tgt, proto, cl.name, pub))
        db.commit()
    try:
        rc, out, err = cl.run(ct.node, extra_ensure(ct.ip, pub, tgt, proto, cl.c.get("public_iface")), timeout=30)
        if rc != 0:
            raise RuntimeError((err or out).strip() or "iptables failed")
    except Exception as e:
        db.execute("UPDATE extra_ports SET target_port=NULL, proto=NULL WHERE cluster=? AND public_port=?", (cl.name, pub))
        db.commit()
        return None, str(e)[:300]
    return pub, None


def clear_extra_ports(ct: CT):
    """Remove the rules and rows of a container that is being deleted. Never raises."""
    try:
        cl = CLUSTERS.get(ct.cluster)
        rows = extra_rows(ct.cluster, ct.vmid)
        if cl and ct.ip:
            script = "".join(extra_remove(ct.ip, p, t, pr, cl.c.get("public_iface")) for p, t, pr in rows if t)
            if script:
                cl.run(ct.node, script)
    except Exception as e:
        print(f"[destroy] could not remove extra port forwards: {e}")
    db.execute("DELETE FROM extra_ports WHERE cluster=? AND vmid=?", (ct.cluster, ct.vmid))
    db.commit()


def sync_extra_rules():
    """Re-apply every extra port forward (survives node reboots)."""
    groups: dict = {}
    for cn, node, ip, pub, tgt, proto in db.execute(
        "SELECT c.cluster, c.node, c.ip, e.public_port, e.target_port, e.proto "
        "FROM extra_ports e JOIN cts c ON c.cluster=e.cluster AND c.vmid=e.vmid "
        "WHERE e.target_port IS NOT NULL AND c.ip IS NOT NULL AND c.state='ready'"
    ).fetchall():
        groups.setdefault((cn, node), []).append((ip, pub, tgt, proto))
    for (cn, node), items in groups.items():
        cl = CLUSTERS.get(cn)
        if not cl:
            continue
        script = "".join(extra_ensure(ip, pub, tgt, proto, cl.c.get("public_iface")) for ip, pub, tgt, proto in items) + "true\n"
        try:
            cl.run(node, script)
        except Exception as e:
            print(f"[sync-ports] {cn}/{node}: {e}")


async def handle_assign_ports(m: discord.Message):
    if not is_admin(m.author.id):
        return await reply(m, card("No permission", "Admins only.", RED))
    parts = m.content.strip().split()
    if len(parts) < 3 or not parts[-1].isdigit():
        return await reply(m, card("Usage", PORT_ASSIGN_USAGE, GOLD))
    n = int(parts[-1])
    if not 1 <= n <= 20:
        return await reply(m, card("Out of range", "Assign between 1 and 20 ports at a time.", RED))
    users = await mentioned_users(m)
    vm = next((int(x) for x in parts[1:-1] if x.isdigit() and len(x) < 10), None)
    ct, err = find_ct(users[0].id if users else None, vm)
    if err:
        return await reply(m, card("Not found", err, RED))
    cl = CLUSTERS[ct.cluster]
    if cl.ssh_mode() == "tmate":
        return await reply(m, card("Not supported", "This cluster has no public IPv4, so ports can't be forwarded.", RED))
    try:
        ports, total = await asyncio.to_thread(allocate_extra_ports, cl, ct, n, m.author.id)
    except Exception as e:
        return await reply(m, card("Could not assign", f"```{err_text(e)}```", RED))
    plist = ", ".join(f"`{p}`" for p in ports)
    host = cl.public_host(ct.node)
    body = (
        f"**Container** `{ct.hostname}` · CT `{ct.vmid}` · owner <@{ct.owner}>\n"
        f"**New public ports** {plist}\n"
        f"**Total assigned** {total}\n\n"
        f"The owner runs `{PREFIX}port forward <port on the VM>` to use one. Address will be `{host}:<port>`."
    )
    await reply(m, card("🔌 Ports assigned", body, GREEN))


async def _port_forward(m: discord.Message, args: list):
    if not args or not args[0].isdigit() or not 1 <= int(args[0]) <= 65535:
        return await reply(m, card("Usage", PORT_USAGE, GOLD))
    tgt, proto, vm = int(args[0]), "tcp", None
    if tgt == 22 and GW_ON:
        return await reply(m, card("Not allowed", f"SSH goes through the gateway on port {GW_PORT}, which always asks for the token. Forward a different port.", RED))
    for a in args[1:]:
        if a.lower() in PORT_PROTOS:
            proto = a.lower()
        elif a.isdigit():
            vm = int(a)
    ct, err = find_ct(m.author.id, None) if vm is None else find_ct(None, vm)
    if err:
        return await reply(m, card("Not found", err, RED))
    if not can_use_ct(m.author.id, ct):
        return await reply(m, card("No permission", "You can only forward ports on your own or shared containers.", RED))
    if ct.suspended and not is_admin(m.author.id):
        return await reply(m, card("Suspended", "This container is suspended by an admin.", RED))
    cl = CLUSTERS[ct.cluster]
    if cl.ssh_mode() == "tmate":
        return await reply(m, card("Not supported", "This cluster has no public IPv4, so ports can't be forwarded.", RED))
    if not ct.ip:
        return await reply(m, card("No IP yet", f"Press **SSH** in `{PREFIX}manage` first so the container's IP is resolved.", RED))
    pub, err = await asyncio.to_thread(_do_forward, cl, ct, tgt, proto)
    if err:
        return await reply(m, card("Could not forward", err, RED))
    host = cl.public_host(ct.node)
    body = (
        f"**Container** `{ct.hostname}` · CT `{ct.vmid}`\n"
        f"**Public** `{host}:{pub}` → **VM port** `{tgt}` ({proto})\n\n"
        f"Connect to `{host}:{pub}` from outside. The service inside the VM must listen on `0.0.0.0:{tgt}` "
        "(not only 127.0.0.1), and the VM's own firewall must allow it.\n"
        "-# Everyone shares one public IPv4, so each forward has its own public port."
    )
    await reply(m, card("🔌 Port forwarded", body, GREEN))


async def _port_list(m: discord.Message, args: list):
    vm = next((int(a) for a in args if a.isdigit()), None)
    if vm is None:
        cts = await asyncio.to_thread(user_cts, m.author.id)
    else:
        ct, err = find_ct(None, vm)
        if err:
            return await reply(m, card("Not found", err, RED))
        if not can_use_ct(m.author.id, ct):
            return await reply(m, card("No permission", "That isn't your container.", RED))
        cts = [ct]
    if not cts:
        return await reply(m, card("🔌 Port forwards", "You have no containers.", GOLD))
    lines = []
    for ct in cts:
        cl = CLUSTERS[ct.cluster]
        host = cl.public_host(ct.node)
        lines.append(f"### 🖥️ {ct.hostname} · CT `{ct.vmid}`")
        if ct.ssh_port:
            lines.append(f"`{host}:{ct.ssh_port}` → `22` (SSH)")
        rows = extra_rows(ct.cluster, ct.vmid)
        for pub, tgt, proto in rows:
            lines.append(f"`{host}:{pub}` → `{tgt}` ({proto})" if tgt else f"`{host}:{pub}` → *free, not forwarded yet*")
        if not rows:
            lines.append("-# No extra ports assigned. Ask an admin for `assign-ports`.")
    await reply(m, card("🔌 Port forwards", "\n".join(lines)[:3800], BLUE))


async def _port_remove(m: discord.Message, args: list):
    if not args or not args[0].isdigit():
        return await reply(m, card("Usage", PORT_USAGE, GOLD))
    pub = int(args[0])
    rows = [r for r in db.execute("SELECT cluster, vmid, target_port, proto FROM extra_ports WHERE public_port=?", (pub,)).fetchall() if r[0] in CLUSTERS]
    if not rows:
        return await reply(m, card("Not found", f"Public port `{pub}` isn't assigned to any container.", RED))
    cluster, vmid, tgt, proto = rows[0]
    ct = get_ct(cluster, vmid)
    if ct is None or not can_use_ct(m.author.id, ct):
        return await reply(m, card("No permission", "That port isn't on your container.", RED))
    if not tgt:
        return await reply(m, card("Not forwarded", f"Public port `{pub}` isn't forwarded right now.", GOLD))
    cl = CLUSTERS[cluster]
    if ct.ip:
        try:
            await asyncio.to_thread(cl.run, ct.node, extra_remove(ct.ip, pub, tgt, proto, cl.c.get("public_iface")), 30)
        except Exception as e:
            return await reply(m, card("Could not remove", f"```{err_text(e)}```", RED))
    db.execute("UPDATE extra_ports SET target_port=NULL, proto=NULL WHERE cluster=? AND public_port=?", (cluster, pub))
    db.commit()
    await reply(m, card("🔌 Forward removed", f"Public port `{pub}` no longer points at VM port `{tgt}`. It is still assigned to CT `{vmid}`; use `port forward` to reuse it.", GREEN))


async def handle_port(m: discord.Message):
    parts = m.content.strip().split()
    sub = parts[1].lower() if len(parts) > 1 else ""
    if sub == "forward":
        return await _port_forward(m, parts[2:])
    if sub in ("list", "ls"):
        return await _port_list(m, parts[2:])
    if sub in ("remove", "rm", "delete"):
        return await _port_remove(m, parts[2:])
    await reply(m, card("🔌 Ports", PORT_USAGE, BLUE))


# ---- never hand out an IP or SSH port that something on the node already uses
def _live_used(cl: "Cluster"):
    node = next(iter(cl.c.get("nodes", {})), cl.name)
    script = (
        "cat /etc/pve/lxc/*.conf 2>/dev/null | grep -E '^net0:' | grep -oE 'ip=[0-9.]+'; echo ---; "
        "iptables -t nat -S PREROUTING 2>/dev/null | grep -oE -- '--dport [0-9]+ '"
    )
    ips, ports, seen = set(), set(), False
    try:
        rc, out, _ = cl.run(node, script, timeout=30)
    except Exception as e:
        dbg(f"live scan failed: {e}")
        return ips, ports
    for line in out.splitlines():
        line = line.strip()
        if line == "---":
            seen = True
        elif not seen and line.startswith("ip="):
            ips.add(line[3:])
        elif seen and line.startswith("--dport "):
            v = line.split()[1]
            if v.isdigit():
                ports.add(int(v))
    return ips, ports


def allocate_ip(cl: Cluster) -> str:
    used = {r[0] for r in db.execute("SELECT ip FROM cts WHERE cluster=?", (cl.name,)) if r[0]}
    used |= _live_used(cl)[0]
    start = int(ipaddress.ip_address(cl.c["pool_start"]))
    end = int(ipaddress.ip_address(cl.c["pool_end"]))
    for n in range(start, end + 1):
        ip = str(ipaddress.ip_address(n))
        if ip not in used and ip != str(cl.c.get("gateway")):
            return ip
    raise RuntimeError(f"IP pool exhausted on {cl.name}")


def allocate_port(cl: Cluster) -> int:
    used = {r[0] for r in db.execute("SELECT ssh_port FROM cts WHERE cluster=? AND ssh_port IS NOT NULL", (cl.name,))}
    try:
        used |= {r[0] for r in db.execute("SELECT public_port FROM extra_ports WHERE cluster=?", (cl.name,))}
    except sqlite3.OperationalError:
        pass
    used |= _live_used(cl)[1]
    for p in range(int(cl.c.get("ssh_port_start", 20000)), int(cl.c.get("ssh_port_end", 29999)) + 1):
        if p not in used:
            return p
    raise RuntimeError(f"SSH port range exhausted on {cl.name}")


# ---- give the "Free VPS users" role to everyone who gets a container
VPS_ROLE_ID = cfg.get("vps_role_id")
VPS_ROLE_NAME = cfg.get("vps_role_name", "Free VPS users")
VPS_ROLE_GUILD = cfg.get("vps_role_guild_id")
_ROLE_LOCK = asyncio.Lock()


async def give_vps_role(status, user):
    try:
        guild = status.guild or (client.get_guild(int(VPS_ROLE_GUILD)) if VPS_ROLE_GUILD else None)
        if guild is None:
            return
        async with _ROLE_LOCK:
            role = guild.get_role(int(VPS_ROLE_ID)) if VPS_ROLE_ID else discord.utils.get(guild.roles, name=VPS_ROLE_NAME)
            if role is None:
                role = await guild.create_role(name=VPS_ROLE_NAME, reason="Created by the deploy bot")
        member = guild.get_member(user.id) or await guild.fetch_member(user.id)
        if role not in member.roles:
            await member.add_roles(role, reason="Got a VPS from the bot")
            dbg(f"gave role {role.name} to {user}")
    except discord.Forbidden:
        dbg("role: the bot needs Manage Roles, and its role must be above the Free VPS users role")
    except discord.NotFound:
        pass
    except Exception as e:
        dbg(f"role error: {e}")


# ---- SSH access token gate + new deploy cards ----------------------------------
import hashlib

TOKEN_GATE_SCRIPT = r"""#!/bin/bash
# Cubaris Secure VPS Gateway: animated banner, then the access token check.
HASH=$(cat /etc/ssh/token.sha256 2>/dev/null)
if [ -z "$HASH" ]; then
  echo "Access token is not configured on this VPS. Ask the owner to run the token command in Discord." >&2
  exit 1
fi
SH=${SHELL:-/bin/bash}
E=$'\033'; RS="${E}[0m"; BD="${E}[1m"
GR="${E}[38;5;84m"; RD="${E}[38;5;203m"; CY="${E}[38;5;45m"; PK="${E}[38;5;213m"
KEY=0
if [ -n "$SSH_USER_AUTH" ] && grep -q '^publickey' "$SSH_USER_AUTH" 2>/dev/null; then KEY=1; fi
IS_TTY=0
if [ -t 0 ] && [ -t 1 ]; then IS_TTY=1; fi

type_out() {
  local t="$1"
  local i
  for ((i = 0; i < ${#t}; i++)); do printf '%s' "${t:i:1}"; sleep 0.02; done
}

step() {
  printf '  %s▸%s %-18s' "$CY" "$RS" "$1"
  sleep 0.15
  printf '%s%s%s\n' "$GR" "$2" "$RS"
}

banner() {
  local art=(
" ██████╗██╗   ██╗██████╗  █████╗ ██████╗ ██╗███████╗"
"██╔════╝██║   ██║██╔══██╗██╔══██╗██╔══██╗██║██╔════╝"
"██║     ██║   ██║██████╔╝███████║██████╔╝██║███████╗"
"██║     ██║   ██║██╔══██╗██╔══██║██╔══██╗██║╚════██║"
"╚██████╗╚██████╔╝██████╔╝██║  ██║██║  ██║██║███████║"
" ╚═════╝ ╚═════╝ ╚═════╝ ╚═╝  ╚═╝╚═╝  ╚═╝╚═╝╚══════╝"
  )
  local col=(51 45 39 63 99 171)
  local i
  echo
  for i in 0 1 2 3 4 5; do
    printf '%s  %s%s\n' "${E}[1;38;5;${col[$i]}m" "${art[$i]}" "$RS"
    sleep 0.07
  done
  printf '\n  %s✦ ━━━━━━━━━━━ %s%s' "$PK" "$RS" "$BD"
  type_out "CUBARIS SECURE VPS GATEWAY"
  printf ' %s━━━━━━━━━━━ ✦%s\n\n' "$PK" "$RS"
  step "Secure channel" "✔ established"
  step "Node" "$(hostname)"
  if [ "$KEY" = 1 ]; then step "Identity" "✔ SSH key verified"; else step "Identity" "✔ password accepted"; fi
  step "Time" "$(date -u '+%Y-%m-%d %H:%M UTC')"
  printf '\n  %s' "${E}[38;5;99m"
  for ((i = 0; i < 32; i++)); do printf '█'; sleep 0.02; done
  printf '%s\n\n' "$RS"
}

ok=0
if [ "$IS_TTY" = 1 ]; then banner; fi
if [ "$KEY" = 1 ]; then
  ok=1
elif [ "$IS_TTY" = 1 ]; then
  for n in 1 2 3; do
    read -r -s -p "${E}[1;38;5;51m🔐 Access Token: ${RS}" t < /dev/tty
    echo
    if [ "$(printf %s "$t" | sha256sum | cut -d' ' -f1)" = "$HASH" ]; then ok=1; break; fi
    printf '  %s✖ Wrong token (%s/3)%s\n' "$RD" "$n" "$RS"
    sleep 1.5
  done
else
  echo "Interactive login required (access token). scp/sftp need an SSH key." >&2
fi
if [ "$ok" != 1 ]; then
  if [ "$IS_TTY" = 1 ]; then printf '  %sAccess denied.%s\n' "$RD" "$RS"; fi
  exit 1
fi
if [ "$IS_TTY" = 1 ]; then printf '  %s✔ Access granted. Welcome to Cubaris.%s\n\n' "$GR" "$RS"; fi
if [ -n "$SSH_ORIGINAL_COMMAND" ]; then
  exec "$SH" -c "$SSH_ORIGINAL_COMMAND"
fi
exec "$SH" -l
"""


def get_token(cluster: str, vmid: int) -> Optional[str]:
    r = db.execute("SELECT token FROM cts WHERE cluster=? AND vmid=?", (cluster, vmid)).fetchone()
    return r[0] if r and r[0] else None


def install_token_gate(cl: Cluster, node: str, vmid: int, rotate: bool = False) -> str:
    """Install the token prompt inside the container (idempotent). Returns the token."""
    token = (None if rotate else get_token(cl.name, vmid)) or secrets.token_urlsafe(24)
    digest = hashlib.sha256(token.encode()).hexdigest()
    conf = "ExposeAuthInfo yes\\nForceCommand /usr/local/sbin/token-gate\\n"
    script = (
        "mkdir -p /usr/local/sbin /run/sshd; "
        "cat > /usr/local/sbin/token-gate <<'GATE_EOF'\n" + TOKEN_GATE_SCRIPT + "GATE_EOF\n"
        "chmod 755 /usr/local/sbin/token-gate; "
        f"echo {digest} > /etc/ssh/token.sha256; chmod 600 /etc/ssh/token.sha256; "
        "if grep -qE '^[[:space:]]*Include[[:space:]]+/etc/ssh/sshd_config.d/' /etc/ssh/sshd_config; then "
        f"mkdir -p /etc/ssh/sshd_config.d; printf '{conf}' > /etc/ssh/sshd_config.d/00-token-gate.conf; "
        "else "
        f"grep -q token-gate /etc/ssh/sshd_config || {{ printf '{conf}' | cat - /etc/ssh/sshd_config > /tmp/sshd_new && cat /tmp/sshd_new > /etc/ssh/sshd_config; }}; "
        "fi; "
        "sshd -t && (systemctl restart ssh 2>/dev/null || systemctl restart sshd)"
    )
    rc, out, err = cl.pct_exec(node, vmid, script, timeout=60)
    if rc != 0:
        raise RuntimeError("token gate: " + ((err or out).strip()[-250:] or f"exit {rc}"))
    db.execute("UPDATE cts SET token=? WHERE cluster=? AND vmid=?", (token, cl.name, vmid))
    db.commit()
    return token


_orig_setup_ssh = setup_ssh


def setup_ssh(cl, node, vmid, ip, port):
    """Same as before, and re-applies the token gate (same token) if this container already has one,
    so reinstalls and the SSH button never remove it."""
    ip = _orig_setup_ssh(cl, node, vmid, ip, port)
    if get_token(cl.name, vmid):
        install_token_gate(cl, node, vmid)
    return ip


def short_ram(mb: int) -> str:
    return fmt_ram(mb).replace(" ", "")


def os_label(template_file: Optional[str]) -> str:
    for v in OS_TEMPLATES.values():
        if v.get("file") == template_file and v.get("label"):
            return str(v["label"])
    m = re.match(r"([a-z]+)-(\d+(?:\.\d+)?)", template_file or "")
    return f"{m[1]}-{m[2]}" if m else (template_file or "unknown")


async def deploy_container(
    status: discord.Message,
    user: discord.abc.User,
    requester: discord.abc.User,
    ram: int,
    cpu: int,
    disk: int,
    cname: Optional[str],
    template_file: Optional[str] = None,
    extra_note: str = "",
) -> bool:
    """Deploy flow with the new cards and the SSH access token."""
    loader = Loader(status, "Deploying container", DEPLOY_STEPS, user.mention,
                    expect=[2, 3, 30, 25], flavor=DEPLOY_FLAVOR)
    loader.start()
    try:
        notes: list[str] = []
        target = await asyncio.to_thread(pick_target, ram, cname, notes)
        if not target:
            await loader.stop()
            detail = "\n".join(notes) or "No clusters are configured."
            title = "No capacity" if ENFORCE_CAPACITY else "No node available"
            await status.edit(view=card(title, f"Couldn't find an online node to deploy on:\n{detail}", RED))
            return False
        cl, node, _ = target

        loader.set(1)
        vmid, ip, port, host = await asyncio.to_thread(reserve, cl, node, user.id, ram, cpu, disk)
        pw = gen_password()

        loader.set(2)
        try:
            await asyncio.to_thread(provision, cl, node, vmid, host, ip, ram, cpu, disk, pw, template_file)
        except Exception:
            db.execute("DELETE FROM cts WHERE cluster=? AND vmid=?", (cl.name, vmid))
            db.commit()
            raise
    except Exception as e:
        await loader.stop()
        await status.edit(view=card("Deploy failed", f"```{err_text(e)}```", RED))
        return False

    loader.set(3)
    tmate_mode = cl.ssh_mode() == "tmate"
    ssh_note = ""
    token = None
    try:
        if tmate_mode:
            await asyncio.to_thread(setup_tmate, cl, node, vmid, ip)
        else:
            await asyncio.to_thread(setup_ssh, cl, node, vmid, ip, port)
            token = await asyncio.to_thread(install_token_gate, cl, node, vmid)
    except Exception as e:
        ssh_note = f"\n⚠️ SSH/token setup needs attention: `{err_text(e, 180)}`. Press **SSH** in `{PREFIX}manage`, then run `{PREFIX}token {vmid}`."
        if not tmate_mode:
            token = get_token(cl.name, vmid)

    ct = get_ct(cl.name, vmid)
    os_name = os_label(template_file)
    specs = f"{short_ram(ram)} RAM / {cpu} CPU / {disk}GB Disk"
    await loader.stop()
    took = int(loader.elapsed())
    if "give_vps_role" in globals():
        asyncio.create_task(give_vps_role(status, user))
    docker_note = ""
    if SEC_DOCKER:
        docker_note = "\n🐳 Docker is installing in the background - ready in about 1-2 minutes (`docker ps`)."
        spawn(_docker_background(cl, node, vmid, host))

    def pub_view(warn: str = ""):
        body = (
            f"VPS deployed on Proxmox for {user.mention}\n\n"
            "> **Resources**\n"
            f"> **RAM:** {short_ram(ram)}\n"
            f"> **CPU:** {cpu} Cores\n"
            f"> **Storage:** {disk}GB\n\n"
            "> **Details**\n"
            f"> **VMID:** {vmid}\n"
            f"> **Hostname:** `{host}`\n"
            f"> **OS:** {os_name}\n\n"
            "> **Access**\n"
            "> Credentials sent via DM 📩\n"
            f"{ssh_note}{warn}{extra_note}{docker_note}\n"
            f"-# ⚡ Deployed in {took}s"
        )
        return card("VPS Created!", body, BLUE)

    await status.edit(view=pub_view())

    if tmate_mode:
        login = tmate_card(ct)
    else:
        host_pub, _ = ssh_host_port(ct)
        lines = [
            f"Your new VPS has been created on **{BRAND}**!",
            "",
            f"**VMID** `{vmid}` • **Hostname** `{host}`",
            f"**OS** {os_name} • **Resources** {specs}",
            "",
            f"**SSH Command**\n```bash\nssh root@{host_pub} -p {port}\n```",
        ]
        if token:
            lines.append(f"**Token** ||{token}||")
        lines += [f"**Username** `root` • **Port** `{port}`", f"**Password** ||{pw}||", ""]
        if token:
            lines.append(f"When you connect, SSH asks for the **password** first and then for the **token**. Keep both private. Lost the token? Run `{PREFIX}token`.")
        else:
            lines.append(f"⚠️ The access token isn't active on this VPS yet. Run `{PREFIX}token {vmid}` to enable it.")
        lines.append(f"Change the password after your first login, or add an SSH key with `{PREFIX}manage`.")
        lines = gw_lines(vmid, host, os_name, specs, token) if GW_ON else lines
        login = card("Your VPS is Ready!", "\n".join(lines), GREEN)

    sent = False
    for dest in (user, requester):
        try:
            await dest.send(view=login)
            if tmate_mode:
                try:
                    await dest.send(view=card("🔑 Root password", f"||{pw}|| (useful once you're in)", GREEN))
                except discord.HTTPException:
                    pass
            sent = True
            break
        except discord.HTTPException:
            continue
    if not sent:
        try:
            await status.edit(view=pub_view(f"\n⚠️ I couldn't DM the credentials. Open your DMs, press **Reset password** in `{PREFIX}manage`, then run `{PREFIX}token`."))
        except discord.HTTPException:
            pass
    return True


def _token_ct(user_id: int, vm: Optional[int]):
    if vm is None:
        cts = user_cts(user_id)
        if not cts:
            return None, "You have no containers."
        if len(cts) > 1:
            return None, "You have several containers, add the vmid: " + ", ".join(f"`{c.vmid}`" for c in cts)
        return cts[0], None
    names = [r[0] for r in db.execute("SELECT cluster FROM cts WHERE vmid=? AND state='ready'", (vm,)).fetchall() if r[0] in CLUSTERS]
    if len(names) != 1:
        return None, f"No unique container with CT `{vm}`."
    return get_ct(names[0], vm), None


async def handle_token(m: discord.Message):
    parts = m.content.strip().split()
    rotate = len(parts) > 1 and parts[1].lower() in ("reset", "new", "rotate")
    args = parts[2:] if rotate else parts[1:]
    vm = next((int(a) for a in args if a.isdigit()), None)
    ct, err = _token_ct(m.author.id, vm)
    if err:
        return await reply(m, card("Token", err + "\nUsage: `token [vmid]` · `token reset [vmid]`", GOLD))
    if not (ct.owner == m.author.id or is_admin(m.author.id) or is_shared_with(ct.owner, m.author.id)):
        return await reply(m, card("No permission", "That isn't your container.", RED))
    cl = CLUSTERS[ct.cluster]
    if cl.ssh_mode() == "tmate":
        return await reply(m, card("Not needed", "This cluster uses a temporary relay link instead of a token.", GOLD))
    try:
        token = await asyncio.to_thread(install_token_gate, cl, ct.node, ct.vmid, rotate)
    except Exception as e:
        return await reply(m, card("Token setup failed", f"```{err_text(e)}```\nIs the container running?", RED))
    body = (
        f"**VMID** `{ct.vmid}` • **Hostname** `{ct.hostname}`\n"
        f"**Token** ||{token}||\n\n"
        + ("This is a **new** token; the old one no longer works.\n" if rotate else "")
        + "SSH asks for your password first, then for this token."
    )
    try:
        await m.author.send(view=card("🔑 Access token", body, GREEN))
    except discord.HTTPException:
        return await reply(m, card("Couldn't DM you", "Open your DMs and run the command again. The token is never posted in a channel.", RED))
    await reply(m, card("🔑 Token sent", "Check your DMs.", GREEN))


# ---- one public SSH port (gateway) for every VPS
GW = cfg.get("gateway") or {}
GW_ON = bool(GW.get("password"))
GW_USER = str(GW.get("user", "connect"))
GW_PORT = int(GW.get("port", 2222))
GW_PASS = str(GW.get("password", ""))
GW_HOST = str(GW.get("host") or (cfg["clusters"][0].get("public_host") or cfg["clusters"][0]["host"]))


def gw_lines(vmid, host, os_name, specs, token):
    out = [
        f"Your new VPS has been created on **{BRAND}**!",
        "",
        f"**VMID** `{vmid}` • **Hostname** `{host}`",
        f"**OS** {os_name} • **Resources** {specs}",
        "",
        f"**SSH Command**\n```bash\nssh {GW_USER}@{GW_HOST} -p {GW_PORT}\n```",
    ]
    if token:
        out.append(f"**Token** ||{token}||")
    out += [f"**Username** `{GW_USER}` • **Port** `{GW_PORT}`", f"**Password** ||{GW_PASS}||", ""]
    if token:
        out.append(f"SSH asks for the **password** first, then the **token**. The token decides which VPS you enter, so keep it private. Lost it? Run `{PREFIX}token`.")
    else:
        out.append(f"⚠️ Your access token isn't ready yet. Run `{PREFIX}token {vmid}` to create it.")
    return out


# ---- gateway mode: no per-VPS public SSH port, only the gateway (port 2222)
_prev_allocate_port = allocate_port
_prev_setup_ssh = setup_ssh
_prev_install_token_gate = install_token_gate
_prev_ssh_card = ssh_card


def allocate_port(cl):
    if GW_ON:
        return None
    return _prev_allocate_port(cl)


def setup_ssh(cl, node, vmid, ip, port):
    if GW_ON and port is None:
        wait_ready(cl, node, vmid)
        if ip is None:
            ip = discover_ip(cl, node, vmid)
            db.execute("UPDATE cts SET ip=? WHERE cluster=? AND vmid=?", (ip, cl.name, vmid))
            db.commit()
        return ip
    return _prev_setup_ssh(cl, node, vmid, ip, port)


def install_token_gate(cl, node, vmid, rotate=False):
    if not GW_ON:
        return _prev_install_token_gate(cl, node, vmid, rotate)
    token = (None if rotate else get_token(cl.name, vmid)) or secrets.token_urlsafe(24)
    db.execute("UPDATE cts SET token=? WHERE cluster=? AND vmid=?", (token, cl.name, vmid))
    db.commit()
    return token


def ssh_cmd(ct):
    if GW_ON and not ct.ssh_port:
        return f"ssh {GW_USER}@{GW_HOST} -p {GW_PORT}"
    host, port = ssh_host_port(ct)
    return f"ssh root@{host} -p {port}"


def ssh_card(ct):
    if GW_ON and not ct.ssh_port:
        body = (
            f"```bash\n{ssh_cmd(ct)}\n```\n"
            f"**Username** `{GW_USER}` • **Port** `{GW_PORT}`\n\n"
            "SSH asks for the shared password first, then for **your VPS token**. "
            f"The token decides which VPS you enter. Lost it? Run `{PREFIX}token {ct.vmid}`."
        )
        return card("🔐 SSH access", body, GREEN)
    return _prev_ssh_card(ct)


# ---- nicer OS picker
OS_STYLE = {
    "ubuntu": ("🟠", "Popular and beginner friendly. Fresh packages, great for apps, bots and websites."),
    "debian": ("🔴", "Rock solid and lightweight. The classic choice for stable servers."),
}


def _os_meta(key, label):
    desc = (OS_TEMPLATES.get(key) or {}).get("desc")
    for k, (emoji, d) in OS_STYLE.items():
        if k in key.lower() or k in str(label).lower():
            return emoji, desc or d
    return "💿", desc or "A clean, minimal install."


class OSChoiceView(ui.LayoutView):
    def __init__(self, viewer_id: int, spec_line: str, options):
        super().__init__(timeout=120)
        self.viewer_id = viewer_id
        self.chosen = None
        box = ui.Container(accent_colour=BLUE)
        box.add_item(ui.TextDisplay("## 💿 Choose an operating system"))
        box.add_item(ui.TextDisplay(spec_line))
        box.add_item(ui.Separator())
        for key, label in list(options)[:5]:
            emoji, desc = _os_meta(key, label)
            btn = ui.Button(label="Install", style=discord.ButtonStyle.success)
            btn.callback = self._pick(key)
            box.add_item(ui.Section(ui.TextDisplay(f"### {emoji} {label}\n{desc}"), accessory=btn))
        box.add_item(ui.Separator())
        cancel = ui.Button(label="Cancel", style=discord.ButtonStyle.secondary)
        cancel.callback = self._cancel
        row = ui.ActionRow()
        row.add_item(cancel)
        box.add_item(row)
        box.add_item(ui.TextDisplay(f"-# {BRAND} · only you can choose · expires in 2 minutes"))
        self.add_item(box)

    def _pick(self, key):
        async def cb(i: discord.Interaction):
            self.chosen = key
            await i.response.defer()
            self.stop()
        return cb

    async def _cancel(self, i: discord.Interaction):
        self.chosen = None
        await i.response.defer()
        self.stop()

    async def interaction_check(self, i: discord.Interaction) -> bool:
        if i.user.id != self.viewer_id:
            await i.response.send_message("This prompt isn't yours.", ephemeral=True)
            return False
        return True


# ---- urgent fixes: safe status, mass delete, token on every SSH path
async def handle_status(m: discord.Message):
    if not is_admin(m.author.id):
        return await reply(m, card("No permission", "Admins only.", RED))
    msg = await reply(m, loading_card("Checking infrastructure", "Querying every node…"))
    data = await asyncio.to_thread(cluster_overview)
    box = ui.Container(accent_colour=BLUE)
    box.add_item(ui.TextDisplay("## 📡 Infrastructure status"))
    for cname, nodes in data:
        box.add_item(ui.Separator())
        if nodes is None:
            box.add_item(ui.TextDisplay(f"### 🌐 {cname}\n🔴 API unreachable"))
            continue
        lines = [f"### 🌐 {cname}"]
        for n in sorted(nodes, key=lambda x: str(x.get("node", ""))):
            name = n.get("node", "?")
            cts = db.execute("SELECT COUNT(*) FROM cts WHERE cluster=? AND node=?", (cname, name)).fetchone()[0]
            if n.get("status") != "online":
                lines.append(f"🔴 **{name}** offline · {cts} CTs")
            elif not n.get("maxmem") or n.get("cpu") is None:
                lines.append(f"🟡 **{name}** online but sends no stats (API token needs Sys.Audit, or run `systemctl restart pvestatd` there) · {cts} CTs")
            else:
                mem = n.get("mem") or 0
                lines.append(
                    f"🟢 **{name}** · CPU `{n['cpu'] * 100:.0f}%` · "
                    f"RAM `{bar(mem / n['maxmem'], 8)}` {mem / GB:.1f}/{n['maxmem'] / GB:.0f} GB · {cts} CTs"
                )
        box.add_item(ui.TextDisplay("\n".join(lines)))
    box.add_item(ui.TextDisplay(f"-# {BRAND}"))
    view = ui.LayoutView()
    view.add_item(box)
    await msg.edit(view=view)


class ManageViewMass(ManageView):
    mass = False

    async def render(self, fresh: bool = True):
        await super().render(fresh)
        try:
            if not (is_admin(self.viewer_id) and self._cts and self.children and not self.busy and not self.confirming):
                return
            box = self.children[-1]
            kids = list(box.children)
            last = kids[-1] if kids else None
            if last is not None:
                box.remove_item(last)
            row = ui.ActionRow()
            if self.mass:
                box.add_item(ui.TextDisplay(f"⚠️ **Delete ALL {len(self._cts)} containers of {self.owner.mention}?** This cannot be undone."))
                row.add_item(self._btn(f"Yes, delete all {len(self._cts)}", discord.ButtonStyle.danger, self.on_mass_yes, emoji="🗑️"))
                row.add_item(self._btn("Cancel", discord.ButtonStyle.secondary, self.on_mass_cancel))
            else:
                row.add_item(self._btn(f"Mass delete ({len(self._cts)})", discord.ButtonStyle.danger, self.on_mass, emoji="💥"))
            box.add_item(row)
            if last is not None:
                box.add_item(last)
        except Exception as e:
            dbg(f"mass delete button: {e}")

    async def on_mass(self, i: discord.Interaction):
        if not is_admin(self.viewer_id):
            return await i.response.send_message("Admins only.", ephemeral=True)
        self.mass = True
        await i.response.defer()
        await self._refresh(i)

    async def on_mass_cancel(self, i: discord.Interaction):
        self.mass = False
        await i.response.defer()
        await self._refresh(i)

    async def on_mass_yes(self, i: discord.Interaction):
        if not is_admin(self.viewer_id):
            return await i.response.send_message("Admins only.", ephemeral=True)
        await i.response.defer()
        cts = list(self._cts)
        self.mass = False
        self.busy = f"Deleting {len(cts)} containers"
        await self.render(fresh=False)
        await i.edit_original_response(view=self)
        ok, errs = 0, []
        for ct in cts:
            try:
                await asyncio.to_thread(destroy, ct)
                ok += 1
            except Exception as e:
                errs.append(f"CT {ct.vmid}: {err_text(e, 60)}")
        self.note = f"🗑️ Deleted {ok} container(s)." + (f" ❌ {len(errs)} failed: " + "; ".join(errs[:3]) if errs else "")
        self.selected = None
        self.busy = None
        await self._refresh(i)


ManageView = ManageViewMass


async def handle_migrate_ssh(m: discord.Message):
    if not is_admin(m.author.id):
        return await reply(m, card("No permission", "Admins only.", RED))
    if not GW_ON:
        return await reply(m, card("Gateway is off", "Add the `gateway:` block to config.yaml first.", RED))
    words = m.content.strip().split()
    retire = len(words) > 1 and words[1].lower() == "retire"
    frag, names = _known_sql()
    rows = db.execute(f"SELECT cluster, vmid FROM cts WHERE state='ready' AND {frag}", names).fetchall()
    cts = [c for c in (get_ct(a, b) for a, b in rows) if c]
    msg = await reply(m, loading_card("Retiring old SSH ports" if retire else "Sending gateway tokens", f"{len(cts)} VPS…"))
    if retire:
        done, bad = 0, 0
        for ct in cts:
            cl = CLUSTERS[ct.cluster]
            try:
                if ct.ssh_port and ct.ip:
                    await asyncio.to_thread(cl.run, ct.node, remove_snippet(ct.ip, ct.ssh_port, cl.c.get("public_iface")))
                db.execute("UPDATE cts SET ssh_port=NULL WHERE cluster=? AND vmid=?", (ct.cluster, ct.vmid))
                done += 1
            except Exception:
                bad += 1
        db.commit()
        return await msg.edit(view=card("✅ Old ports retired", f"**{done}** VPS now use only `{GW_HOST}:{GW_PORT}` with their token.\n{bad} failed.", GREEN))
    sent, failed = 0, []
    for ct in cts:
        cl = CLUSTERS[ct.cluster]
        try:
            token = await asyncio.to_thread(install_token_gate, cl, ct.node, ct.vmid)
            owner = await resolve_user(ct.owner)
            body = (
                f"**VMID** `{ct.vmid}` • **Hostname** `{ct.hostname}`\n\n"
                f"**SSH Command**\n```bash\nssh {GW_USER}@{GW_HOST} -p {GW_PORT}\n```\n"
                f"**Username** `{GW_USER}` • **Port** `{GW_PORT}`\n"
                f"**Password** ||{GW_PASS}||\n**Token** ||{token}||\n\n"
                "SSH asks for the password first, then for the token. The token decides which VPS you enter, so keep it private. "
                "Your old SSH port will stop working soon."
            )
            await owner.send(view=card("🔐 Your VPS login has changed", body, GOLD))
            sent += 1
        except Exception:
            failed.append(f"<@{ct.owner}> (CT {ct.vmid})")
        await asyncio.sleep(1)
    extra = ("\n\nCould not DM: " + ", ".join(failed[:15])) if failed else ""
    await msg.edit(view=card("📨 Tokens sent", f"**{sent}** owner(s) got the new login by DM.{extra}\n\nWhen everyone has it, run `{PREFIX}migrate-ssh retire` to close the old ports.", GREEN))


HANDLERS = {
    "create": handle_create,
    "manage": handle_manage,
    "status": handle_status,
    "help": handle_help,
    "admin-add": handle_admin_add,
    "admin-remove": handle_admin_remove,
    "admins": handle_admins_list,
    "share-user": handle_share,
    "unshare-user": handle_unshare,
    "invites": handle_invites,
    "leaderboard": handle_leaderboard,
    "claim-reward": handle_claim_reward,
    "boost-set": handle_boost_set,
    "reinstall": handle_reinstall,
    "ct-list": handle_ct_list,
    "suspend": handle_suspend,
    "unsuspend": handle_unsuspend, "migrate-ssh": handle_migrate_ssh, "token": handle_token, "assign-ports": handle_assign_ports, "port": handle_port,
}


@client.event
async def on_message(m: discord.Message):
    # NOTE: DMs are allowed now (the old `not m.guild` check silently dropped every DM).
    if m.author.bot or not m.content.strip():
        return
    text = m.content.strip()
    had_prefix = text[: len(PREFIX)].lower() == PREFIX
    if had_prefix:
        text = text[len(PREFIX):].lstrip()
        m.content = text  # normalize so handlers behave the same with or without the prefix
    word = text.split(maxsplit=1)[0].lower() if text else ("help" if had_prefix else "")
    handler = HANDLERS.get(word)
    if not handler:
        return
    if rate_limited(m.author.id):  # quiet: just an hourglass, no reply spam
        try:
            await m.add_reaction("⏳")
        except discord.HTTPException:
            pass
        return
    if m.guild is None and word in GUILD_ONLY:
        return await reply(m, card("Server only", f"`{word}` only works inside the server, not in DMs.", GOLD))
    try:
        await handler(m)
    except Exception as e:
        print(f"[cmd:{word}] {type(e).__name__}: {e}")
        try:
            await reply(m, card("Something went wrong", f"```{err_text(e, 500)}```", RED))
        except discord.HTTPException:
            pass


@client.event
async def on_member_join(member: discord.Member):
    if not INVITES_ENABLED:
        return
    guild = member.guild
    try:
        live_invites = await guild.invites()
    except discord.Forbidden:
        return record_join(guild.id, member.id, None, None)  # bot lacks Manage Server
    used = diff_used_invite(guild.id, live_invites)
    inviter_id, code = (used.inviter.id, used.code) if used else (None, None)
    record_join(guild.id, member.id, inviter_id, code)

    if inviter_id and INVITE_ANNOUNCE_CHANNEL_ID:
        ch = guild.get_channel(int(INVITE_ANNOUNCE_CHANNEL_ID))
        if ch:
            count = invite_count(guild.id, inviter_id)
            try:
                await ch.send(
                    view=card(
                        "📨 New invite",
                        f"{member.mention} joined, invited by <@{inviter_id}>.\n<@{inviter_id}> now has **{count}** tracked invites.",
                        BLUE,
                    )
                )
            except discord.HTTPException:
                pass


@client.event
async def on_invite_create(invite: discord.Invite):
    if INVITES_ENABLED and invite.guild:
        cache_invites(invite.guild.id, [invite])


@client.event
async def on_ready():
    if INVITES_ENABLED:
        for guild in client.guilds:
            try:
                cache_invites(guild.id, await guild.invites())
            except discord.Forbidden:
                print(f"[invites] missing Manage Server permission in {guild.name}")
    print(f"Logged in as {client.user} · clusters: {', '.join(CLUSTERS)}")
    frag, names = _known_sql()
    orphans = db.execute(f"SELECT COUNT(*) FROM cts WHERE NOT ({frag})", names).fetchone()[0]
    if orphans:
        dbg(f"WARNING: {orphans} container record(s) belong to clusters that are not in config.yaml (hidden). "
            f"They still count toward each owner's limit - clean up with single_node.py --purge-db")
    dbg(f"security: nesting={SEC_NESTING} docker={SEC_DOCKER} net={SEC_NET_MBIT}Mbit pids={SEC_PIDS_MAX} "
        f"port_isolation={SEC_PORT_ISOLATION} rate_limit={RL_COMMANDS}/{RL_SECONDS:g}s")


client.run(cfg["token"])
