# cf-share-screenshot

Select an area of the screen, upload it to a temporary public URL through a
Cloudflare quick tunnel, and copy that URL to the clipboard. Useful for pasting
a screenshot link into a chat with an AI agent or a colleague.

The clipboard receives
`[Screenshot](https://….trycloudflare.com/img/<id>.png) (try again in a few seconds if this URL doesn't work)`.

## Requirements

Linux with GNOME on Wayland, Python 3.10 or newer, and
these executables on your `PATH`:

- [`cloudflared`](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/)
- `gnome-screenshot`
- `wl-copy` (from `wl-clipboard`)
- `notify-send` (from `libnotify`) for desktop notifications

Install these tools separately. On Debian or Ubuntu:

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

Everything starts automatically when needed.

Bind the command to a keyboard shortcut in GNOME Settings → Keyboard → Custom
Shortcuts, using the full path, for example
`/home/you/.local/bin/cf-share-screenshot`.

## Availability

- Screenshots are available for 30 minutes while the app is running.
- A new link may take a few seconds to start working.
- The app shuts down after 80 minutes without a new screenshot, or if its
  Cloudflare connection fails. Taking another screenshot starts it again.
- Shutting down the app makes existing links unavailable.

## Security

Anyone with a screenshot link can view it while it is available. Treat links as
secrets and avoid sharing sensitive content. Cloudflare's free Quick Tunnels
service has no uptime guarantee.

## Development

```sh
uv run python -m unittest discover -s tests -v
uvx --from build pyproject-build
uvx twine check --strict dist/*
```

## Publishing

The version lives in `__version__` in `cf_share_screenshot.py` (currently
`0.1.2`). Build from a clean checkout using the commands above, ensuring `dist/`
contains only the current release. Upload to TestPyPI:

```sh
uvx twine upload --repository testpypi dist/*
```

Verify the TestPyPI release in an isolated environment:

```sh
uvx --isolated --index https://test.pypi.org/simple/ --default-index https://pypi.org/simple/ cf-share-screenshot@0.1.2 --help
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
