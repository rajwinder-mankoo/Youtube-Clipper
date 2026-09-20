# Proxmox deployment

This guide runs YouTube Clipper continuously in an Ubuntu Server VM and keeps
the dashboard private through Tailscale.

## Recommended VM

| Resource | Starting value |
|---|---|
| OS | Ubuntu Server 24.04 LTS |
| CPU | 6 vCPUs, CPU type `host` |
| Memory | 16 GB |
| System disk | 40-64 GB on SSD-backed storage |
| Media disk | 500 GB or more on bulk storage |
| Network | VirtIO on the normal Proxmox bridge |

Enable the QEMU Guest Agent in Proxmox and install it in the guest:

```bash
sudo apt update
sudo apt full-upgrade -y
sudo apt install -y qemu-guest-agent openssh-server
sudo systemctl enable --now qemu-guest-agent
```

## System dependencies

```bash
sudo apt install -y python3 python3-venv python3-pip ffmpeg tesseract-ocr \
  git curl unzip build-essential libgl1 libglib2.0-0
```

Install a current Deno binary using the official Deno instructions, then
verify the toolchain:

```bash
python3 --version
ffmpeg -version
tesseract --version
deno --version
```

## Service account and application

```bash
sudo adduser --system --group --home /srv/youtube-clipper ytclipper
sudo mkdir -p /srv/youtube-clipper/app
sudo chown -R ytclipper:ytclipper /srv/youtube-clipper
sudo -u ytclipper git clone https://github.com/rajwinder-mankoo/Youtube-Clipper.git \
  /srv/youtube-clipper/app
sudo -u ytclipper python3 /srv/youtube-clipper/app/scripts/bootstrap.py
sudo -u ytclipper /srv/youtube-clipper/app/.venv/bin/python \
  /srv/youtube-clipper/app/scripts/doctor.py
```

For larger installations, mount a separate filesystem at
`/mnt/youtube-data`, create `input`, `output`, `cache`, `logs`, and `music`
inside it, and link those directories into the application root. Confirm the
target disk before formatting or changing `/etc/fstab`.

## Credentials

For dashboard account connection, create a Google OAuth **Web application**
credential, save it as `/srv/youtube-clipper/app/oauth_web_client.json`, and
register the exact Tailscale callback displayed on the Accounts page. You can
then complete authorization from any browser connected to your tailnet.

Restrict every credential file:

```bash
sudo chown ytclipper:ytclipper /srv/youtube-clipper/app/oauth_web_client.json
sudo chmod 600 /srv/youtube-clipper/app/oauth_web_client.json
```

Apply the same ownership and mode to token files.

## Verification

```bash
cd /srv/youtube-clipper/app
sudo -u ytclipper .venv/bin/python -m compileall -q .
sudo -u ytclipper .venv/bin/python test_isolation.py
sudo -u ytclipper .venv/bin/python dashboard.py
```

From another terminal in the VM:

```bash
curl http://127.0.0.1:8765/api/state
```

Stop the manual dashboard after the response succeeds.

## systemd service

Create `/etc/systemd/system/youtube-clipper.service`:

```ini
[Unit]
Description=YouTube Clipper Dashboard
Wants=network-online.target
After=network-online.target tailscaled.service

[Service]
Type=simple
User=ytclipper
Group=ytclipper
WorkingDirectory=/srv/youtube-clipper/app
Environment=PYTHONUNBUFFERED=1
Environment=PATH=/srv/youtube-clipper/app/.venv/bin:/usr/local/bin:/usr/bin:/bin
ExecStart=/srv/youtube-clipper/app/.venv/bin/python /srv/youtube-clipper/app/dashboard.py
Restart=on-failure
RestartSec=5
UMask=0077
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
```

Enable and inspect it:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now youtube-clipper
sudo systemctl status youtube-clipper
sudo journalctl -u youtube-clipper -f
```

## Private Tailscale access

Install Tailscale in the VM and join the tailnet. Keep the dashboard on its
default `127.0.0.1:8765` address, then proxy it privately:

```bash
sudo tailscale serve --bg http://127.0.0.1:8765
sudo tailscale serve status
```

Use Tailscale access controls so only trusted identities can reach the VM. Do
not use public port forwarding or Tailscale Funnel for this dashboard.

## First production test

1. Generate one Short.
2. Choose **Prepare upload** and inspect the dry-run review.
3. Set visibility to **Private**.
4. Approve the upload.
5. Confirm the result in YouTube Studio and inspect the service log.

## Backups

Back up the VM configuration and application code separately from large media.
At minimum, protect account configuration, OAuth files, `cache/`, and `output/`.
Treat every backup containing credentials as sensitive.
