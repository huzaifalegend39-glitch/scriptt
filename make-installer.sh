#!/usr/bin/env bash
# make-installer.sh
# Packs your working bot (bot.py + doctor.py) into ONE self-extracting installer script.
#
#   Run it on the node that has the working bot:   bash make-installer.sh
#   Result:                                         /root/install.sh
#   Upload install.sh to any host that serves raw text, then on a fresh Proxmox node:
#       bash <(curl -sSf https://YOUR-URL/install.sh)
#
# Usage: bash make-installer.sh [bot_folder] [output_file]
set -euo pipefail
SRC=${1:-/root/proxmox_service-bot}
OUT=${2:-/root/install.sh}

for f in bot.py doctor.py; do
  [ -f "$SRC/$f" ] || { echo "missing $SRC/$f"; exit 1; }
done
python3 -c 'import ast,sys; ast.parse(open(sys.argv[1], encoding="utf-8").read())' "$SRC/bot.py" \
  || { echo "bot.py has a syntax error, fix it first"; exit 1; }

TPL=$(mktemp); PAY=$(mktemp)
trap 'rm -f "$TPL" "$PAY"' EXIT

cat > "$TPL" <<'TPLEOF'
#!/usr/bin/env bash
# Cubaris Cloud installer: Discord VPS bot, private network (vmbr1) and SSH gateway.
set -u
RED=$'\033[1;31m'; GRN=$'\033[1;32m'; CYN=$'\033[1;36m'; YEL=$'\033[1;33m'; RST=$'\033[0m'
say()  { printf '%s\n' "${CYN}▸${RST} $*"; }
ok()   { printf '%s\n' "${GRN}✔${RST} $*"; }
warn() { printf '%s\n' "${YEL}!${RST} $*"; }
die()  { printf '%s\n' "${RED}✖ $*${RST}" >&2; exit 1; }

[ "$(id -u)" = 0 ] || die "Run this as root."
APP_DIR=${APP_DIR:-/root/proxmox_service-bot}

ask()        { local a; read -r -p "$1${2:+ [$2]}: " a </dev/tty; echo "${a:-$2}"; }
ask_secret() { local a; read -r -s -p "$1: " a </dev/tty; echo >&2; echo "$a"; }
latest()     { pveam available --section system 2>/dev/null | awk -v p="$1" '$2 ~ "^"p {print $2}' | sort -V | tail -n1; }

extract_payload() {
  base64 -d <<'__CUBARIS_PAYLOAD__' | tar xz -C "$1"
@@PAYLOAD@@
__CUBARIS_PAYLOAD__
}

# ---------------------------------------------------------------- network
setup_network() {
  command -v ifreload >/dev/null 2>&1 || die "This needs Proxmox VE (ifreload not found)."
  if grep -qE '^(auto|iface) vmbr1' /etc/network/interfaces; then
    ok "vmbr1 already exists, skipping"
    return
  fi
  local up net gw
  up=$(ip route show default | awk '{print $5; exit}')
  [ -n "$up" ] || die "No default route found."
  net=$(ask "Private subnet for containers" "10.20.0.0/24")
  [[ $net =~ ^([0-9]+\.[0-9]+\.[0-9]+)\.0/24$ ]] || die "Use the form 10.20.0.0/24"
  gw="${BASH_REMATCH[1]}.1"
  cp /etc/network/interfaces "/etc/network/interfaces.bak.$(date +%s)"
  cat >> /etc/network/interfaces <<EOF

auto vmbr1
iface vmbr1 inet static
    address $gw/24
    bridge-ports none
    bridge-stp off
    bridge-fd 0
    post-up   echo 1 > /proc/sys/net/ipv4/ip_forward
    post-up   iptables -t nat -A POSTROUTING -s $net -o $up -j MASQUERADE
    post-down iptables -t nat -D POSTROUTING -s $net -o $up -j MASQUERADE
EOF
  echo 'net.ipv4.ip_forward=1' > /etc/sysctl.d/99-cubaris.conf
  ifreload -a || die "ifreload failed. Your old file is saved as /etc/network/interfaces.bak.*"
  ip -4 -br addr show vmbr1 | grep -q "$gw" || die "vmbr1 did not come up"
  ok "vmbr1 ready: $gw/24 (NAT out through $up)"
}

