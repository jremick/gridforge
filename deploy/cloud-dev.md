# Cloud-Based Development Setup (Zero Local Docker / PyQGIS)

GridForge's headless PyQGIS 3.40 LTR + GDAL 3.10 + Node 22 build and test pipeline runs fastest—and keeps your laptop completely free of Docker Desktop RAM/disk overhead—on a **native Linux cloud dev VM** accessed privately over **Tailscale SSH**, paired with **GitHub Actions CI/Release offloading**.

---

## Architecture

1. **Linux Cloud Dev VM (GCP Compute Engine or Hetzner Cloud):** Runs native Linux Docker Engine, Git worktrees, PyQGIS test containers, Vite dev server, and AI coding agents.
2. **Tailscale SSH + Loopback Port Forwarding:** Private encrypted access with zero public SSH ports opened on the internet, preserving GridForge's `127.0.0.1` local-first security default.
3. **Antigravity Remote SSH:** Your laptop runs only the lightweight desktop UI; builds, tests, and agents execute remotely and persist even if your laptop sleeps.
4. **GitHub Actions + Railway Offloading:** Offline CI, on-demand live open-data E2E runs (`GRIDFORGE_RUN_LIVE=1`), multi-arch GHCR builds, and 300–600 DPI release sample PDF rendering all run in the cloud.

---

## 1. Provision a Linux Dev VM

Recommended sizing for snappy PyQGIS renders and parallel Vite + Docker workflows:

- **CPU / RAM:** `4–8 vCPU`, `16–32 GB RAM`
- **Disk:** `80–100 GB` SSD (plenty of room for Docker BuildKit cache, sample renders, and regional packs)
- **OS:** Ubuntu 24.04 LTS or Debian 12/13

### Option A — Google Cloud Compute Engine (`australia-southeast2`)

Matches the region used in [`cloudbuild.yaml`](../cloudbuild.yaml):

```bash
gcloud compute instances create gridforge-dev \
  --project="<your-gcp-project>" \
  --zone="australia-southeast2-a" \
  --machine-type="e2-standard-4" \
  --boot-disk-size="100GB" \
  --boot-disk-type="pd-balanced" \
  --image-family="ubuntu-2404-lts-amd64" \
  --image-project="ubuntu-os-cloud"
```

> **Cost tip:** On pay-per-minute GCP VMs, pass `--idle-shutdown-minutes 60` in Step 2 so the VM powers itself off when you step away. You only pay pennies per month for the persistent disk while stopped, and can wake it with `gcloud compute instances start gridforge-dev --zone="australia-southeast2-a"`.

### Option B — Hetzner Cloud (Flat-Rate VPS)

Provision a `CPX31` / `CPX41` (4–8 vCPU, 8–16 GB RAM) or `CAX31` (`arm64`, 8 vCPU, 16 GB RAM) running Ubuntu 24.04 or Debian 12/13. Leave `--idle-shutdown-minutes` at `0` (default) for flat-rate VPS instances.

---

## 2. Bootstrap Docker, Node 22, `gh`, and Tailscale

SSH into the fresh VM (via `gcloud compute ssh` or your VPS provider console), clone the repo, and run [`scripts/bootstrap_cloud_dev.sh`](../scripts/bootstrap_cloud_dev.sh):

```bash
git clone https://github.com/jremick/gridforge.git
cd gridforge

# On flat-rate VPS (e.g. Hetzner):
sudo ./scripts/bootstrap_cloud_dev.sh --user "$USER"

# Or on pay-per-minute cloud VMs (e.g. GCP) with 60-minute idle auto-shutdown:
sudo ./scripts/bootstrap_cloud_dev.sh --user "$USER" --idle-shutdown-minutes 60
```

Join your private Tailnet with **Tailscale SSH** enabled:

```bash
sudo tailscale up --ssh
```

Verify Docker and Node without `sudo` (start a fresh login shell or run `newgrp docker`):

