# macOS test environments

Decision recorded: **2026-09-11**. MacinCloud is **parked**. Use available
operator-owned physical Macs for additional coverage. An authorized SSH test on
an M4 Pro Mac mini passed with the readiness fix now merged in plugin 0.4.1.

## Options considered

These are options for macOS testing, not additional implemented skills. The
[skill catalog](skills-catalog.md) lists the three tools that actually exist.

| Option | Potential use | Decision and limits |
|---|---|---|
| Available physical Macs, local or SSH | Native Docker and LaunchAgent testing; add different CPU/OS combinations using hardware already available. | **Preferred path.** Local arm64 baseline passed; SSH on M4 Pro also passed with the readiness fix now merged in 0.4.1. Inventory additional hardware/OS combinations before testing them. |
| Local macOS VM using Tart | Repeatable macOS guests on Apple silicon, useful for isolated OS/application checks. | Deferred for the full Docker E2E. Tart currently documents nested virtualization for Linux guests only, so a macOS guest cannot provide the required local Docker Linux VM through this route. A remote Docker arrangement would be a different test needing its own validation. [Tart FAQ](https://tart.run/faq/#nested-virtualization-support) |
| macOS VM on Proxmox | Community OpenCore/Hackintosh route for x86 macOS experiments. | Not selected; no VM created. Adds bootloader and nested-virtualization work and does not establish Apple silicon coverage. This is separate from our tested **Linux LXC** skill. [Community project](https://github.com/luchina-gabriel/OSX-PROXMOX) |
| MacinCloud Dedicated | Advertised Intel and older macOS choices; root access for a configurable test host. | **Parked.** Live inventory and Docker suitability did not establish the intended older configuration. Detailed findings below. |
| AWS EC2 Mac | Bare-metal Intel and Apple silicon hosts with selectable macOS AMIs; candidate for an automated compatibility matrix. | Deferred. Minimum host allocation is 24 hours. Verify the exact host/AMI pairing, firmware, region, capacity and full cost before rental. OS and hardware cannot be mixed arbitrarily. [Host constraints](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/ec2-mac-instances.html), [AMI compatibility](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/macos-ami-overview.html) |
| Scaleway Apple silicon | Hosted Mac mini for a short cloud test, with console-supported macOS reinstallation. | Deferred. Minimum macOS lease is 24 hours; allocation keeps billing until deletion. Some recovery/admin operations are restricted. Confirm the available OS/model and Docker/GUI readiness. [Provider FAQ](https://www.scaleway.com/en/docs/apple-silicon/faq/) |
| MacStadium bare metal | A persistent dedicated Mac for repeated tests or a longer-running lab. | Deferred. Public pricing listed an M4.S with 16 GB RAM and 256 GB SSD at US$149/month; account inventory and exact OS compatibility were not checked. [Pricing and terms](https://macstadium.com/pricing) |

All provider and VM findings are research dated 2026-09-11. No cloud machine was
rented and no macOS VM was created. Published capabilities are not NanoClaw E2E
qualification. Recheck stock, pricing and technical restrictions before use.

## Why MacinCloud is parked

MacinCloud was investigated as a way to test different Mac generations and older
macOS releases. Its [advertised dedicated plans](https://www.macincloud.com/pages/dedicated.html)
list Intel, M1, M2 and M4 configurations, with an overall OS list extending from
El Capitan to Tahoe. That list does not establish that every combination is
compatible or available.

The **live checkout inspected on 2026-09-11** differed from that advertised
range:

| Item | Observed in the checkout |
|---|---|
| Older hardware | Intel Core i7, M1 and M2 were marked sold out. |
| Selectable configuration | M4 with macOS Tahoe 26.6.2, near Frankfurt, Germany. |
| Base allocation | 4 CPU cores, 8 GB RAM, 250 GB SSD, 100 Mbps network and RDP. |
| Displayed price | US$149.99 per month; the 16 GB RAM option showed an additional US$15 per month. This was a configuration price, not a final billing quote. |
| Intel-plan link | Opening the advertised Intel customization link selected the available M4/Tahoe configuration instead. |

These are dated observations of the [live dedicated checkout](https://checkout.macincloud.com/select/dedicated),
not a permanent statement about inventory in every region. Availability, prices
and OS choices must be checked again if the option is revisited.

Docker compatibility also remained unresolved. The
[plan comparison](https://checkout.macincloud.com/select) described Dedicated as
an instance on a physical Mac server. That wording and administrator access do
not establish whether the rented macOS environment is itself virtualized, or
whether it can run Docker's Linux VM. No MacinCloud Docker test was performed.

## Prepared inquiry and rental requirements

A [custom quote form](https://www.macincloud.com/pages/quote.html) was drafted but
**not submitted**. No contact details were entered, account created, subscription
purchased or server provisioned. The inquiry asked for:

- One Intel Mac with Sonoma 14, 16 GB RAM and at least 120 GB SSD as a preferred
  first configuration; another older configuration could be considered if it
  meets the same runtime requirements.
- Full administrator/sudo access, SSH with public-key authentication, and a
  persistent GUI login for the same user running Docker Desktop and NanoClaw's
  LaunchAgent.
- Docker building and running Linux containers on the rented Mac itself,
  including local bind mounts and published ports.
- Confirmation of physical versus virtualized macOS, and explicit confirmation
  that local Docker works on the exact offered configuration.
- Available Intel/M1 hardware and older macOS versions, plus the process, cost
  and lead time for reinstalling or switching macOS.
- The shortest rental term and monthly pricing, including setup fees, renewal
  terms and availability. Europe was preferred, with other regions acceptable.

The inquiry was only for compatibility, availability and pricing. There is no
pending vendor response because it was never sent. The browser draft is not
required to resume this work; the requirements above can recreate it.

## Next step: available physical hardware

Use the existing [native macOS skill](../skills/e2e-macos/SKILL.md), which supports
local execution and an existing SSH connection. Start by identifying the
available Macs and recording model, architecture, macOS version, RAM, free disk
space, Docker readiness and access method. Prefer a machine that adds coverage
beyond the already-tested local Apple silicon environment. Intel/Sonoma is a
candidate if that hardware is available, not an assumed inventory item.

For the selected Mac:

1. Check that its OS supports the chosen Node and Docker versions. An old OS
   being bootable does not prove that the current NanoClaw dependencies run on
   it. Docker Desktop's [published support window](https://docs.docker.com/desktop/setup/install/mac-install/)
   is the current and two previous major macOS releases; verify this again when
   choosing the target and runtime version.
2. Perform read-only preflight. For SSH, verify the destination and host key,
   access as the intended user, and an active GUI login for that same user.
   Confirm a functioning local Docker daemon and choose gateway reuse or a new
   installation explicitly.
3. Within the agreed scope for that Mac, run an exact NanoClaw commit into a new
   persistent checkout. Preserve existing services, Docker containers, shared
   configuration and credentials.
4. Require a real agent reply, successful NanoClaw verification, a running
   LaunchAgent and passing preservation checks. Retain the result and checkout
   for inspection. Installing prerequisites or passing preflight alone is not a
   completed E2E test.

Choosing real hardware does not identify a target or authorize changes to every
available Mac. Select the machine and installation scope before a live run.

## Existing evidence and remaining gaps

The [native skill's validation record](../skills/e2e-macos/SKILL.md#source-contracts-and-validation)
documents a successful **local** run on 2026-09-11: macOS 26.6.1 / arm64, Docker
Desktop and Node 26.8.1, testing NanoClaw
[`74224f62a6c08418acccc727114ab02f92e403bf`](https://github.com/nanocoai/nanoclaw/commit/74224f62a6c08418acccc727114ab02f92e403bf).
It produced a real model reply, passed service verification and preserved the
existing shared state.

An additional **SSH** run passed on 2026-09-11 on an Apple M4 Pro Mac mini with
24 GiB RAM, macOS 26.6.2 / arm64, Node 26.8.2 and Docker Desktop's local Docker
Engine 29.7.2. The same NanoClaw commit completed a fresh installation in about
44 seconds with warm caches, a real reply, successful service verification and
passing shared-state preservation checks.

The first SSH attempt exposed a migration race and failed before inference.
The shared installer now waits for the CLI socket before running the agent
initializer, so host migrations complete first. A second, separate fresh
checkout passed; both attempts were retained. This readiness fix merged in
[PR 6](https://github.com/nanocoai/nanoclaw-oss-dev-tools/pull/6), bringing the
plugin manifest to **0.4.1** and the offline suite to **88 tests**, with hosted
CI passing on Ubuntu and macOS.

The same Mac also passed a separate SSH installation against upstream
[PR 3766](https://github.com/nanocoai/nanoclaw/pull/3766), exact NanoClaw commit
[`1d5179b28ce7b76afef6a23b7c21f981e51cfdad`](https://github.com/nanocoai/nanoclaw/commit/1d5179b28ce7b76afef6a23b7c21f981e51cfdad),
in about 50 seconds with warm caches and the original installer ordering.
Real reply, LaunchAgent verification and preservation checks passed. Both
deterministic cross-process migration regressions passed on that Mac too;
the live logs alone do not establish concurrent migration overlap. See the
[catalog's Mac evidence](skills-catalog.md#e2e-macos) for the tools revision
and the distinction between the readiness fix and upstream migration coverage.

Intel/older-macOS compatibility, cold prerequisite setup and recovery after
reboot/logout remain separate, unqualified cases. Use additional physical
hardware to address those gaps before renting cloud capacity.

Revisit MacinCloud if a needed hardware/OS combination is unavailable locally,
or repeatable rented capacity becomes useful. Recheck inventory and obtain
confirmation of local Docker support before choosing a paid configuration.