download_templates() {
  UBU=ubuntu-24.04-standard_24.04-2_amd64.tar.zst
  DEB=debian-12-standard_12.12-1_amd64.tar.zst
  command -v pveam >/dev/null 2>&1 || return 0
  pveam update >/dev/null 2>&1
  local u d f
  u=$(latest ubuntu-24.04-standard); d=$(latest debian-12-standard)
  [ -n "$u" ] && UBU=$u
  [ -n "$d" ] && DEB=$d
  for f in "$UBU" "$DEB"; do
    pveam list local 2>/dev/null | grep -q "$f" && continue
    say "Downloading $f"
    pveam download local "$f" >/dev/null 2>&1 || warn "could not download $f (check: pveam available --section system)"
  done
}

# ---------------------------------------------------------------- bot
write_config() {
  if [ -f config.yaml ]; then
    ok "config.yaml already exists, keeping it"
    return
  fi
  command -v pveum >/dev/null 2>&1 || die "No local Proxmox found. Create config.yaml by hand for a remote node."
  local token admin brand cname storage gw base up pubhost secret
  token=$(ask_secret "Discord bot token (hidden)")
  [[ $token =~ ^[A-Za-z0-9._-]+$ ]] || die "That doesn't look like a bot token"
  admin=$(ask "Your Discord user ID (root admin)" "")
  [[ $admin =~ ^[0-9]{15,20}$ ]] || die "Discord ID must be 15-20 digits"
  brand=$(ask "Brand name shown in the bot" "Cubaris Cloud")
  [[ $brand =~ ^[A-Za-z0-9\ ._-]+$ ]] || die "Brand: letters, digits, space . _ - only"
  cname=$(ask "Name for this node" "node1")
  [[ $cname =~ ^[A-Za-z0-9_-]+$ ]] || die "Node name: letters, digits, - and _ only"
  storage=$(pvesm status --content rootdir 2>/dev/null | awk 'NR>1 && $3=="active" {print $1; exit}')
  storage=$(ask "Container storage" "${storage:-local-lvm}")
  [[ $storage =~ ^[A-Za-z0-9_.-]+$ ]] || die "Invalid storage name"
  gw=$(ip -4 -o addr show vmbr1 2>/dev/null | awk '{print $4}' | cut -d/ -f1 | head -n1)
  [ -n "$gw" ] || die "vmbr1 not found. Run the network step first (menu 2, 3 or 5)."
  base=${gw%.*}
  up=$(ip route show default | awk '{print $5; exit}')
  pubhost=$(ask "Public IP or domain of this node" "$(curl -4 -s -m 5 https://ifconfig.me 2>/dev/null)")
  [[ $pubhost =~ ^[A-Za-z0-9.:-]+$ ]] || die "Invalid public host"
  pveum user add discordbot@pve >/dev/null 2>&1
  pveum aclmod / -user discordbot@pve -role PVEAdmin
  pveum user token remove discordbot@pve discordbot >/dev/null 2>&1
  secret=$(pveum user token add discordbot@pve discordbot --privsep 0 --output-format json | python3 -c 'import sys,json; print(json.load(sys.stdin)["value"])')
  [ -n "$secret" ] || die "Could not create the API token"
  pveum aclmod / -token 'discordbot@pve!discordbot' -role PVEAdmin >/dev/null 2>&1
  download_templates
  (umask 077; cat > config.yaml <<EOF
token: "$token"
admin_ids: [$admin]
max_cts_per_user: 3
brand: "$brand"
limits:
  max_cpu: 16
  max_ram_mb: 32768
  max_disk_gb: 500
enforce_capacity: false
security:
  nesting: false
  install_docker: false
  net_mbit: 100
  pids_max: 2048
  port_isolation: true
  rate_limit:
    commands: 6
    per_seconds: 10
os_templates:
  ubuntu:
    label: "Ubuntu 24.04"
    file: $UBU
    image: images:ubuntu/24.04
  debian:
    label: "Debian 12"
    file: $DEB
    image: images:debian/12
invites:
  enabled: false
  announce_channel_id: null
  cluster: null
  reward_tiers: []
boost_tiers: []
clusters:
  - name: $cname
    host: 127.0.0.1
    user: discordbot@pve
    token_name: discordbot
    token_value: "$secret"
    verify_ssl: false
    storage: $storage
    template_storage: local
    bridge: vmbr1
    subnet: $base.0/24
    gateway: $gw
    pool_start: $base.10
    pool_end: $base.250
    dns: 1.1.1.1
    ssh_host: localhost
    node_ssh:
      user: root
      port: 22
      key_file: $APP_DIR/proxmox_bot_key
    ssh_mode: forward
    public_host: $pubhost
    public_iface: $up
    ssh_port_start: 20000
    ssh_port_end: 29999
EOF
)
  chmod 600 config.yaml
  ok "config.yaml written (permissions 600)"
}

