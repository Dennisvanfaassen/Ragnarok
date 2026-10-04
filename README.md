# Ragnarok Bot

A clean OpenKore-style Ragnarok automation core with a modern local dashboard.

The previous screen-reading and mouse-control prototype has been removed from `main`.

## Current milestone

The repository now contains:

- FastAPI dashboard/backend
- modern browser dashboard
- server profiles
- runtime/profile state
- network session boundary
- bot/task engine shell
- Soulbound connection metadata from the supplied client configuration

The Ragnarok login/game packet protocol is **not implemented yet**. The dashboard deliberately reports that the protocol profile must be verified instead of pretending the bot is connected.

## Soulbound profile

Known client configuration:

- host: `88.214.58.232`
- login port: `6900`
- client version: `55`
- service type: `korea`
- server type: `primary`

The exact packet version / send-receive profile still needs to be identified.

## Install

```powershell
.\setup.bat
.\run.bat
```

The dashboard opens at `http://127.0.0.1:8765`.

## Dashboard

The initial dashboard includes hunting map, monster targets, weight threshold, storage map, healing item, healing threshold, server probing, and start/stop controls.

The separate `Ragnarokmap` repository remains the source for extracted map/world data.

## Scope

This project is intended for servers where the owner/operator permits automation. It does not include anti-cheat bypass or evasion mechanisms.
