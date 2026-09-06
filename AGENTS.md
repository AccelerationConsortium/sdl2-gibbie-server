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

- The UR arm and the XPR balance sit on a **USB Ethernet adapter** on the
  Gibbie PC (192.168.1.x). Unplugged adapter == both `unknown`; that is the
  cable, not the devices.
- The Flex's robot-server is stopped while the Gibbie REPL owns the hardware
  (one process may hold the Flex's hardware controller). nginx answers 502 in
  that state — the robot is alive, the server is not. The probe distinguishes
  the two.
- The Gibbie UI bridge binds `127.0.0.1:8000`; it is reachable only from the
  Gibbie PC, which is why this service runs there.