install_service() {
  cat > /etc/systemd/system/proxmox-bot.service <<EOF
[Unit]
Description=Proxmox Discord deploy bot
After=network-online.target
Wants=network-online.target

[Service]
WorkingDirectory=$APP_DIR
ExecStart=$APP_DIR/venv/bin/python $APP_DIR/bot.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload
  systemctl enable --now proxmox-bot >/dev/null 2>&1
  sleep 6
  if systemctl is-active --quiet proxmox-bot; then
    ok "Bot is running (logs: journalctl -u proxmox-bot -f)"
  else
    die "Bot did not start. See: journalctl -u proxmox-bot -n 30 --no-pager"
  fi
}

install_bot() {
  say "Installing packages"
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq && apt-get install -y -qq python3 python3-venv python3-pip openssh-client curl ca-certificates >/dev/null || die "apt failed"
  mkdir -p "$APP_DIR"
  [ -f "$APP_DIR/bot.py" ] && cp "$APP_DIR/bot.py" "$APP_DIR/bot.py.old"
  say "Unpacking the bot into $APP_DIR"
  extract_payload "$APP_DIR"
  cd "$APP_DIR" || die "cd failed"
  [ -d venv ] || python3 -m venv venv || die "venv failed"
  venv/bin/pip install -q -U pip
  venv/bin/pip install -q "discord.py>=2.6" proxmoxer paramiko pyyaml requests || die "pip install failed"
  ok "Python packages installed"
  [ -f proxmox_bot_key ] || ssh-keygen -q -t ed25519 -N '' -f proxmox_bot_key
  mkdir -p /root/.ssh; chmod 700 /root/.ssh
  grep -qF "$(cat proxmox_bot_key.pub)" /root/.ssh/authorized_keys 2>/dev/null || cat proxmox_bot_key.pub >> /root/.ssh/authorized_keys
  chmod 600 /root/.ssh/authorized_keys proxmox_bot_key
  write_config
  install_service
}

# ---------------------------------------------------------------- gateway
write_wrapper() {
  cat > /usr/local/sbin/cubaris-enter <<'WRAPEOF'
#!@@APP_DIR@@/venv/bin/python
import getpass, hmac, os, re, sqlite3, subprocess, sys, time

DB = "@@APP_DIR@@/bot.db"
CFG = "@@APP_DIR@@/config.yaml"
STATE = "/run/cubaris-gw"
E = "\033"
RS = E + "[0m"
ART = [
    " ██████╗██╗   ██╗██████╗  █████╗ ██████╗ ██╗███████╗",
    "██╔════╝██║   ██║██╔══██╗██╔══██╗██╔══██╗██║██╔════╝",
    "██║     ██║   ██║██████╔╝███████║██████╔╝██║███████╗",
    "██║     ██║   ██║██╔══██╗██╔══██║██╔══██╗██║╚════██║",
    "╚██████╗╚██████╔╝██████╔╝██║  ██║██║  ██║██║███████║",
    " ╚═════╝ ╚═════╝ ╚═════╝ ╚═╝  ╚═╝╚═╝  ╚═╝╚═╝╚══════╝",
]
COLORS = [51, 45, 39, 63, 99, 171]


def w(s, d=0.0):
    sys.stdout.write(s)
    sys.stdout.flush()
    if d:
        time.sleep(d)


def banner():
    w("\n")
    for line, c in zip(ART, COLORS):
        w(f"{E}[1;38;5;{c}m  {line}{RS}\n", 0.07)
    w(f"\n  {E}[38;5;213m✦ ━━━━━━━━━━━ {RS}{E}[1m")
    for ch in "CUBARIS SECURE VPS GATEWAY":
        w(ch, 0.02)
    w(f" {E}[38;5;213m━━━━━━━━━━━ ✦{RS}\n\n")
    rows = (
        ("Secure channel", "✔ established"),
        ("Identity", "✔ password accepted"),
        ("Time", time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())),
    )
    for k, v in rows:
        w(f"  {E}[38;5;45m▸{RS} {k:<16}", 0.15)
        w(f"{E}[38;5;84m{v}{RS}\n")
    w(f"\n  {E}[38;5;99m")
    for _ in range(32):
        w("█", 0.02)
    w(f"{RS}\n\n")


