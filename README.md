[![Build](https://github.com/alyraffauf/morsels/actions/workflows/build.yml/badge.svg?branch=master)](https://github.com/alyraffauf/morsels/actions/workflows/build.yml) [![License: AGPL v3](https://img.shields.io/badge/License-AGPL%20v3-blue.svg)](https://www.gnu.org/licenses/agpl-3.0) [![Ko-fi](https://img.shields.io/badge/Donate-Ko--fi-ff5e5b?logo=ko-fi&logoColor=white)](https://ko-fi.com/alyraffauf)

<div align="center">
  <h1>morsels</h1>
  <h3>Small bites, big ideas.</h3>
  <p>A social pastebin on the <a href="https://atproto.com">Atmosphere</a>.</p>
</div>

## Features

- **Share bites**: Paste anything, share it with a link.
- **Built on atproto**: Your bites are stored in your own repo as `blue.morsels.bite` records. Anyone can view them with a morsels instance.
- **OAuth login**: Sign in with your Bluesky handle or any atproto account. No passwords stored.
- **Replies**: Comment on bites. Replies are `blue.morsels.reply` records in your repo.
- **Syntax highlighting**: Auto-detected via [Pygments](https://pygments.org/).
- **Self-hostable**: One Docker command to run your own instance.

## Quick start

### Docker (recommended)

```bash
docker run -d -p 8000:8000 -v morsel-data:/data ghcr.io/alyraffauf/morsels:latest
```

Or with Docker Compose:

```bash
git clone https://github.com/alyraffauf/morsels.git
cd morsel
docker compose up -d
```

Visit `http://localhost:8000`. First run auto-generates secrets.

### From source

Requires Python 3.14+ and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/alyraffauf/morsels.git
cd morsel
uv sync
just dev
```

## Architecture

morsels is a thin client, so it doesn't store your data. Bites and replies live in each user's atproto repo. The server handles:

- **OAuth sessions** in SQLite (`morsel.db`)
- **App signing key** in `secrets.json`
- **In-memory caches** for identity resolution, profiles, and the recent bites feed

External services:

- [Slingshot](https://slingshot.microcosm.blue/) — cached record fetching
- [Constellation](https://constellation.microcosm.blue/) — reply backlink index
- [UFOs](https://ufos.microcosm.blue/) — recent bites feed

## Configuration

All configuration is automatic. On first run, morsels generates:

- `secrets.json` — app secret key and OAuth client signing key
- `morsel.db` — SQLite database for OAuth sessions

Set `MORSEL_DATA_DIR` to control where these files are stored (default: current directory, `/data` in Docker).

## License

[AGPL-3.0](LICENSE.md)
