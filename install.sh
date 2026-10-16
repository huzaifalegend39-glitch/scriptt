#!/usr/bin/env bash
# Cubaris Cloud installer: Discord VPS bot, private network (vmbr1), SSH gateway and WireGuard routing.
# bot.py and doctor.py are downloaded from the same GitHub repo as this script.
set -u
RAW_BASE=${RAW_BASE:-https://raw.githubusercontent.com/huzaifalegend39-glitch/scriptt/main}
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

# ---------------------------------------------------------------- look and feel
LOG=/tmp/cubaris-install.log
: > "$LOG" 2>/dev/null || LOG=/dev/null
IS_TTY=0; [ -t 1 ] && IS_TTY=1
ESC=$'\033'
hide_cursor() { if [ "$IS_TTY" = 1 ]; then printf '%s' "${ESC}[?25l"; fi; }
show_cursor() { if [ "$IS_TTY" = 1 ]; then printf '%s' "${ESC}[?25h"; fi; }
trap 'show_cursor' EXIT
trap 'show_cursor; printf "\n"; exit 130' INT TERM

ART=(
  " ██████╗██╗   ██╗██████╗  █████╗ ██████╗ ██╗███████╗"
  "██╔════╝██║   ██║██╔══██╗██╔══██╗██╔══██╗██║██╔════╝"
  "██║     ██║   ██║██████╔╝███████║██████╔╝██║███████╗"
  "██║     ██║   ██║██╔══██╗██╔══██║██╔══██╗██║╚════██║"
  "╚██████╗╚██████╔╝██████╔╝██║  ██║██║  ██║██║███████║"
  " ╚═════╝ ╚═════╝ ╚═════╝ ╚═╝  ╚═╝╚═╝  ╚═╝╚═╝╚══════╝"
)
ART_COLORS=(51 45 39 63 99 171)
SPIN=(⠋ ⠙ ⠹ ⠸ ⠼ ⠴ ⠦ ⠧ ⠇ ⠏)

rep() { local s="" n=$2; while [ "$n" -gt 0 ]; do s+="$1"; n=$((n-1)); done; printf '%s' "$s"; }

typewriter() {
  local s=$1 d=${2:-0.02} i
  for ((i=0; i<${#s}; i++)); do printf '%s' "${s:i:1}"; sleep "$d"; done
}

banner() {
  if [ "$IS_TTY" != 1 ]; then printf '\n  CUBARIS CLOUD INSTALLER\n\n'; return; fi
  local i
  printf '\n'
  for i in "${!ART[@]}"; do
    printf '%s\n' "${ESC}[1;38;5;${ART_COLORS[$i]}m  ${ART[$i]}${RST}"
    sleep 0.06
  done
  printf '\n  %s' "${ESC}[38;5;213m✦ ━━━ ${RST}${ESC}[1m"
  typewriter "CLOUD INSTALLER" 0.025
  printf '%s\n\n' " ${ESC}[38;5;213m━━━ ✦${RST}"
}

# loading bar, used for short "working" moments
loading() {
  local msg=$1 w=26 i
  if [ "$IS_TTY" != 1 ]; then ok "$msg"; return; fi
  hide_cursor
  for ((i=0; i<=w; i++)); do
    printf '\r  %s[%s%s]%s %s' "$CYN" "$(rep '█' "$i")" "$(rep '░' $((w-i)))" "$RST" "$msg"
    sleep 0.025
  done
  printf '\r%s\n' "  ${GRN}✔${RST} $msg${ESC}[K"
  show_cursor
}

# step "message" command args...   runs a command with a spinner, output goes to $LOG
# Set STEP_SOFT=1 in front of the call to get a return code instead of aborting.
step() {
  local msg=$1; shift
  local rc pid i=0 start=$SECONDS
  if [ "$IS_TTY" != 1 ]; then
    say "$msg"
    "$@" >>"$LOG" 2>&1; rc=$?
  else
    "$@" >>"$LOG" 2>&1 &
    pid=$!
    hide_cursor
    while kill -0 "$pid" 2>/dev/null; do
      printf '\r  %s%s%s %s %s%ss%s%s' "$CYN" "${SPIN[i % 10]}" "$RST" "$msg" "${ESC}[2m" "$((SECONDS-start))" "$RST" "${ESC}[K"
      i=$((i+1)); sleep 0.1
    done
    wait "$pid"; rc=$?
    show_cursor
  fi
  if [ "$rc" -eq 0 ]; then
    [ "$IS_TTY" = 1 ] && printf '\r%s\n' "  ${GRN}✔${RST} $msg ${ESC}[2m$((SECONDS-start))s${RST}${ESC}[K"
    return 0
  fi
  [ "$IS_TTY" = 1 ] && printf '\r%s\n' "  ${RED}✖${RST} $msg${ESC}[K"
  if [ "${STEP_SOFT:-0}" = 1 ]; then return 1; fi
  printf '%s\n' "${YEL}Last lines of the log:${RST}" >&2
  tail -n 15 "$LOG" >&2
  die "Step failed: $msg  (full log: $LOG)"
}

apt_install() { export DEBIAN_FRONTEND=noninteractive; apt-get update -qq && apt-get install -y -qq "$@"; }

# ---------------------------------------------------------------- license
LICENSE_SHA=e4285a2d52e44f8590aa2cb30c7db8a7d17a8a5fe3bcac0f2d59e77e8c7c47cf
verify_license() {
  local k n
  for n in 1 2 3; do
    if [ "$n" = 1 ] && [ -n "${CUBARIS_KEY:-}" ]; then k=$CUBARIS_KEY; else k=$(ask_secret "License key"); fi
    if [ "$(printf '%s' "$k" | sha256sum | cut -d' ' -f1)" = "$LICENSE_SHA" ]; then
      loading "License verified"
      return 0
    fi
    warn "Invalid license key ($n/3)"
  done
  die "Access denied."
}

# Download bot.py and doctor.py from GitHub into $1 (checks Python syntax first).
fetch_files() {
  local dest=$1 f tmp
  tmp=$(mktemp -d)
  for f in bot.py doctor.py; do
    curl -fsSL "$RAW_BASE/$f" -o "$tmp/$f" || { rm -rf "$tmp"; die "Could not download $f from $RAW_BASE (is it uploaded to the repo?)"; }
    python3 -c 'import ast,sys; ast.parse(open(sys.argv[1], encoding="utf-8").read())' "$tmp/$f" \
      || { rm -rf "$tmp"; die "$f from GitHub has a syntax error, not installing it"; }
  done
  for f in bot.py doctor.py; do
    cp "$tmp/$f" "$dest/$f"
  done
  rm -rf "$tmp"
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
  step "Installing packages" apt_install python3 python3-venv python3-pip openssh-client curl ca-certificates
  mkdir -p "$APP_DIR"
  [ -f "$APP_DIR/bot.py" ] && cp "$APP_DIR/bot.py" "$APP_DIR/bot.py.old"
  step "Downloading the bot into $APP_DIR" fetch_files "$APP_DIR"
  cd "$APP_DIR" || die "cd failed"
  [ -d venv ] || python3 -m venv venv || die "venv failed"
  step "Updating pip" venv/bin/pip install -q -U pip
  step "Installing Python packages" venv/bin/pip install -q "discord.py>=2.6" proxmoxer paramiko pyyaml requests
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
  step "Installing sudo and openssh-server" apt_install sudo openssh-server

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

# ---------------------------------------------------------------- wireguard
WG_VPS_IP=10.99.0.1
WG_PVE_IP=10.99.0.2

# Script that runs on the public VPS (sent over SSH).
wg_remote_script() {
cat <<'WGREMOTE'
set -eu
PUB="$1"; PORT="$2"; KEEP="$3"
export DEBIAN_FRONTEND=noninteractive
command -v apt-get >/dev/null 2>&1 || { echo "The VPS must run Debian or Ubuntu."; exit 1; }
apt-get update -qq >/dev/null 2>&1 || true
apt-get install -y -qq wireguard wireguard-tools iptables >/dev/null 2>&1 || { echo "apt install wireguard failed on the VPS"; exit 1; }
umask 077
mkdir -p /etc/wireguard
[ -f /etc/wireguard/cubaris.key ] || wg genkey > /etc/wireguard/cubaris.key
VPUB=$(wg pubkey < /etc/wireguard/cubaris.key)
UP=$(ip route show default | awk '{print $5; exit}')
[ -n "$UP" ] || { echo "The VPS has no default route"; exit 1; }
cat > /usr/local/sbin/cubaris-wg-fw <<FWEOF
#!/bin/bash
UP=$UP
WGPORT=$PORT
KEEP="$KEEP"
TARGET=10.99.0.2
rules() {
  local n=\$1 f=\$2 p
  iptables -t nat \$n PREROUTING -i \$UP -p udp --dport \$WGPORT -j RETURN
  for p in \${KEEP//,/ }; do
    iptables -t nat \$n PREROUTING -i \$UP -p tcp --dport \$p -j RETURN
    iptables -t nat \$n PREROUTING -i \$UP -p udp --dport \$p -j RETURN
  done
  iptables -t nat \$n PREROUTING -i \$UP -p tcp -j DNAT --to-destination \$TARGET
  iptables -t nat \$n PREROUTING -i \$UP -p udp -j DNAT --to-destination \$TARGET
  iptables -t nat \$n POSTROUTING -o wg0 -d \$TARGET -j MASQUERADE
  iptables \$f FORWARD -i \$UP -o wg0 -d \$TARGET -j ACCEPT
  iptables \$f FORWARD -i wg0 -o \$UP -j ACCEPT
}
case "\$1" in
  up)   rules -D -D 2>/dev/null; rules -A -I ;;
  down) rules -D -D 2>/dev/null; true ;;
esac
FWEOF
chmod 755 /usr/local/sbin/cubaris-wg-fw
echo 'net.ipv4.ip_forward=1' > /etc/sysctl.d/99-cubaris-wg.conf
sysctl -w net.ipv4.ip_forward=1 >/dev/null
cat > /etc/wireguard/wg0.conf <<WGEOF
[Interface]
Address = 10.99.0.1/24
ListenPort = $PORT
PrivateKey = $(cat /etc/wireguard/cubaris.key)
PostUp = /usr/local/sbin/cubaris-wg-fw up
PostDown = /usr/local/sbin/cubaris-wg-fw down

[Peer]
PublicKey = $PUB
AllowedIPs = 10.99.0.2/32
WGEOF
chmod 600 /etc/wireguard/wg0.conf
if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q "Status: active"; then
  ufw allow "$PORT/udp" >/dev/null 2>&1 || true
fi
systemctl enable wg-quick@wg0 >/dev/null 2>&1
systemctl restart wg-quick@wg0 || { echo "wg-quick@wg0 failed on the VPS (journalctl -u wg-quick@wg0)"; exit 1; }
echo "VPSPUB=$VPUB"
WGREMOTE
}

setup_wireguard() {
  say "WireGuard: route a public VPS IP to this node"
  local vip vssh wgport keep cp out vpub mypub i hs
  if [ -f /etc/wireguard/wg0.conf ]; then
    [ "$(ask "wg0 already exists here. Replace it? (yes/no)" "no")" = yes ] || { ok "Keeping the existing wg0"; return; }
    systemctl stop wg-quick@wg0 2>/dev/null
  fi
  vip=$(ask "Public IP of the VPS that should route to this node" "")
  [[ $vip =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]] || die "That is not an IPv4 address"
  vssh=$(ask "SSH port of that VPS" "22")
  [[ $vssh =~ ^[0-9]{1,5}$ ]] || die "Invalid SSH port"
  wgport=$(ask "WireGuard port on the VPS (UDP)" "51820")
  [[ $wgport =~ ^[0-9]{2,5}$ ]] || die "Invalid port"
  keep=$(ask "Ports to keep on the VPS and NOT forward (comma list; add 8006 to hide the Proxmox panel)" "$vssh")
  [[ $keep =~ ^[0-9]+(,[0-9]+)*$ ]] || die "Use a comma list like 22,8006"

  say "Installing WireGuard here"
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq >/dev/null 2>&1
  apt-get install -y -qq wireguard wireguard-tools iptables openssh-client >/dev/null || die "apt failed"
  mkdir -p /etc/wireguard
  (umask 077; [ -f /etc/wireguard/cubaris.key ] || wg genkey > /etc/wireguard/cubaris.key)
  mypub=$(wg pubkey < /etc/wireguard/cubaris.key)

  say "Connecting to root@$vip (type the VPS root password when asked)"
  cp=/tmp/cubaris-wg-ctl
  rm -f "$cp"
  ssh -o StrictHostKeyChecking=accept-new -o ControlMaster=yes -o ControlPath="$cp" -o ControlPersist=300 \
      -p "$vssh" "root@$vip" true || die "Could not SSH to root@$vip (needs root login and the right port)"
  say "Setting up the VPS side"
  out=$(ssh -o ControlPath="$cp" -p "$vssh" "root@$vip" "bash -s -- '$mypub' '$wgport' '$keep'" < <(wg_remote_script) 2>&1) \
    || { ssh -O exit -o ControlPath="$cp" "root@$vip" 2>/dev/null; die "VPS setup failed: $out"; }
  ssh -O exit -o ControlPath="$cp" "root@$vip" 2>/dev/null
  vpub=$(grep '^VPSPUB=' <<<"$out" | tail -n1 | cut -d= -f2)
  [[ $vpub =~ ^[A-Za-z0-9+/]{43}=$ ]] || die "Did not get the VPS public key. Output: $out"

  (umask 077; cat > /etc/wireguard/wg0.conf <<EOF
[Interface]
Address = $WG_PVE_IP/24
PrivateKey = $(cat /etc/wireguard/cubaris.key)

[Peer]
PublicKey = $vpub
Endpoint = $vip:$wgport
AllowedIPs = $WG_VPS_IP/32
PersistentKeepalive = 25
EOF
)
  chmod 600 /etc/wireguard/wg0.conf
  systemctl enable wg-quick@wg0 >/dev/null 2>&1
  systemctl restart wg-quick@wg0 || die "wg-quick@wg0 failed here (journalctl -u wg-quick@wg0)"

  ping -c1 -W2 "$WG_VPS_IP" >/dev/null 2>&1
  hs=0
  for i in $(seq 1 15); do
    hs=$(wg show wg0 latest-handshakes 2>/dev/null | awk '{print $2; exit}')
    [ "${hs:-0}" != 0 ] && break
    sleep 1
  done
  [ "${hs:-0}" != 0 ] || die "No handshake. Open UDP port $wgport in the VPS provider's firewall and run this option again."
  ok "Tunnel is up: this node $WG_PVE_IP <-> VPS $WG_VPS_IP"
  ok "Everything on $vip (except ports $keep and UDP $wgport) now goes to this node"
  warn "Connections reach this node as coming from $WG_VPS_IP, so real client IPs are hidden."

  if [ -f "$APP_DIR/config.yaml" ]; then
    if [ "$(ask "Set the bot's public_host to $vip and public_iface to wg0? (yes/no)" "no")" = yes ]; then
      cp "$APP_DIR/config.yaml" "$APP_DIR/config.yaml.bak.$(date +%s)"
      sed -i -E "s|^([[:space:]]*public_host:).*|\1 $vip|; s|^([[:space:]]*public_iface:).*|\1 wg0|" "$APP_DIR/config.yaml"
      systemctl restart proxmox-bot 2>/dev/null
      ok "Bot config updated and bot restarted"
    fi
  fi
}

# ---------------------------------------------------------------- web panels (Pterodactyl, Paymenter)
PANEL_CREDS=/root/cubaris-panels.txt

rand_pw() { head -c 64 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 24; }

set_env() {  # file key value
  if grep -q "^$2=" "$1"; then sed -i "s|^$2=.*|$2=$3|" "$1"; else echo "$2=$3" >> "$1"; fi
}

has_opt() { php artisan "$1" --help 2>/dev/null | grep -q -- "$2"; }

panel_guard() {
  if command -v pveversion >/dev/null 2>&1; then
    warn "This is a Proxmox host. A web panel is better on a separate VPS or in a container."
    [ "$(ask "Install it on this Proxmox host anyway? (yes/no)" "no")" = yes ] || return 1
  fi
  [ -r /etc/os-release ] || die "Cannot detect the operating system"
  . /etc/os-release
  case "${ID:-x}-${VERSION_ID:-x}" in
    ubuntu-22.04|ubuntu-24.04|debian-12|debian-13) ;;
    *) die "Supported systems: Ubuntu 22.04/24.04 and Debian 12/13 (this one is ${PRETTY_NAME:-unknown})" ;;
  esac
  return 0
}

panel_packages() {
  export DEBIAN_FRONTEND=noninteractive
  . /etc/os-release
  apt-get update -qq || return 1
  apt-get install -y -qq curl ca-certificates gnupg tar unzip git cron || return 1
  case "$ID-$VERSION_ID" in
    ubuntu-22.04)
      apt-get install -y -qq software-properties-common || return 1
      LC_ALL=C.UTF-8 add-apt-repository -y ppa:ondrej/php || return 1 ;;
    debian-*)
      curl -fsSL https://packages.sury.org/php/apt.gpg -o /usr/share/keyrings/deb.sury.org-php.gpg || return 1
      echo "deb [signed-by=/usr/share/keyrings/deb.sury.org-php.gpg] https://packages.sury.org/php/ $VERSION_CODENAME main" > /etc/apt/sources.list.d/sury-php.list ;;
  esac
  apt-get update -qq || return 1
  apt-get install -y -qq php8.3 php8.3-{common,cli,gd,mysql,mbstring,bcmath,xml,fpm,curl,zip,intl,redis} \
    mariadb-server nginx redis-server certbot python3-certbot-nginx || return 1
  curl -fsSL https://getcomposer.org/installer | php -- --install-dir=/usr/local/bin --filename=composer || return 1
  systemctl enable --now mariadb redis-server nginx php8.3-fpm cron
}

make_db() {  # db user password
  local db; db=$(command -v mariadb || command -v mysql)
  "$db" -e "CREATE DATABASE IF NOT EXISTS \`$1\`;" || return 1
  "$db" -e "CREATE USER IF NOT EXISTS '$2'@'127.0.0.1' IDENTIFIED BY '$3';" || return 1
  "$db" -e "ALTER USER '$2'@'127.0.0.1' IDENTIFIED BY '$3';" || return 1
  "$db" -e "GRANT ALL PRIVILEGES ON \`$1\`.* TO '$2'@'127.0.0.1' WITH GRANT OPTION; FLUSH PRIVILEGES;"
}

panel_download() {  # dir url
  mkdir -p "$1" && cd "$1" || return 1
  curl -fsSL -o panel.tar.gz "$2" || return 1
  tar -xzf panel.tar.gz || return 1
  rm -f panel.tar.gz
  chmod -R 755 storage/* bootstrap/cache/
}

nginx_site() {  # name domain dir
  local f="/etc/nginx/sites-available/$1.conf"
  cat > "$f" <<'NGX'
server {
    listen 80;
    server_name @@DOMAIN@@;
    root @@DIR@@/public;
    index index.php;

    access_log /var/log/nginx/@@NAME@@-access.log;
    error_log  /var/log/nginx/@@NAME@@-error.log error;

    client_max_body_size 100m;
    client_body_timeout 120s;
    sendfile off;

    location / {
        try_files $uri $uri/ /index.php?$query_string;
    }

    location = /favicon.ico { access_log off; log_not_found off; }
    location = /robots.txt  { access_log off; log_not_found off; }

    location ~ \.php$ {
        fastcgi_split_path_info ^(.+\.php)(/.+)$;
        fastcgi_pass unix:/run/php/php8.3-fpm.sock;
        fastcgi_index index.php;
        include fastcgi_params;
        fastcgi_param PHP_VALUE "upload_max_filesize = 100M \n post_max_size=100M";
        fastcgi_param SCRIPT_FILENAME $document_root$fastcgi_script_name;
        fastcgi_param HTTP_PROXY "";
        fastcgi_intercept_errors off;
        fastcgi_buffer_size 16k;
        fastcgi_buffers 4 16k;
        fastcgi_connect_timeout 300;
        fastcgi_send_timeout 300;
        fastcgi_read_timeout 300;
    }

    location ~ /\.ht {
        deny all;
    }
}
NGX
  sed -i "s|@@DOMAIN@@|$2|g; s|@@DIR@@|$3|g; s|@@NAME@@|$1|g" "$f"
  ln -sf "$f" "/etc/nginx/sites-enabled/$1.conf"
  rm -f /etc/nginx/sites-enabled/default
  nginx -t && systemctl reload nginx
}

queue_service() {  # service dir args
  cat > "/etc/systemd/system/$1.service" <<EOF
[Unit]
Description=$1 queue worker
After=redis-server.service

[Service]
User=www-data
Group=www-data
Restart=always
ExecStart=/usr/bin/php $2/artisan queue:work $3
StartLimitInterval=180
StartLimitBurst=30
RestartSec=5s

[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload
  systemctl enable --now "$1.service"
}

panel_finish() {  # name dir service queue-args
  echo "* * * * * root php $2/artisan schedule:run >> /dev/null 2>&1" > "/etc/cron.d/$1"
  chmod 644 "/etc/cron.d/$1"
  chown -R www-data:www-data "$2"
  queue_service "$3" "$2" "$4"
}

save_creds() { (umask 077; printf '%s\n' "$@" >> "$PANEL_CREDS"); }

ask_domain_email() {  # sets P_DOMAIN P_EMAIL P_SSL
  P_DOMAIN=$(ask "Domain for the panel (its DNS A record must already point to this server)" "")
  [[ $P_DOMAIN =~ ^[A-Za-z0-9.-]+\.[A-Za-z]{2,}$ ]] || die "That is not a valid domain"
  P_EMAIL=$(ask "Your email (admin account and SSL notices)" "")
  [[ $P_EMAIL =~ ^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$ ]] || die "That is not a valid email"
  P_SSL=$(ask "Get a free SSL certificate now? (yes/no)" "yes")
}

try_ssl() {  # domain email -> prints https or http in P_SCHEME
  P_SCHEME=http
  [ "$P_SSL" = yes ] || return 0
  if STEP_SOFT=1 step "Getting a Let's Encrypt certificate" certbot --nginx -d "$1" --non-interactive --agree-tos -m "$2" --redirect; then
    P_SCHEME=https
  else
    warn "SSL failed (check the DNS record and that ports 80/443 are open; on Cloudflare use DNS-only while issuing). Continuing with http."
  fi
}

ptero_configure() {  # dir email url tz dbpw user pass
  local dir=$1 email=$2 url=$3 tz=$4 dbpw=$5 user=$6 pass=$7
  cd "$dir" || return 1
  cp .env.example .env
  export COMPOSER_ALLOW_SUPERUSER=1
  composer install --no-dev --optimize-autoloader --no-interaction || return 1
  php artisan key:generate --force || return 1
  local a=(--author="$email" --url="$url" --timezone="$tz" --cache=redis --session=redis --queue=redis
           --redis-host=127.0.0.1 --redis-pass=null --redis-port=6379 --no-interaction)
  has_opt p:environment:setup --settings-ui && a+=(--settings-ui=true)
  has_opt p:environment:setup --telemetry && a+=(--telemetry=false)
  php artisan p:environment:setup "${a[@]}" || return 1
  php artisan p:environment:database --host=127.0.0.1 --port=3306 --database=panel \
    --username=pterodactyl --password="$dbpw" --no-interaction || return 1
  php artisan migrate --seed --force || return 1
  php artisan p:user:make --email="$email" --username="$user" --name-first=Admin --name-last=User \
    --password="$pass" --admin=1 --no-interaction || return 1
}

install_pterodactyl() {
  panel_guard || return 0
  local dir=/var/www/pterodactyl user pass tz dbpw url
  [ -e "$dir/artisan" ] && die "Pterodactyl is already installed in $dir"
  say "Pterodactyl panel installer"
  ask_domain_email
  user=$(ask "Admin username" "admin")
  [[ $user =~ ^[A-Za-z0-9_]{3,20}$ ]] || die "Username: 3-20 letters, digits or _"
  pass=$(ask_secret "Admin password (8-64 chars: letters digits @ . _ -)")
  [[ $pass =~ ^[A-Za-z0-9@._-]{8,64}$ ]] || die "Password must be 8-64 characters: letters, digits and @ . _ - only"
  tz=$(ask "Timezone" "$(cat /etc/timezone 2>/dev/null || echo UTC)")
  [[ $tz =~ ^[A-Za-z0-9_+/-]+$ ]] || die "Invalid timezone"

  step "Installing PHP, MariaDB, Nginx and Redis (a few minutes)" panel_packages
  dbpw=$(rand_pw)
  step "Creating the database" make_db panel pterodactyl "$dbpw"
  step "Downloading Pterodactyl" panel_download "$dir" https://github.com/pterodactyl/panel/releases/latest/download/panel.tar.gz
  step "Configuring Nginx" nginx_site pterodactyl "$P_DOMAIN" "$dir"
  try_ssl "$P_DOMAIN" "$P_EMAIL"
  url="$P_SCHEME://$P_DOMAIN"
  step "Setting up the panel (composer, database, admin user)" ptero_configure "$dir" "$P_EMAIL" "$url" "$tz" "$dbpw" "$user" "$pass"
  step "Starting cron and the queue worker" panel_finish pterodactyl "$dir" pteroq "--queue=high,standard,low --sleep=3 --tries=3"
  save_creds "Pterodactyl  $url  admin: $user ($P_EMAIL)  db: panel / pterodactyl / $dbpw"
  loading "Pterodactyl is ready"
  ok "Open $url and log in as $user"
  warn "Database password saved in $PANEL_CREDS (root only)."
  warn "Next: in the panel add a Location and a Node, then install Wings on your game server."
}

paymenter_configure() {  # dir url dbpw
  cd "$1" || return 1
  cp .env.example .env
  export COMPOSER_ALLOW_SUPERUSER=1
  composer install --no-dev --optimize-autoloader --no-interaction || return 1
  php artisan key:generate --force || return 1
  php artisan storage:link || true
  set_env .env APP_URL "$2"
  set_env .env DB_HOST 127.0.0.1
  set_env .env DB_PORT 3306
  set_env .env DB_DATABASE paymenter
  set_env .env DB_USERNAME paymenter
  set_env .env DB_PASSWORD "$3"
  php artisan migrate --force --seed || return 1
  php artisan db:seed --class=CustomPropertySeeder --force || return 1
}

install_paymenter() {
  panel_guard || return 0
  local dir=/var/www/paymenter dbpw url
  [ -e "$dir/artisan" ] && die "Paymenter is already installed in $dir"
  say "Paymenter installer"
  ask_domain_email

  step "Installing PHP, MariaDB, Nginx and Redis (a few minutes)" panel_packages
  dbpw=$(rand_pw)
  step "Creating the database" make_db paymenter paymenter "$dbpw"
  step "Downloading Paymenter" panel_download "$dir" https://github.com/paymenter/paymenter/releases/latest/download/paymenter.tar.gz
  step "Configuring Nginx" nginx_site paymenter "$P_DOMAIN" "$dir"
  try_ssl "$P_DOMAIN" "$P_EMAIL"
  url="$P_SCHEME://$P_DOMAIN"
  step "Setting up Paymenter (composer, database)" paymenter_configure "$dir" "$url" "$dbpw"

  say "Paymenter now asks two things. When it asks for the URL, type: $url"
  ( cd "$dir" && php artisan app:init </dev/tty ) || warn "app:init failed. Run it later: cd $dir && php artisan app:init"
  say "Create your admin account"
  ( cd "$dir" && php artisan app:user:create </dev/tty ) || warn "Run it later: cd $dir && php artisan app:user:create"

  step "Starting cron and the queue worker" panel_finish paymenter "$dir" paymenter ""
  save_creds "Paymenter  $url  db: paymenter / paymenter / $dbpw"
  loading "Paymenter is ready"
  ok "Open $url"
  warn "Database password saved in $PANEL_CREDS (root only)."
}

main() {
  banner
  verify_license
  local lines=("" "${CYN}  Cubaris Cloud installer${RST}" ""
    "  1) Install the bot"
    "  2) Set up the private network (vmbr1)"
    "  3) Network + bot"
    "  4) SSH gateway (one public port, token login)"
    "  5) Everything (network, bot, gateway)"
    "  6) WireGuard: route a public VPS IP to this node"
    "  7) Pterodactyl panel (own VPS recommended)"
    "  8) Paymenter billing (own VPS recommended)"
    "  9) Quit" "") l
  for l in "${lines[@]}"; do
    printf '%s\n' "$l"
    if [ "$IS_TTY" = 1 ]; then sleep 0.03; fi
  done
  case "$(ask "Choose" "5")" in
    1) install_bot ;;
    2) setup_network ;;
    3) setup_network; install_bot ;;
    4) install_gateway ;;
    5) setup_network; install_bot; install_gateway ;;
    6) setup_wireguard ;;
    7) install_pterodactyl ;;
    8) install_paymenter ;;
    *) exit 0 ;;
  esac
  ok "Done."
}
main
