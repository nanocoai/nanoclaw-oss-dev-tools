# Preparing a Windows test environment

Read this when the requested Windows test needs infrastructure. These are
preparation and evidence requirements, not an unattended VM lifecycle driver.
Keep live credentials and host-specific VM receipts outside this repository.

## Windows and Docker prerequisites

Use Windows 11 and a fresh WSL2 Ubuntu distribution for the initial scenario.
For a VM, first verify nested virtualization on the physical host and in the
guest. A powered-on Windows desktop or installed WSL package does not prove
that the nested Linux VM can run.

Use the official [Windows Enterprise Evaluation download](https://www.microsoft.com/en-us/evalcenter/download-windows-11-enterprise)
when the operator has selected evaluation media. Verify its exact SHA-256
against Microsoft's published checksum for that download. Keep the ISO in
the hypervisor's ISO storage, commonly `/var/lib/vz/template/iso` on Proxmox.
An ISO image and a reusable installed VM template are different artifacts.

The live preparation used a new VM with 4 host-type vCPUs, 16 GiB RAM,
a 120 GiB SATA boot disk, Q35, OVMF, Secure Boot, a TPM 2.0 state disk,
VirtIO networking and the QEMU guest agent. SATA avoided an additional storage
driver during Windows PE. It used a UTC virtual RTC and Windows UTC settings.
Check the installed Proxmox CLI before constructing equivalent commands.

Before any boot or destructive Windows installer action, prove that the VM ID,
random ownership marker and writable volumes belong to this test. Never adopt
an existing guest by name alone. Preserve the ownership receipt and recheck it
before guest execution, console input, cloning or template conversion.

Create a dedicated local Windows test account. If using an answer file, keep
its generated password on the hypervisor with private permissions; do not put
passwords in controller command arguments or publish the answer file. Keep
temporary automatic logon limited to setup. No Microsoft account or Docker
account login was necessary for the qualified path.

## Clean Windows template

Capture a base before installing WSL distributions, Docker, NanoClaw or model
credentials. Remove temporary test accounts, profiles, autologon secrets and
cached private answer files before capture, while retaining the guest agent.
Keep a separate, fresh per-clone answer file when using unattended setup.

Run Sysprep with generalization, OOBE and shutdown. Inspect its actual result
and Windows setup state before converting the stopped VM to a template:

- A zero launcher exit code alone is insufficient. Verify
  `IMAGE_STATE_GENERALIZE_RESEAL_TO_OOBE` and successful Sysprep logs.
- Automatic disk encryption can prevent generalization (`0x80310039` in the
  live run). Do not disable protection or decrypt a disk without scoped
  authorization. After authorized decryption, verify fully decrypted state
  before trying Sysprep again.
- Preserve evaluation licensing with `SkipRearm=1`; this does not extend the
  evaluation period. Record activation and expiration limits.
- Detach password-bearing media before capture. Retire obsolete seed ISOs and
  node-side credential files only after checking all guest references.

For each clone, verify the expected computer name, a new SID, its own fresh
readiness record, normal logged-in desktop, `IMAGE_STATE_COMPLETE`, networking
and the guest agent. A readiness file inherited from the template is stale.

The 2026-09-11 template used an explicit `F:\Autounattend.xml` selection and
UTC hardware clock. Its clone still displayed "Why did my PC restart?" on
first boot. Selecting the visible Next button completed setup. The remaining
root cause is unproven: describe this template as needing console assistance,
not as a qualified fully unattended installation. Do not script unknown
screen coordinates or change setup-state registry flags to manufacture success.

## WSL and the Windows user boundary

Follow Microsoft's [WSL installation instructions](https://learn.microsoft.com/en-us/windows/wsl/install).
From the dedicated Windows user, install WSL and reboot when requested:

```powershell
wsl --install --no-distribution --web-download
```

After the normal Windows desktop returns, install and launch Ubuntu:

```powershell
wsl --install --distribution Ubuntu-24.04 --web-download
```

Complete its regular Linux user setup. Verify `wsl --list --verbose`, execute
Linux `uname -r`, and confirm systemd is PID 1. Follow Microsoft's
[systemd guidance](https://learn.microsoft.com/en-us/windows/wsl/systemd)
if configuration is needed; merge settings into `/etc/wsl.conf` rather than
overwriting unrelated sections.

WSL distributions belong to the Windows user. QEMU guest execution runs as
SYSTEM, whose WSL inventory differs from the logged-in test account's.
For automation, dispatch a bounded scheduled task under the existing interactive
test user, record its task/process identity, and inspect the result before
retrying an ambiguous launch. Do not solve this by installing a second distro
under SYSTEM or opening additional remote-login services.

Install only OS and test-harness prerequisites before the public wizard: Git,
Python/venv, curl/CA certificates, sudo, build tools, and user-session packages.
The regular Linux test user needs noninteractive sudo for this disposable
scenario and an active `systemctl --user` session. Use a scoped test sudo rule
with mode 0440 and validate it with `visudo`; use linger when required by the
test session. Verify those conditions before invoking NanoClaw.

## Docker Desktop and credentials

Install Docker Desktop from its [official Windows download](https://docs.docker.com/desktop/setup/install/windows-install/),
checking the publisher signature and available download checksum. Start it
as the Windows account that owns Ubuntu and select its WSL2 backend.
Grant only the distribution integration within the operator's authorized scope:
Settings > Resources > WSL integration > Ubuntu-24.04 > Apply.
Follow Docker's [WSL integration documentation](https://docs.docker.com/desktop/features/wsl/).

Prove a local Windows Linux-container run first. Then use the skill's
`--preflight-only` check inside Ubuntu to compare engine identities and test
a Linux-home bind mount. Docker Offload, a remote context, an Ubuntu-installed
daemon, or a hand-made socket link is not equivalent evidence.

Docker documents [supported VM/VDI environments](https://docs.docker.com/desktop/setup/vm-vdi/).
The Proxmox combination here is an experimental compatibility result, not
a vendor support claim.

Transfer only a credential authorized for this particular test machine and
model use. Use a private transport such as pinned SSH stdin, never command-line
arguments or echoed terminal input. If Windows staging is necessary, restrict
its ACL to SYSTEM and the dedicated test account before writing the secret.
Install the Linux copy exclusively with mode 0600 under a private directory,
verify its owner/mode, then remove and verify removal of the Windows staging
copy. Do not transfer raw product auth logs back to the controller.

## Recovery observations

Record recovery independently of a fresh-install pass. In the qualified VM,
NanoClaw survived the setup terminal closing. Terminating Ubuntu and starting
a normal session then left its Docker socket absent, and the enabled service
failed to return within 60 seconds. After a full Windows reboot, Docker Desktop
was still absent after 109 seconds. A normal Docker Desktop launch restored
its local engine, Ubuntu integration and NanoClaw's existing service/socket,
without changing product configuration. A subsequent retained-agent request
still timed out (CLI exit 3). Therefore neither unattended recovery nor
post-reboot inference passed. Preserve the failure instead of treating a
working socket or environment-only preflight as proof of model inference.