def sanitize(ip):
    return re.sub(r"[^0-9a-fA-F:.]", "_", ip)[:64] or "unknown"


def fails(ip):
    now = time.time()
    try:
        stamps = [float(x) for x in open(f"{STATE}/{ip}").read().split()]
    except (OSError, ValueError):
        stamps = []
    return [t for t in stamps if now - t < 600]


def add_fail(ip):
    os.makedirs(STATE, mode=0o700, exist_ok=True)
    st = fails(ip) + [time.time()]
    with open(f"{STATE}/{ip}", "w") as f:
        f.write(" ".join(str(t) for t in st))


def lookup(token):
    try:
        con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=5)
        rows = con.execute("SELECT vmid, token, suspended FROM cts WHERE token IS NOT NULL AND state='ready'").fetchall()
        con.close()
    except Exception:
        return None, 0, True
    hit = None
    for vmid, tok, susp in rows:
        if hmac.compare_digest(str(tok).encode(), token.encode()):
            hit = (vmid, susp)
    if hit:
        return hit[0], hit[1], False
    return None, 0, False


def route(token):
    import yaml
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=5)
    rows = con.execute("SELECT token, cluster, node FROM cts WHERE token IS NOT NULL AND state='ready'").fetchall()
    con.close()
    hit = None
    for tok, cluster, node in rows:
        if hmac.compare_digest(str(tok).encode(), token.encode()):
            hit = (cluster, node)
    cfg = yaml.safe_load(open(CFG, encoding="utf-8"))
    c = next((x for x in cfg["clusters"] if hit and x["name"] == hit[0]), None)
    if c is None:
        return None, "proxmox", "root", 22, None
    n = (c.get("nodes") or {}).get(hit[1], {})
    host = n.get("ssh_host", c.get("ssh_host", c.get("host")))
    ns = c.get("node_ssh") or {}
    if host in ("localhost", "127.0.0.1", "::1"):
        host = None
    return host, c.get("backend", "proxmox"), ns.get("user", "root"), int(ns.get("port", 22)), ns.get("key_file")


def remote(host, user, port, key, cmd, tty=False):
    return (["ssh"] + (["-t"] if tty else []) +
            ["-i", str(key), "-p", str(port), "-o", "BatchMode=yes",
             "-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=8", f"{user}@{host}", cmd])


def status_of(vmid, tok):
    try:
        host, backend, user, port, key = route(tok)
        if backend == "incus":
            return subprocess.CompletedProcess([], 0, "running", "")
        if host is None:
            return subprocess.run(["pct", "status", str(int(vmid))], capture_output=True, text=True, timeout=20)
        return subprocess.run(remote(host, user, port, key, f"pct status {int(vmid)}"), capture_output=True, text=True, timeout=30)
    except Exception:
        return subprocess.CompletedProcess([], 1, "unreachable", "")


def open_vps(vmid, tok):
    host, backend, user, port, key = route(tok)
    os.environ["HOME"] = "/root"
    if backend == "incus":
        inner = f"incus exec ct{int(vmid)} -- bash -l"
        argv = ["bash", "-c", inner] if host is None else remote(host, user, port, key, inner, True)
    elif host is None:
        argv = ["pct", "enter", str(int(vmid))]
    else:
        argv = remote(host, user, port, key, f"pct enter {int(vmid)}", True)
    os.execvp(argv[0], argv)


