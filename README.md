# cf-share-screenshot

Select an area of the screen, upload it to a temporary public URL through a
Cloudflare quick tunnel, and copy that URL to the clipboard. Useful for pasting
a screenshot link into a chat with an AI agent or a colleague.

The clipboard receives `screenshot at https://….trycloudflare.com/image/<id>.png`.

## Requirements

Linux with GNOME on Wayland, Python 3.10 or newer (standard library only), and
these executables on your `PATH`:

- [`cloudflared`](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/)
- `gnome-screenshot`
- `wl-copy` (from `wl-clipboard`)
- `notify-send` (optional, from `libnotify`) for desktop notifications

Installing this package does not install any of them. On Debian or Ubuntu:

```sh
sudo apt install gnome-screenshot wl-clipboard libnotify-bin
```

## Installation

```sh
uv tool install cf-share-screenshot
```

Requires [uv](https://docs.astral.sh/uv/getting-started/installation/).
This puts `cf-share-screenshot` in `~/.local/bin`. A one-off run with
`uvx cf-share-screenshot` also works, but a persistent installation is
better for a keyboard shortcut.

You can also run the single file without installing anything:

```sh
python3 cf_share_screenshot.py
```

## Usage

```sh
cf-share-screenshot   # select an area, upload it, copy the URL
```

There are no subcommands; the background server starts automatically when needed.

Bind the command to a keyboard shortcut in GNOME Settings → Keyboard → Custom
Shortcuts, using the full path, for example
`/home/you/.local/bin/cf-share-screenshot`.

## How it works

The first screenshot starts a background server on `127.0.0.1:18765` and a
`cloudflared` quick tunnel to it. Later screenshots reuse both, so the public
host name stays the same while the server runs.

- Each screenshot is kept in memory for 30 minutes under a random 128-bit ID.
- The server checks its public URL about every minute and restarts the tunnel
  if it stops responding; the host name then changes.
- After 3 hours without uploads the server and the tunnel shut down.
- Cancelling the area selection does not start the server.

Runtime files (bearer token, lock files, `server.log`) live in
`$XDG_RUNTIME_DIR/cf-share-screenshot/` with mode `0700`. Check `server.log`
when something goes wrong.

## Security

Anyone who knows an image URL can download that image until it expires. The IDs
are random and are not listed anywhere, but treat the URL as a secret and don't
share screenshots with sensitive content. Uploads and `/status` require a bearer
token that only the local user can read. The server never logs image IDs or
tokens. The tunnel depends on Cloudflare's free quick tunnel service, which has
no uptime guarantee.

## Development

```sh
uv run python -m unittest discover -s tests -v
uvx --from build pyproject-build
uvx twine check --strict dist/*
```

## Publishing

The version lives in `__version__` in `cf_share_screenshot.py` (currently
`0.1.0`). Build from a clean checkout using the commands above, ensuring `dist/`
contains only the current release. Upload to TestPyPI:

```sh
uvx twine upload --repository testpypi dist/*
```

Verify the TestPyPI release in an isolated environment:

```sh
uvx --isolated --index https://test.pypi.org/simple/ --default-index https://pypi.org/simple/ cf-share-screenshot@0.1.0 --help
```

Then upload the same artifacts to PyPI:

```sh
uvx twine upload dist/*
```

Use an API token when Twine prompts for credentials. Never commit credentials.
For subsequent releases, bump `__version__` and build fresh artifacts; PyPI does
not allow replacing an existing release file. GitHub Actions runs tests and
validates distributions; publishing is manual.

## License

MIT; see [LICENSE](LICENSE).
