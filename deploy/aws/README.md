# Coderbot on AWS Spot instances

Runs any number of coderbot agents in your own AWS account, each on the cheapest
Spot instance that fits, with a persistent EBS volume so a reclaimed instance's
replacement picks the task up where it stopped. CloudFormation and the AWS CLI only:
nothing here depends on this repository's CI or on a particular account. The Azure
equivalent lives in [`../azure`](../azure/README.md).

```
account (once)                 per agent, stack "coderbot-<name>"
┌─────────────────────┐        ┌──────────────────────────────────────────┐
│ shared.yaml         │        │ agent.yaml                               │
│  5 Secrets Manager  │ import │  EBS gp3 volume  (snapshotted on delete) │
│  managed policy     │◄───────│  role + launch template (AL2023 arm64)   │
│  security group     │        │  ASG 1/1/1, one AZ, Spot pool of 5 types │
│  (no ingress; SSM)  │        │    capacity rebalance on                 │
└─────────────────────┘        └──────────────────────────────────────────┘
```

## What survives an interruption

Everything durable lives on the agent's volume, mounted at `/mnt/coderbot`:

| Path | Holds |
|---|---|
| `data/` | `state.json`, `instance_id`, `holds.json`, `processed_msgs.json`, `token.json`, transcripts, outbox |
| `claude/` | the bot's `~/.claude`: `.credentials.json` and the session files `claude --resume` reads |
| `repo/<target>` | the target repo working copy, including uncommitted agent work |
| `coderbot/` | this repository at the pinned ref (the build context) |
| `docker/` | Docker's data-root: image layers, build cache, the e2e stack's images |
| `codebot.env`, `agent.env`, `claude.json` | the shared `.env`, the per-agent overrides, the `~/.claude.json` seed |

On a Spot notice (or a rebalance recommendation) the watcher on the instance stops
the container, which makes `src/main.py` drop its in-flight tick and exit within
seconds, then unmounts and detaches the volume. The Auto Scaling group launches a
replacement in the same AZ; its first boot attaches the volume, starts Docker on
the cached data-root and brings the container up. It comes back with the same
`instance_id`, so it re-asserts its own backlog claim, finds `state.json` at the
phase that was interrupted, and re-runs that phase. Only the single agent
invocation that was running is lost, which is the same recovery as a crash.
Boot-to-running is about a minute once the image cache exists.

## Prerequisites

- AWS CLI v2 with credentials for the target account (`AWS_PROFILE`, `AWS_REGION`).
- A VPC with a public subnet (instances get a public IP and only make outbound
  connections; there is no NAT gateway). The default VPC works.
- The files coderbot needs, produced once on your machine by `scripts/setup.sh`:
  `.env` (with `GH_TOKEN` and `CLAUDE_CODE_OAUTH_TOKEN`), `data/token.json`,
  `data/credentials.json`, `~/.claude/.credentials.json`, `~/.claude.json`.
- `CODEBOT_INSTANCE` and `CODEBOT_REPO_PATH` in that `.env` are ignored: each agent
  gets its own from the stack.

## Deploy

```bash
cd deploy/aws
./deploy.sh shared --vpc vpc-0123456789            # once; omit --vpc for the default VPC
./deploy.sh secrets --env ../../.env --data ../../data
./deploy.sh agent nccr --subnet subnet-0123456789 --target-repo getriverly/riverly
./deploy.sh agent nccr2 --subnet subnet-0123456789 --target-repo getriverly/riverly
./deploy.sh status                                  # every agent: instance, market, volume, container
./deploy.sh logs nccr                               # follow the container log over SSM
```

`agent` options: `--ref` (branch/tag/commit of coderbot, default `main`),
`--size` (volume GB, default 40), `--types` (exactly five arm64 instance types,
most preferred first), `--on-demand-pct` (0 = all Spot), `--snapshot` (restore a
volume from a snapshot, for example into another AZ), `--reseed` (overwrite the
credential files on the volume with the current secrets on next boot; they are
otherwise seeded once because the app refreshes tokens in place), `--coderbot-url`
(your fork).

Day two:

| Command | Effect |
|---|---|
| `deploy.sh update NAME` | fetch the pinned ref again, rebuild the image, restart the container in place |
| `deploy.sh agent NAME ... --ref v2` | change parameters; the ASG rolls the instance (volume kept) |
| `deploy.sh recycle NAME` | terminate the instance and let the ASG replace it (the recovery drill) |
| `deploy.sh stop NAME` / `start NAME` | desired capacity 0 / 1; the volume and its state stay |
| `deploy.sh shell NAME` | interactive shell on the instance (SSM, no SSH keys or open ports) |
| `deploy.sh destroy NAME` | delete the stack; the volume is snapshotted first |

To retire an agent that is mid-task, send it `ABORT <name>` by email first (see the
main README) so it unclaims the task, then `destroy`.

## Cost per agent

Approximate, us-east-1, monthly:

| Item | Cost |
|---|---|
| m7g.large Spot (~$0.035/h) | ~$25 |
| t4g.large Spot (~$0.02/h) when the pool lands there | ~$15 |
| 40 GB gp3 data volume + 16 GB root | ~$4.5 |
| public IPv4 address | ~$3.6 |
| Secrets Manager, 5 secrets shared by all agents | ~$2 total |

Roughly $25-35 per agent against $60+ on-demand for the same size.

## Caveats

- **One AZ per agent.** An EBS volume cannot move, so the ASG is pinned to the
  subnet's AZ. If Spot capacity for all five types dries up there, the agent is down
  until it returns. Escape hatches: `--on-demand-pct 100` (redeploys nothing else),
  or snapshot the volume and redeploy with `--snapshot` into another subnet.
- **Shared credentials.** All agents use one `.env` (one Gmail account, one GitHub
  token, one Claude login). Identity is the `CODEBOT_INSTANCE` override per stack.
- **The image is built on the instance.** The first boot of an agent takes several
  minutes; later boots reuse the build cache on the volume. There is no registry.
- **Unclean stops.** A hard reclaim without the two-minute notice leaves the volume
  mounted at termination; ext4 journaling and Docker's own recovery handle it, and
  orphaned e2e containers show as Exited (coderbot's teardown removes them).
- The container runs as uid 501 (see the Dockerfile); `boot.sh` chowns the volume.

## Files

- `shared.yaml`, `agent.yaml`: the two CloudFormation templates.
- `deploy.sh`: the CLI (`aws cloudformation deploy` plus a few EC2/ASG/SSM calls).
- `instance/bootstrap.sh`: the user-data (installs Docker, attaches the volume,
  clones coderbot, hands over). It is embedded gzip+base64 into `agent.yaml` at
  deploy time, so it needs no CloudFormation escaping.
- `instance/boot.sh`: runs from the checkout on the volume on every boot and on
  `update`: seeds secrets, clones the target repo, writes `agent.env`, installs the
  systemd units, starts the container.
- `instance/coderbot.service`, `instance/coderbot-spot-watch.service`,
  `instance/spot-watch.sh`: the container unit and the interruption watcher.
- `docker-compose.aws.yml`: the compose file with every path on the volume.