def main():
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        sys.stderr.write("An interactive terminal is required (no scp/sftp/commands through the gateway).\n")
        return 1
    ip = sanitize((os.environ.get("SSH_CLIENT") or "unknown").split()[0])
    banner()
    for n in (1, 2, 3):
        if len(fails(ip)) >= 10:
            w(f"  {E}[38;5;203m✖ Too many wrong tokens from your address. Try again in 10 minutes.{RS}\n")
            return 1
        try:
            tok = getpass.getpass(f"{E}[1;38;5;51m🔐 Access Token: {RS}").strip()
        except (EOFError, KeyboardInterrupt):
            return 1
        vmid, susp, err = lookup(tok) if tok else (None, 0, False)
        if err:
            w(f"  {E}[38;5;203m✖ Gateway error. Tell the admin.{RS}\n")
            return 1
        if vmid is not None:
            if susp:
                w(f"  {E}[38;5;203m✖ This VPS is suspended.{RS}\n")
                return 1
            r = status_of(vmid, tok)
            if "running" not in r.stdout:
                w(f"  {E}[38;5;203m✖ Your VPS is stopped. Start it from Discord (cb?manage).{RS}\n")
                return 1
            w(f"  {E}[38;5;84m✔ Token accepted. Opening your VPS...{RS}\n\n")
            open_vps(vmid, tok)
        add_fail(ip)
        w(f"  {E}[38;5;203m✖ Wrong token ({n}/3){RS}\n")
        time.sleep(1.5)
    w(f"  {E}[38;5;203mAccess denied.{RS}\n")
    return 1


if __name__ == "__main__":
    sys.exit(main())
WRAPEOF
  sed -i "s|@@APP_DIR@@|$APP_DIR|g" /usr/local/sbin/cubaris-enter
  chmod 755 /usr/local/sbin/cubaris-enter
  "$APP_DIR/venv/bin/python" -c "import ast; ast.parse(open('/usr/local/sbin/cubaris-enter').read())" || die "gateway program is broken"
}

