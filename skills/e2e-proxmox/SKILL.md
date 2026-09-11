---
name: e2e-proxmox
description: Create a fresh unprivileged Debian 13 LXC on a Proxmox VE node and test an exact NanoClaw commit through headless setup, a real agent reply, and service verification. Use for NanoClaw install or integration testing on Proxmox; preserve existing development containers.
license: MIT
---

# NanoClaw E2E on Proxmox

Use [scripts/proxmox-run.py](scripts/proxmox-run.py) from the root of the
**NanoClaw checkout under test**. It creates one new LXC and delegates application
setup to `e2e-exe-dev/scripts/e2e-install.sh`. The installer remains shared with
the exe.dev workflow; no NanoClaw setup logic is copied into this skill.

This follows the configuration of the
[NanoClaw development helper](https://github.com/glifocat/ProxmoxVED/tree/a01b31a1e932299b5883a26a4ddbef4cb2e8550d):
Debian 13, unprivileged LXC, `nesting=1,keyctl=1`, a `nanoclaw` developer account,
`/opt/nanoclaw`, and a lingering systemd user session. Defaults are 2 CPUs,
8 GiB RAM and a 40 GiB root disk. Proxmox documents `keyctl` as necessary for
[Docker in unprivileged containers](https://pve.proxmox.com/pve-docs/pct.conf.5.html).

The driver uses SSH to the **node**, then `pct exec` to reach the guest. It needs
no direct route to the LXC, guest SSH key, Proxmox API token or browser session.
The node runs Proxmox lifecycle commands; Docker and NanoClaw run inside the LXC.

## Install and prerequisites

The operator needs Python 3.10+, Git and OpenSSH. The target is a Proxmox VE
node with root SSH access; the first version supports Debian 13 amd64 guests.

Install both skills, or install the repository's Claude Code plugin, which
includes both:

```bash
npx skills add nanocoai/nanoclaw-oss-dev-tools --global --skill e2e-exe-dev
npx skills add nanocoai/nanoclaw-oss-dev-tools --global --skill e2e-proxmox
```

The shared installer is found in the sibling `e2e-exe-dev` directory. If the
skills live in different locations, pass `--installer` with its actual path.
Copy and symlink installations are both supported; the operator's NanoClaw
checkout is unrelated to either installation directory.

Before a live run, establish:

- A root SSH destination for the intended node, with its host key already
  verified. `--host` accepts an alias or `root@hostname`; port `8006` is the web
  interface, not the SSH port. A custom SSH port belongs in the operator's SSH
  configuration. `--identity-file` selects an existing key without consulting
  ssh-agent. The driver never enrolls keys or changes SSH configuration.
- An existing **Debian 13 amd64 OS template** from `pveam list local`, a storage
  with enough space for the root disk, and an existing bridge with DHCP, DNS and
  outbound access. The driver doesn't download templates or change networking.
- An Anthropic API key or OAuth token in a local file, normally
  `~/.nanoclaw-e2e/anthropic_key`, with permissions `0600`.
- A NanoClaw commit that the guest can fetch from a public HTTPS repository.
  `--ref` resolves locally; fetch locally first to test fresh upstream code.
  GitHub SSH origins are converted to HTTPS. Private repository authentication
  and local-only commits are outside this first version.

Read-only inventory commands on the node include `pveversion`, `pct list`,
`pveam list local`, `pvesm status`, and `ip -brief link show type bridge`.
Choose the host, template, storage and bridge from actual inventory. Never use
an existing personal/development guest as a disposable test target.

## Run

Set `PROXMOX_SKILL_DIR` to the directory containing this `SKILL.md`. From the
NanoClaw checkout, first inspect the plan:

```bash
python3 "$PROXMOX_SKILL_DIR/scripts/proxmox-run.py" \
  --host root@pve.example.test \
  --template local:vztmpl/debian-13-standard_13.6-1_amd64.tar.zst \
  --storage local-lvm --bridge vmbr0 \
  --ref origin/main \
  --result-file /path/to/proxmox-result.json --dry-run
```

Replace the example infrastructure values with the inspected ones. The report's
parent directory must exist. Dry run resolves the commit and records `planned`;
it performs no SSH calls and reads no credentials. Remove `--dry-run` to create
the LXC and run the test within the user's authorized scope.

The driver asks Proxmox for the next available guest ID, or accepts an explicit
unused `--ctid`. Proxmox still arbitrates allocation if another task races for
that ID. Only a successful create operation plus the matching random run marker
and required settings authorizes guest operations. An uncertain create result
stops the run without adopting, starting or writing into a discovered guest.

The new guest receives a developer account with passwordless sudo, scoped to
that disposable container, so the shared installer can perform NanoClaw's setup
steps. The bootstrap waits for DNS, rejects incomplete APT index refreshes, and
adds Docker-group membership before starting the user manager. A manager started
earlier retains stale groups even when a new interactive shell can use Docker.
Credentials and the documented environment settings travel over SSH stdin
into private files. The installer sets up Docker, OneCLI, credentials, the agent
image, the service and a CLI agent, then requires a real reply and setup
verification. Existing channels are not connected.

Supported installer settings are `NANOCLAW_ONECLI_API_HOST`,
`NANOCLAW_ONECLI_API_TOKEN`, `NANOCLAW_DISPLAY_NAME`, `NANOCLAW_E2E_TZ` and
`NANOCLAW_E2E_FORCE_AUTH`. `--key-file` or `NANOCLAW_E2E_KEY_FILE` chooses the
local credential file. This workflow retains its CLI agent for verification.

## Results and retained guests

Read the local JSON report, including `status`, `phase`, `requested_commit`,
`commit`, `guest.ctid` and the nested `installer` result. A pass requires the
installer's exact commit, successful exit, `ping: ok`, and completed verification
to match this run. `commit` stays null until a completed installer result proves
which revision ran. The top-level exit code is the driver's exit; the nested
installer retains its original fields and timings.

After argument parsing and result-path checks, a new `running` report replaces
an earlier pass before preflight. A normal failure records its phase and exit
code. Abrupt termination may leave `running`; a timeout or lost SSH connection
can leave a remote operation in progress. Inspect the recorded guest and its
task state before retrying. Do not adopt an uncertain guest on a retry.

Every created LXC is retained on success or failure, with its ID printed. There
is no automatic delete, clone, template conversion or change to existing guests.
Inspect the test guest from the node with `pct enter <CTID>`, then
`machinectl shell nanoclaw@` and `cd /opt/nanoclaw`. Its detailed installer logs
and result are at `/opt/nanoclaw/logs/e2e/`. It contains test credentials in the
OneCLI vault; treat it accordingly when choosing to retain or remove it.
Cleanup needs a separately scoped request identifying the test CT.

## Source contracts and validation

The shared installer follows NanoClaw
[`74224f62a6c08418acccc727114ab02f92e403bf`](https://github.com/nanocoai/nanoclaw/tree/74224f62a6c08418acccc727114ab02f92e403bf):
`setup.sh`, `setup/install-docker.sh`, `setup/index.ts`, `setup/service.ts`,
`setup/verify.ts`, `scripts/init-cli-agent.ts`, and `scripts/chat.ts`.
Re-check these when NanoClaw's setup or reply classification changes. Proxmox's
[pct command reference](https://pve.proxmox.com/pve-docs/pct.1.html) defines guest
creation and execution; no community-script engine is required by this driver.

The offline suite exercises allocation and identity checks, failed bootstrap,
exact commit selection, credential transport, copied skill paths and result
validation through simulated SSH. These tests do not establish nested Docker
or real inference compatibility. Record a live run's Proxmox and kernel version,
template, tested NanoClaw SHA and installer result before claiming this driver
works on a host. The driver checks initial setup and service health; it does not
yet test reboot recovery, cached clones or the transactional updater.

## Compatibility evidence

A fresh live run passed on **2026-09-11** with the following configuration:

| Component | Verified value |
|---|---|
| Proxmox VE | `9.2.18`, build `614bede5d65599c6` |
| Host kernel | `7.0.14-16-pve` |
| Guest template | `debian-13-standard_13.6-1_amd64.tar.zst` |
| LXC | Unprivileged, `nesting=1,keyctl=1`, 2 CPUs, 8 GiB RAM, 40 GiB disk |
| NanoClaw commit | `74224f62a6c08418acccc727114ab02f92e403bf` |
| Installer result | `status: pass`, `exit_code: 0`, `ping: ok`, `phase: complete` |
| Service | `systemd-user`, active and running, linger enabled |

The run completed in about four minutes, returned a real model reply, and passed
NanoClaw's final verification. A subsequent inspection confirmed healthy OneCLI
and PostgreSQL containers and removal of the temporary credential file. The
guest and its OneCLI vault were retained. This establishes fresh-install
compatibility for this configuration; reboot recovery was not exercised.

Earlier attempts exposed Proxmox's encoded description newline, transient guest
DNS/APT failures, and stale Docker membership in an already-running user manager.
The driver and bootstrap handle those cases, with offline regressions for each.
