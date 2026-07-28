# Bitcoin Node Doctor

Bitcoin Node Doctor is a health, diagnostics, and update-planning tool for self-hosted Bitcoin and Lightning nodes.

## Current capabilities

- Bitcoin Core synchronization and peer checks
- LND synchronization, graph, peer, channel, and wallet checks
- LNDg health and database integrity checks
- systemd service monitoring
- Docker container status and uptime
- disk, memory, SMART, connectivity, and NTP checks
- backup freshness checks
- Pi-hole health checks
- software version reporting
- stable and prerelease update detection
- dry-run update planning
- Docker Compose backup and rollback planning

## Supported software

- Bitcoin Core
- LND
- LNDg
- ThunderHub
- RTL
- LNbits
- Fulcrum
- Mempool
- Pi-hole

## Usage

Run a full health report:

```bash
./node-doctor

