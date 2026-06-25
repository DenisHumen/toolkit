<p align="center">
  <img src="assets/logo.svg" alt="toolkit logo" width="560">
</p>

<p align="center">
  <b>A growing collection of scripts for very different tasks.</b><br>
  <sub>System administration · automation · one-off helpers — each script documented and ready to run.</sub>
</p>

<p align="center">
  <a href="README.md">🇬🇧 English</a> &nbsp;•&nbsp; <a href="README.ru.md">🇷🇺 Русский</a>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/Shell-Bash-4EAA25?logo=gnubash&logoColor=white" alt="Bash">
  <img src="https://img.shields.io/badge/Platform-Linux%20%7C%20Proxmox-1793D1?logo=linux&logoColor=white" alt="Platform">
  <img src="https://img.shields.io/badge/Status-Actively%20growing-2ea44f" alt="Status">
</p>

---

## 📖 About

`toolkit` is a single home for standalone scripts that each solve an unrelated problem — from
server maintenance to quick automation helpers. There is no shared framework: every script is
self-contained, documented below, and safe to copy out and run on its own.

This README is the project's front page and grows together with the repository: **every new
script gets its own block** with a short description and the commands to run it.

## 🧰 Scripts

| Script | Category | What it does |
|---|---|---|
| [`proxmox-wipe.sh`](#proxmox-wipesh) | Proxmox | Destroys all guests and zeroes every non-system disk, with a live progress bar + ETA. |
| [`install-docker.sh`](#install-dockersh) | Linux | Auto-detects the distro and installs Docker Engine + Compose v2 from Docker's official repos. |
| [`install-pingvin-share.sh`](#install-pingvin-sharesh) | Linux | Deploys Pingvin Share via Docker behind a Caddy reverse proxy with automatic HTTPS, and opens the firewall. |

> 📌 This table grows as new scripts are added.

---

### `proxmox-wipe.sh`

> 🧨 Destroy every VM/CT and **zero all non-system disks** on a Proxmox host — safely, with a live progress bar and ETA.

**Location:** [`proxmox/proxmox-wipe.sh`](proxmox/proxmox-wipe.sh)

System disks backing `/`, `/boot` and `/boot/efi` are auto-detected by two independent methods
and protected; if detection finds nothing valid, the script aborts instead of guessing. Data
disks are erased with `dd` (live progress bar + ETA) or, with `--discard`, a fast hardware zero.

#### ▶️ Run

```bash
chmod +x proxmox/proxmox-wipe.sh
sudo ./proxmox/proxmox-wipe.sh --dry-run
```

#### Commands

| Command | Purpose |
|---|---|
| `./proxmox-wipe.sh --dry-run` | **Preview only.** Prints the `[KEEP]` / `[WIPE]` disk lists and the guests that would be removed — nothing is changed. Always run this first. |
| `./proxmox-wipe.sh --only sdb,sdc,sdd,sde --dry-run` | Preview a wipe restricted to the named disks (recommended, safest). |
| `./proxmox-wipe.sh --only sdb,sdc,sdd,sde` | Wipe **only** the explicitly named disks. |
| `./proxmox-wipe.sh` | Wipe **all** non-system disks on the host. |

#### Options

| Flag | Description |
|---|---|
| `-n`, `--dry-run` | Preview every action without changing anything. |
| `--only sdX,sdY` | Restrict the wipe to an explicit comma-separated disk list; a system disk in the list is rejected. |
| `-y`, `--yes` | Skip the interactive confirmation prompt. |
| `--discard` | Use `blkdiscard -z` for a fast hardware zero (no progress bar); falls back to `dd` if unsupported. |
| `-h`, `--help` | Print the script's built-in help. |

> ⚠️ **Destructive and irreversible.** Must run as `root`. It permanently destroys every VM/CT and
> zeroes the listed disks — there is **no undo**. Without `--yes` you must type `ERASE-ALL-DATA`
> to proceed. A full log is written to `/var/log/proxmox-wipe-*.log`.

---

### `install-docker.sh`

> 🐳 One installer for **Docker Engine + Docker Compose v2** that detects the distro and runs the matching official path — apt on Ubuntu/Debian, dnf on Fedora/RHEL/CentOS.

**Location:** [`linux/install-docker.sh`](linux/install-docker.sh)

Reads `/etc/os-release` to pick the right package manager and Docker repository, then installs the
same official package set everywhere — `docker-ce`, `docker-ce-cli`, `containerd.io`,
`docker-buildx-plugin` and `docker-compose-plugin` (Compose v2, used as `docker compose`). It also
enables the service and adds your user to the `docker` group. Mirrors the official
[docs.docker.com](https://docs.docker.com/engine/install/) steps.

#### ▶️ Run

```bash
chmod +x linux/install-docker.sh
sudo ./linux/install-docker.sh --dry-run
```

#### Commands

| Command | Purpose |
|---|---|
| `./install-docker.sh --dry-run` | **Preview only.** Prints the detected distro and every command that would run — nothing is changed. Run this first. |
| `./install-docker.sh` | Install Docker after an interactive confirmation prompt. |
| `./install-docker.sh --yes` | Install non-interactively (assume "yes") — handy for provisioning. |

#### Options

| Flag | Description |
|---|---|
| `-n`, `--dry-run` | Preview every step without changing anything. |
| `-y`, `--yes` | Skip the confirmation prompt (non-interactive). |
| `--no-start` | Do not enable/start the `docker` systemd service. |
| `--no-group` | Do not add the current user to the `docker` group. |
| `-h`, `--help` | Print the script's built-in help. |

> 💡 Supported: **Ubuntu / Debian** (apt) and **Fedora / RHEL / CentOS** (dnf). Run as `root` or with
> `sudo`. After install, log out/in (or run `newgrp docker`) to use Docker without `sudo`, then verify
> with `docker run hello-world`.

---

### `install-pingvin-share.sh`

> 🐧 One installer that deploys **Pingvin Share** (self-hosted file sharing) with Docker Compose, served on **your own domain with automatic HTTPS**, firewall and all.

**Location:** [`linux/install-pingvin-share.sh`](linux/install-pingvin-share.sh)

Detects the distro family, makes sure Docker + Compose v2 are present — and if they are not, it runs
[`install-docker.sh`](#install-dockersh) from this same repo (the local sibling file, or downloaded
from GitHub). By default it writes a stack with **Pingvin Share + a Caddy reverse proxy** that obtains
a free Let's Encrypt certificate for your `--domain` automatically (no certbot, no manual renewals),
sets `TRUST_PROXY=true`, opens **80/443** in the firewall (`ufw` on apt distros, `firewalld` on dnf),
labels volumes for SELinux, then brings everything up. Use `--no-proxy` to publish Pingvin Share
directly on a port instead (no TLS — for use behind an existing proxy).

#### ▶️ Run

```bash
chmod +x linux/install-pingvin-share.sh
# Internet-facing, HTTPS on your domain (preview first):
sudo ./linux/install-pingvin-share.sh --domain share.example.com --email you@example.com --dry-run
sudo ./linux/install-pingvin-share.sh --domain share.example.com --email you@example.com
```

> Point your domain's **A/AAAA record at the server** and make sure ports **80 + 443** are reachable
> from the internet *before* running — Caddy needs them to issue the certificate.

#### Commands

| Command | Purpose |
|---|---|
| `./install-pingvin-share.sh --domain d.tld --dry-run` | **Preview only.** Prints the detected distro and every file/command — nothing is changed. Run this first. |
| `./install-pingvin-share.sh --domain d.tld --email you@d.tld` | Install behind Caddy with automatic HTTPS for `d.tld`. |
| `./install-pingvin-share.sh --no-proxy --port 3000` | Install Pingvin Share only, published directly on a port (no TLS). |

#### Options

| Flag | Description |
|---|---|
| `-d`, `--domain <fqdn>` | Domain to serve on (required unless `--no-proxy`). |
| `-e`, `--email <addr>` | Email for Let's Encrypt / ACME (recommended in proxy mode). |
| `-p`, `--port <port>` | Host port for direct mode (default `3000`, only with `--no-proxy`). |
| `--dir <path>` | Install directory (default `/opt/pingvin-share`). |
| `--image <ref>` | Container image (default `stonith404/pingvin-share`). |
| `--no-proxy` | Skip Caddy/HTTPS, publish Pingvin Share directly on `--port`. |
| `--no-firewall` | Do not touch the firewall. |
| `-n`, `--dry-run` | Preview every step without changing anything. |
| `-y`, `--yes` | Skip the confirmation prompt (non-interactive). |
| `-h`, `--help` | Print the script's built-in help. |

> 💡 Supported: **Ubuntu / Debian** and **Fedora / RHEL / CentOS**. Run as `root` or with `sudo`. The
> first account you register becomes the admin; afterwards open **Configuration** and set the *App URL*
> (`https://<domain>`) and the max share size. Manage the stack from the install dir with
> `docker compose logs -f`, `docker compose pull && docker compose up -d` (update), `docker compose down` (stop).

---

## 🗂 Repository structure

```text
toolkit/
├── assets/
│   └── logo.svg
├── linux/
│   ├── install-docker.sh
│   └── install-pingvin-share.sh
├── proxmox/
│   └── proxmox-wipe.sh
├── README.md        # English (this file)
└── README.ru.md     # Русский
```


---

<p align="center"><sub>⚠️ Use these scripts at your own risk. Review the source before running anything that touches disks or data.</sub></p>