install_gateway() {
  [ -f "$APP_DIR/config.yaml" ] || die "Install the bot first (it creates config.yaml)."
  [ -x "$APP_DIR/venv/bin/python" ] || die "Install the bot first (the Python environment is missing)."
  cd "$APP_DIR" || die "cd failed"
  say "Installing sudo and openssh-server"
  export DEBIAN_FRONTEND=noninteractive
  apt-get install -y -qq sudo openssh-server >/dev/null || die "apt failed"

  local ghost gport gpass glisten
  if ! grep -q '^gateway:' config.yaml; then
    ghost=$(ask "Domain or IP your users connect to" "$(awk '/public_host:/ {print $2; exit}' config.yaml)")
    [[ $ghost =~ ^[A-Za-z0-9.:-]+$ ]] || die "Invalid host"
    gport=$(ask "Public SSH gateway port" "2222")
    [[ $gport =~ ^[0-9]{2,5}$ ]] || die "Invalid port"
    gpass=$(ask_secret "Shared password for the gateway user (8-64 chars: letters digits @ . _ -)")
    [[ $gpass =~ ^[A-Za-z0-9@._-]{8,64}$ ]] || die "Password must be 8-64 characters: letters, digits and @ . _ - only"
    [ -n "$(tail -c1 config.yaml)" ] && echo >> config.yaml
    cat >> config.yaml <<EOF

gateway:
  user: connect
  port: $gport
  host: $ghost
  password: "$gpass"
EOF
    chmod 600 config.yaml
  fi
  gport=$(venv/bin/python -c 'import yaml; print(yaml.safe_load(open("config.yaml"))["gateway"].get("port", 2222))')
  [[ $gport =~ ^[0-9]{2,5}$ ]] || die "Bad gateway port in config.yaml"
  glisten=$(ask "Address the gateway listens on (0.0.0.0 = all, or one private IP)" "0.0.0.0")
  [[ $glisten =~ ^[0-9.]+$ ]] || die "Invalid listen address"
  if ! systemctl is-active --quiet cubaris-gateway; then
    ss -tln | grep -q ":$gport " && die "Port $gport is already in use on this machine. Pick another gateway port in config.yaml."
  fi

  say "Creating the gateway user and its restricted sudo rule"
  id connect >/dev/null 2>&1 || useradd -M -d /nonexistent -s /bin/bash connect
  write_wrapper
  cat > /etc/sudoers.d/cubaris-gateway <<'EOF'
Defaults:connect env_keep += "SSH_CLIENT SSH_CONNECTION"
Defaults:connect !lecture
connect ALL=(root) NOPASSWD: /usr/local/sbin/cubaris-enter ""
EOF
  chmod 440 /etc/sudoers.d/cubaris-gateway
  visudo -cf /etc/sudoers.d/cubaris-gateway >/dev/null || { rm -f /etc/sudoers.d/cubaris-gateway; die "sudoers rule is invalid"; }

  say "Writing the gateway SSH service"
  [ -f /etc/ssh/gateway_ed25519 ] || ssh-keygen -q -t ed25519 -N '' -f /etc/ssh/gateway_ed25519
  cat > /etc/ssh/sshd_config_gateway <<EOF
Port $gport
ListenAddress $glisten
HostKey /etc/ssh/gateway_ed25519
PidFile /run/sshd-gateway.pid
AllowUsers connect
PermitRootLogin no
PasswordAuthentication yes
KbdInteractiveAuthentication no
PubkeyAuthentication no
UsePAM yes
AuthenticationMethods password
AllowTcpForwarding no
AllowAgentForwarding no
X11Forwarding no
PermitTunnel no
PermitUserEnvironment no
PrintLastLog no
MaxAuthTries 3
LoginGraceTime 20
ForceCommand /usr/bin/sudo -n /usr/local/sbin/cubaris-enter
EOF
  local t
  t=$(sshd -T -f /etc/ssh/sshd_config_gateway 2>/dev/null) || die "gateway sshd config is invalid"
  grep -q '^forcecommand /usr/bin/sudo' <<<"$t" || die "ForceCommand missing, refusing to start the gateway (it would give a shell on this machine)"
  grep -q '^allowtcpforwarding no' <<<"$t" || die "TCP forwarding is not disabled, refusing to start the gateway"

  cat > /etc/systemd/system/cubaris-gateway.service <<EOF
[Unit]
Description=Cubaris SSH gateway
After=network-online.target
Wants=network-online.target

[Service]
ExecStartPre=/bin/mkdir -p /run/sshd
ExecStart=$(command -v sshd) -D -e -f /etc/ssh/sshd_config_gateway
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
  mkdir -p /etc/ssh/sshd_config.d
  echo 'DenyUsers connect' > /etc/ssh/sshd_config.d/99-deny-gateway-user.conf
  sshd -t && { systemctl reload ssh 2>/dev/null || systemctl reload sshd 2>/dev/null; }

  venv/bin/python - <<'PYEOF'
import subprocess, yaml
g = yaml.safe_load(open("config.yaml"))["gateway"]
subprocess.run(["chpasswd"], input=f"{g['user']}:{g['password']}\n", text=True, check=True)
PYEOF
  systemctl daemon-reload
  systemctl enable --now cubaris-gateway >/dev/null 2>&1
  sleep 2
  systemctl is-active --quiet cubaris-gateway || die "Gateway did not start: journalctl -u cubaris-gateway -n 20 --no-pager"
  systemctl restart proxmox-bot 2>/dev/null
  ok "SSH gateway is running on port $gport (listening on $glisten)"
  warn "Open port $gport in your firewall (or forward it from your public gateway machine) so users can reach it."
  warn "Users connect with: ssh connect@<host> -p $gport  then enter the VPS token."
}

main() {
  printf '%s\n' "" "${CYN}  Cubaris Cloud installer${RST}" "" \
    "  1) Install the bot" \
    "  2) Set up the private network (vmbr1)" \
    "  3) Network + bot" \
    "  4) SSH gateway (one public port, token login)" \
    "  5) Everything (network, bot, gateway)" \
    "  6) Quit" ""
  case "$(ask "Choose" "5")" in
    1) install_bot ;;
    2) setup_network ;;
    3) setup_network; install_bot ;;
    4) install_gateway ;;
    5) setup_network; install_bot; install_gateway ;;
    *) exit 0 ;;
  esac
  ok "Done."
}
main
TPLEOF

tar czf - -C "$SRC" bot.py doctor.py | base64 -w 76 > "$PAY"
sed -e "/^@@PAYLOAD@@\$/{r $PAY" -e 'd}' "$TPL" > "$OUT"
chmod 755 "$OUT"
bash -n "$OUT" || { echo "generated installer has a syntax error"; exit 1; }
echo "syntax OK"
echo "Built $OUT ($(du -h "$OUT" | cut -f1))"
echo "sha256: $(sha256sum "$OUT" | cut -d' ' -f1)"
