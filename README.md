# sdl2-gibbie-server

Read-only **STATUS_SPEC v1.2** gateway for a lab bench whose devices are driven
by something other than the dashboard. One FastAPI process on the bench PC polls
that bench's devices and serves one envelope per device, so the AC Organic Lab
dashboard can show them and PyPoe can alert when one disappears — without this
service ever being able to move, heat, weigh or start anything.

**Two benches run it today, one instance each, same code:**

| Bench | PC | Config | Devices |
|---|---|---|---|
| Gibbie sample prep | `sdl2-pc-04` | `config.example.toml` | workflow bridge, UR-3e, XPR balance, Flex, hotplate |
| Process Chemistry (LLE) | `sdl2-pc-00-lle` | `config.process-chemistry.example.toml` | UR5-CB3, XPR balance, EasyMax reactor service, HPLC data service, pH Pi (tiles are suffixed *(LLE)*) |

Devices are entirely config-driven: a table per device names a probe, and the
probe takes its own options from that table. Adding a bench is a config file, so
the repo name is historical — nothing in the code is Gibbie-specific.

This repo conforms to lab status spec **v1.2** through the §9 read-only clause:
there are no `/control/*` endpoints, `allowed_actions` is always `[]`, and
there is nothing to claim. That is deliberate, not a half-finished migration.
Control of the bench stays in
[`sdl2_sampleprep_platform`](https://github.com/xiaomguo/sdl2_sampleprep_platform).

## What it monitors

| equipment_id | kind | probe | what is observed |
|---|---|---|---|
| `gibbie_workflow` | `other` | `ui_bridge` | the Gibbie UI bridge on `127.0.0.1:8000`: run in progress, live stage, simulation flag, last run error, backend import health |
| `gibbie_ur_arm` | `robot_arm` | `ur_dashboard` | UR Dashboard Server (TCP 29999): `robotmode`, `safetystatus`, `programState` — read-only queries only |
| `gibbie_balance` | `other` | `http_endpoint` | the Mettler XPR SOAP endpoint answers |
| `gibbie_flex` | `liquid_handler` | `flex` | robot-server health + active run; when robot-server is stopped, whether a Gibbie REPL session holds the robot (SSH); camera stream as a component |
| `gibbie_hotplate` | `other` | `serial_port` | COM7 is enumerated (never opened) |

And on the Process Chemistry bench:

| equipment_id | kind | probe | what is observed |
|---|---|---|---|
| `lle_ur5_arm` | `robot_arm` | `ur_dashboard` | as above; this is a CB3, which may answer `safetymode` rather than `safetystatus` (the probe tries both) |
| `lle_xpr_balance` | `other` | `http_endpoint` | the Mettler XPR SOAP endpoint answers |
| `lle_easymax` | `other` | `tcp_port` | Mettler's Reactor Device Server accepts OPC UA connections on this PC's loopback |
| `lle_hplc` | `hplc` | `windows_service` | the Agilent ChemStation data service's state on this PC |
| `lle_ph_unit` | `other` | `tcp_port` | the pH Pi answers on SSH |

Two of those need explaining, because the device itself tells us nothing. The
**EasyMax** answers on no network port of its own; what is observable is
Mettler's reactor service running on the bench PC, which is also what
`automated-lle` connects to. The **HPLC** likewise answers on none of its ports,
because it is ChemStation-driven from this PC rather than through an
instrument-side API like the UPLC-MS on its own bench. In both cases the tile
means "the vendor's software layer is up", and the message says so.

### What each tile means

Every probe reports only what it observed (AGENT_RULES §2):

- **Reachability-only probes** (`http_endpoint`, `serial_port`, `tcp_port`)
  describe the *link*: `ready` means "the endpoint answers" / "the port exists"
  / "something is listening", and the message says the instrument's operating
  state is not monitored. A real state read can replace them later.
- **`windows_service`**: `ready` means the vendor service is running, and a
  *stopped* service reads `requires_init`, never `error` — the instrument may be
  perfectly healthy and driven from its own front end, so a stopped data service
  must not raise an alarm about the hardware. It runs `sc query` only; starting
  a service is a host-ops action with its own whitelist and audit trail.
- **UR arm**: `busy`/`running` means a program is playing on the controller.
  For Gibbie that is the RTDE control program the workflow uploads, which
  plays for the whole session whether or not an axis is moving. Safety stops
  read `error`, e-stops `e_stop`, powered-off `requires_init`.
- **Flex**: with robot-server up, `busy` means an active run. With robot-server
  stopped (the normal state while Gibbie drives the robot through its SSH
  REPL, since one process owns the Flex's hardware controller), a live REPL
  reads `busy`/`running` as external control, no REPL reads `requires_init`,
  and if the SSH probe is off or failing the tile reads `unknown` — enable it.
  nginx's 502 is how "robot alive, server stopped" is told apart from "robot
  gone".
- **Workflow**: a run in `simulation` reads `dry_run` + `running`, so a
  rehearsal never enters the record as lab work.
- **Unreachable**: after `unreachable_after` consecutive failed probes a
  device reads `unknown` (STATUS_SPEC §2.1 — the gateway is fine, the device
  cannot be reached; never `error`), with `components.link.state:
  unreachable` and the outage start in `details.unreachable_since`. One
  missed probe is not an outage; the last good observation stands and
  `details.note` says a probe failed.

Every envelope carries `details.readback_age_s`, `last_seen_at`,
`probe_failures`, `primary_operation` and `monitoring_only: true`.

## HTTP surface

| path | returns |
|---|---|
| `GET /` | `ProbeResponse` for the service |
| `GET /health` | `{"status": "healthy"}` |
| `GET /status` | the monitor process itself (`ready` while polling; permanently `idle`; `metrics.devices_unreachable`) |
| `GET /devices` | the configured devices and their `status_path`s |
| `GET /devices/{id}` | `ProbeResponse` for one device |
| `GET /devices/{id}/status` | that device's `EquipmentStatus` |

All handlers are cache-only; the background monitor is the only thing that
talks to hardware (`[service].poll_interval_s`, default 10 s).

## Run

```powershell
Copy-Item config.example.toml config.toml   # edit addresses
uv sync
uv run gibbie-server --config config.toml --once     # probe everything once, print JSON
uv run gibbie-server --config config.toml            # serve on :8070
```

Environment (only for the optional Flex SSH probe): `GIBBIE_FLEX_SSH_KEY_PASSPHRASE`
for the key named by `ssh_key_file`, or `GIBBIE_FLEX_SSH_PASSWORD` for password
auth. Give `ssh_host` / `ssh_key_file` explicitly: the service runs as
LocalSystem, whose `~/.ssh` is empty, so an alias-only setup cannot resolve.

Deploy as an NSSM service named `gibbie-server` per
`ac-organic-lab/docs/DEVICE_PC_SETUP.md` (`C:\SDL_Tools\uv.exe run --project
<dir> gibbie-server --config <dir>\config.toml`).

## Deployment record

| host | service | port | runs as | since |
|---|---|---|---|---|
| `sdl2-pc-04` (Gibbie PC, tailnet 100.64.254.17) | NSSM `gibbie-server` | 8070 | LocalSystem | 2026-09-06 |

Install layout follows `DEVICE_PC_SETUP.md`: repo at
`C:\Users\sdl2\Projects\sdl2-gibbie-server`, `uv.exe` and `nssm.exe` in
`C:\SDL_Tools`, logs in `C:\SDL_Logs\gibbie-server.{out,err}.log`, firewall
rule "gibbie-server 8070". The venv is synced with `--link-mode copy` and the
service env carries `UV_LINK_MODE=copy`, so the venv holds real files rather
than hardlinks into an elevated user's uv cache (the only shell available on
that PC is an elevated SSH session). Update: `git pull`,
`C:\SDL_Tools\uv.exe sync --link-mode copy`, `nssm restart gibbie-server`.

## Registering with the dashboard

One `equipment.yaml` entry per device, all pointing at this service with a
per-device `status_path`, and **`gateway_fronted: true`** so an `unknown` from
this gateway counts as unreachable for uptime and alerts:

```yaml
  - id: gibbie_ur_arm
    name: UR-3e Arm (Gibbie)
    kind: robot_arm
    adapter: http
    protocol: "1.2"
    gateway_fronted: true
    base_url: http://sdl2-pc-04.tail6a1dd7.ts.net:8070
    status_path: /devices/gibbie_ur_arm/status
    poll_timeout_seconds: 5.0
```

Plus a `hostops_gibbie_pc` entry once `sdl-lab-hostops` runs on the PC, and a
"Sample Prep" section in `platforms.yaml`.

## Tests

```powershell
uv sync --extra dev
uv run pytest -q
```

No hardware: probes are exercised with `httpx.MockTransport`, fake dashboard
sockets and fake port lists.

## Why the Flex is monitored, not driven

The Gibbie workflow drives the Flex through `matterlab_opentrons`'s SSH REPL
and depends on things the robot-server HTTP API does not expose (stem pressure
reads for the pressure-guarded sample draw, gripper jaw control, hardware
positions). The REPL and robot-server cannot both hold the Flex's hardware, so
robot-server stays stopped during Gibbie sessions. This service therefore
observes the Flex from outside and does not include it in the OT-2 gateway
fleet. See the Opentrons gateway repo's `docs/OT2_TAILSCALE.md` for the
robots' network access, and the discussion in `ac-organic-lab/docs/ROADMAP.md`.
