#!/usr/bin/env python3
"""doctor.py - checks that config.yaml matches THIS node.  Run in the bot folder:  venv/bin/python doctor.py
It only reads things (and never prints token secrets)."""
import os
import subprocess

import yaml

cfg = yaml.safe_load(open("config.yaml", encoding="utf-8"))
problems = 0


def sh(cmd, timeout=20):
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout or "").strip()
    except Exception as e:  # noqa: BLE001
        return 1, str(e)


def check(ok, good, bad):
    global problems
    if ok:
        print("✅", good)
    else:
        problems += 1
        print("❌", bad)


if os.stat("config.yaml").st_mode & 0o077:
    print("⚠️  config.yaml is readable by other users (it holds tokens): chmod 600 config.yaml")

for c in cfg.get("clusters", []):
    print(f"\n== cluster {c['name']} ==")
    if c.get("backend", "proxmox") != "proxmox":
        print("(Incus cluster: only SSH is checked)")
    else:
        try:
            from proxmoxer import ProxmoxAPI
            api = ProxmoxAPI(c["host"], user=c["user"], token_name=c["token_name"],
                             token_value=c["token_value"], verify_ssl=False, timeout=10)
            nodes = api.nodes.get()
            check(True, "API token works: " + ", ".join(
                f"{n['node']} ({n['status']}, {(n['maxmem'] - n['mem']) / 2**30:.1f} GB free RAM)" for n in nodes), "")
        except ImportError:
            print("(proxmoxer not installed here - run this with venv/bin/python)")
        except Exception as e:  # noqa: BLE001
            check(False, "", f"API: {type(e).__name__} {str(e)[:110]}  -> the user/token doesn't exist on this node or the secret is wrong")

        rc, out = sh("pvesm status --content rootdir")
        stores = {}
        for line in out.splitlines()[1:]:
            f = line.split()
            if len(f) >= 3:
                stores[f[0]] = f[2]
        st = c.get("storage")
        check(stores.get(st) == "active", f"storage '{st}' is active",
              f"storage '{st}' is missing/inactive. Usable for containers here: "
              f"{', '.join(f'{k} ({v})' for k, v in stores.items()) or 'none'}  -> set storage: in config.yaml")

        br = c.get("bridge")
        rc, out = sh(f"ip -4 -o addr show dev {br}")
        rc2, _ = sh(f"ip link show dev {br}")
        if rc2 != 0:
            check(False, "", f"bridge '{br}' does not exist  -> create it (the vmbr1 block in /etc/network/interfaces) or change bridge:")
        elif c.get("subnet"):
            check(f"{c['gateway']}/" in out, f"bridge '{br}' has the gateway {c['gateway']}",
                  f"bridge '{br}' does not have the address {c['gateway']} (it has: {out or 'no IPv4'})  -> gateway/subnet in config.yaml must match the bridge")
        else:
            check(True, f"bridge '{br}' exists (DHCP mode)", "")

        if c.get("subnet"):
            rc, fwd = sh("sysctl -n net.ipv4.ip_forward")
            check(fwd == "1", "IP forwarding is on", "net.ipv4.ip_forward is 0  -> sysctl -w net.ipv4.ip_forward=1")
            rc, nat = sh("iptables -t nat -S POSTROUTING")
            check(any(c["subnet"] in l and "MASQUERADE" in l for l in nat.splitlines()),
                  f"NAT (MASQUERADE) exists for {c['subnet']}",
                  f"no MASQUERADE rule for {c['subnet']}: containers will have no internet  -> use the vmbr1 block that has the post-up iptables lines, then ifreload -a")

        if c.get("ssh_mode", "forward") == "forward":
            pi = c.get("public_iface")
            rc, _ = sh(f"ip link show dev {pi}")
            check(rc == 0, f"public_iface '{pi}' exists",
                  f"public_iface '{pi}' does not exist  -> use the interface from: ip route show default")
            ph = str(c.get("public_host") or "")
            check(ph and "example.com" not in ph, f"public_host is {ph}",
                  f"public_host is '{ph}': users would be told to SSH to a placeholder  -> put this node's public IP or your real DNS name")

        tstore = c.get("template_storage", "local")
        rc, out = sh(f"pveam list {tstore}")
        for key, t in (cfg.get("os_templates") or {}).items():
            check(t["file"] in out, f"template {t['file']} is downloaded",
                  f"template {t['file']} is NOT on '{tstore}'  -> pveam update; pveam download {tstore} {t['file']}  (or pick a name from: pveam available --section system)")

    key = (c.get("node_ssh") or {}).get("key_file")
    host = c.get("ssh_host", c.get("host"))
    if not key or not os.path.exists(key):
        check(False, "", f"SSH key file '{key}' does not exist  -> fix node_ssh.key_file")
    else:
        rc, _ = sh(f"ssh -i {key} -o BatchMode=yes -o ConnectTimeout=8 -o StrictHostKeyChecking=accept-new root@{host} true")
        check(rc == 0, f"bot can SSH to {host} with {key}", f"SSH to {host} with that key failed  -> add proxmox_bot_key.pub to /root/.ssh/authorized_keys there")

print("\n" + ("All good." if not problems else f"{problems} problem(s) found - fix the ❌ lines above, then restart the bot."))
