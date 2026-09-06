# AGENTS.md — shared agent instructions (`sdl2-gibbie-server`)

Layers on the canonical base in
[`ac-organic-lab/AGENTS.md`](../ac-organic-lab/AGENTS.md). Read that first.

## 1. Binding contract

- [`AGENT_RULES.md`](AGENT_RULES.md) → canonical
  [`ac-organic-lab/docs/AGENT_RULES.md`](../ac-organic-lab/docs/AGENT_RULES.md).
- [`ac-organic-lab/docs/STATUS_SPEC.md`](../ac-organic-lab/docs/STATUS_SPEC.md)
  v1.2 via `sdl-lab-contract` tag `v1.2.0`. This service is **monitoring-only**
  and reaches v1.2 through §9's read-only clause: no `/control/*`,
  `allowed_actions: []`, no claims — deliberately, not half-finished.

## 2. What this repo is

A read-only STATUS_SPEC gateway for the **Gibbie sample-prep bench**, running
on the Gibbie PC (`sdl2-pc-04`, tailnet 100.64.254.17). One FastAPI process,
one envelope per device at `/devices/{id}/status` (the same per-path shape as
`kasa-tapo-services` and `sense-every-zone`), registered in the dashboard's
`equipment.yaml` with `status_path` per entry and `gateway_fronted: true`.

```
src/gibbie_server/
  config.py     TOML -> ServiceConfig / DeviceConfig
  probes/       one module per probe type; each returns an Observation
  monitor.py    background poller + per-device debounce (unreachable_after)
  envelope.py   Observation + monitor state -> EquipmentStatus (§2.1/§2.3)
  api.py        FastAPI app: /, /health, /status, /devices, /devices/{id}/status
  __main__.py   `gibbie-server --config config.toml`
```

## 3. Working conventions

- **Environment: `uv`**, Windows device PC. `uv sync --extra dev`; tests with
  `uv run pytest -q`. No hardware is needed for any test — probes are exercised
  through `httpx.MockTransport`, fake sockets and fake port lists.
- **Adding a device** = a `[devices.<id>]` table in `config.toml` (+ a probe
  module if the type is new) + a registry entry in `ac-organic-lab`
  `equipment.yaml` with `status_path: /devices/<id>/status`.
- **Never actuate to test.** If a probe cannot be verified without moving or
  opening something, it is not a probe for this repo.
- Deploy per `ac-organic-lab/docs/DEVICE_PC_SETUP.md` (uv + NSSM); service
  name `gibbie-server`, port 8070.

## 4. Pitfalls

- **The service runs as LocalSystem, so `~` is the SYSTEM profile.** Nothing
  in `~/.ssh` exists there: the Flex SSH probe needs `ssh_host` /
  `ssh_key_file` spelled out in `config.toml` and the passphrase in the NSSM
  env (`GIBBIE_FLEX_SSH_KEY_PASSPHRASE`), never an alias alone.

- **Everything Gibbie is on the lab switch, 192.168.254.x** — the PC at .79
  (its second onboard NIC; the first, "Ethernet", is unplugged and unused),
  the Flex .81, the UR-3e .89, the XPR .83 — the same addresses as the
  workflow's `sdl2_solid_dose/.env` (`ROBOT_IP`, `BALANCE_IP`). The first
  config shipped with 192.168.1.100 / 192.168.1.1 from an older layout and
  both tiles read `unknown` for a day (2026-09-06) until it was corrected;
  192.168.1.x is a *different* segment behind the Cytation PC's USB adapter,
  with a different UR on it. Other URs on the lab switch (.16 Process
  Chemistry UR5-CB3, .49) also greet as "Universal Robots Dashboard Server" —
  identify by address, not by banner.
- **The XPR web service is on port 81, not 8002.** Both lab balances answer
  HTTP on 81 (a bare GET gets 400 — that is the SOAP endpoint refusing GET,
  and the probe counts any HTTP answer as reachable); 8002 is refused and
  TCP 8000 is not HTTP. The first config assumed 8002 from the Gibbie
  workflow's `mt_balance.py`, which is also wrong for these units; the lab's
  fork of Telescope's `mt_xpr_balance` client defaults to 81.
- The Flex's robot-server is stopped while the Gibbie REPL owns the hardware
  (one process may hold the Flex's hardware controller). nginx answers 502 in
  that state — the robot is alive, the server is not. The probe distinguishes
  the two.
- The Gibbie UI bridge binds `127.0.0.1:8000`; it is reachable only from the
  Gibbie PC, which is why this service runs there.
