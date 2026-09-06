# Agent rules — `sdl2-gibbie-server`

**The canonical rules live in
[`ac-organic-lab/docs/AGENT_RULES.md`](../ac-organic-lab/docs/AGENT_RULES.md).**
Read that file first. This file adds only what is specific to this repo and may
never weaken a canonical rule; if the two seem to conflict, the canonical file
wins — stop and flag it.

## 1. This service observes; it never acts

Every probe in this repo is read-only by contract:

- No `/control/*` endpoints exist and none may be added here. Control of the
  Gibbie bench belongs to `sdl2_sampleprep_platform` (the workflow and its UI
  bridge). If a control surface is ever wanted, it is a different service.
- Probes may not take a control interface: never `RTDEControlInterface` on the
  UR, never `execute.get_protocol_api` or a REPL command on the Flex, never a
  SOAP session that changes balance state, never opening the hotplate's COM
  port. A probe that needs any of those to answer must answer `unknown`.
- `/status` and `/devices/*/status` are side-effect-free and cache-only. The
  background monitor is the only thing that talks to hardware.

## 2. Never report a state the probe did not observe

STATUS_SPEC §2.1/§2.2/§2.3 apply in full. A reachability-only probe describes
the *link* it observed (see README, "What each tile means"), not the device's
operating state. `unknown` is the honest fallback; `error` is only for a fault
the reachable device itself reported (e.g. a UR protective stop).