```bash
docker build --target test -t gridforge:dev .
docker run --rm -v "$(pwd):/app" gridforge:dev pytest -q tests/unit tests/integration tests/e2e
```

Once Tailscale SSH is verified, you can close public TCP port `22` in your cloud firewall so the VM is reachable exclusively over your Tailnet.

---

## 3. Connect via Antigravity SSH Host

Add the VM to your local OpenSSH config on your laptop:

```sshconfig
Host gridforge-dev
  HostName <your-dev-vm>.<your-tailnet>.ts.net
  User <your-linux-user>
  ServerAliveInterval 30
  ServerAliveCountMax 6
  LocalForward 127.0.0.1:8080 127.0.0.1:8080
  LocalForward 127.0.0.1:5173 127.0.0.1:5173
```

Then open **Antigravity → Projects → SSH Host… → `gridforge-dev`** and open your cloned `gridforge` directory.

- All Git operations, agent commands, Docker builds, and PyQGIS renders now execute on the Linux VM.
- You can stop and uninstall Docker Desktop on your laptop.

---

## 4. Preview Studio (`8080`) & Vite (`5173`) Privately

With the `LocalForward` entries above (or Antigravity's built-in port forwarding), simply start the stack on the VM:

```bash
# Terminal 1 on VM: FastAPI + headless PyQGIS worker (bound to 127.0.0.1:8080)
docker compose up -d

# Terminal 2 on VM: Vite hot-reload dev server (bound to 127.0.0.1:5173)
npm --prefix web run dev
```

Open `http://127.0.0.1:8080` or `http://127.0.0.1:5173` in your laptop's browser.

To also open the Studio UI from phones or field tablets on your Tailnet over encrypted HTTPS, use `tailscale serve` as documented in [`deploy/tailscale-serve.md`](tailscale-serve.md).

---

## 5. Cloud CI, Live E2E & Release Sample Offloading

Heavy verification and release tasks run directly on GitHub-hosted runners so you never need to run multi-arch builds or 600 DPI renders locally:

- **Offline CI (automatic on every push & PR):** [`.github/workflows/ci.yml`](../.github/workflows/ci.yml) runs frontend typecheck/tests/build, release preflight, and the full headless PyQGIS offline test suite with GitHub Actions BuildKit caching.
- **Live multi-region open-data E2E (`GRIDFORGE_RUN_LIVE=1`):** Trigger on demand from any device via the GitHub CLI or Actions UI:
  ```bash
  gh workflow run ci.yml -f run_live=true
  ```
- **Multi-arch GHCR Release + 300–600 DPI Sample PDFs:** Pushing an annotated `vX.Y.Z` tag (or running `gh workflow run release.yml -f tag=vX.Y.Z -f render_samples=true`) builds & pushes `linux/amd64` + `linux/arm64` to GHCR and runs [`scripts/render_release_samples.sh`](../scripts/render_release_samples.sh) directly on GitHub's runner, uploading the 5 verified PDFs as workflow artifacts and attaching them to the GitHub Release if it already exists.
- **Live Hosted-Mode PR Previews (optional):** With [`railway.toml`](../railway.toml) already configured, enabling **Railway PR Environments** deploys an ephemeral hosted Studio preview URL for every pull request.

---

## 6. Portable Dev Container (Codespaces / DevPod)

If you prefer zero-VM ephemeral workspaces, [`.devcontainer/devcontainer.json`](../.devcontainer/devcontainer.json) builds directly from the `test` target of [`Dockerfile`](../Dockerfile) (Debian Trixie + PyQGIS 3.40 LTR + GDAL 3.10 + Node 22 + Docker-in-Docker + `gh`) and works identically in **GitHub Codespaces** and **DevPod**.

---

## 7. Maintenance & Disk Hygiene

Keep BuildKit cache bounded over time on your cloud VM:

```bash
docker builder prune --keep-storage 15GB -f
docker image prune -f
```
