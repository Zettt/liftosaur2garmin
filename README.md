# liftosaur2garmin

Sync Liftosaur workouts to Garmin Connect by converting Liftosaur history records into strength-training FIT files and uploading them to Garmin.

The app is based on the `hevy2garmin` project in this repo, but the source integration is replaced with the Liftosaur REST API described in [`liftosaur/docs/content/api.md`](/Users/zettt/Downloads/liftosaur2garmin/liftosaur/docs/content/api.md).

## What It Does

- Fetches workout history from Liftosaur with a personal API key
- Parses Liftosaur workout text into exercises, sets, reps, weights, and warmups
- Reuses the Garmin FIT mapping and upload pipeline from `hevy2garmin`
- Supports CLI sync and the FastAPI dashboard flow

## Requirements

- A Liftosaur premium subscription with API access
- A Garmin Connect account
- Python 3.10+
- [`uv`](https://docs.astral.sh/uv/) for environment and dependency management

## Setup From Scratch

Clone the repo, create a virtual environment, activate it, and install the project itself:

```bash
uv venv
source .venv/bin/activate
UV_CACHE_DIR=.uv-cache uv pip install -e ".[dev]"
```

Why install with `-e ".[dev]"` instead of `-r requirements.txt`?

- `requirements.txt` installs dependencies only
- `uv pip install -e ".[dev]"` installs the `liftosaur2garmin` package and creates the `liftosaur2garmin` command in `.venv/bin/`

Check that the command exists:

```bash
which liftosaur2garmin
liftosaur2garmin --help
```

If you do not want to activate the virtual environment, you can run the CLI with either of these forms:

```bash
.venv/bin/liftosaur2garmin --help
.venv/bin/python -m liftosaur2garmin.cli --help
```

## Credentials And Config

Create a Liftosaur API key in the Liftosaur app:

1. Open Liftosaur
2. Go to `Settings`
3. Open `API Keys`
4. Create a key and copy it

The app can read credentials from:

- `~/.liftosaur2garmin/config.json`
- Environment variables
- CLI flags
- The web setup page

The main environment variables are:

- `LIFTOSAUR_API_KEY`
- `GARMIN_EMAIL`
- `GARMIN_PASSWORD`

Example:

```bash
LIFTOSAUR_API_KEY="your-liftosaur-api-key"
GARMIN_EMAIL="you@example.com"
GARMIN_PASSWORD="your-garmin-password"
```

If a `.env` file exists in your working directory, the app loads it automatically.

If you want the variables exported into your shell session as well, load it like this:

```bash
set -a
source .env
set +a
```

The interactive setup wizard is the simplest path for most users:

```bash
liftosaur2garmin init
```

That command saves your settings to:

```bash
~/.liftosaur2garmin/config.json
```

## CLI

```bash
liftosaur2garmin init
liftosaur2garmin sync
liftosaur2garmin list
liftosaur2garmin status
liftosaur2garmin unmapped
liftosaur2garmin serve
```

You can also pass credentials as flags for one-off runs:

```bash
liftosaur2garmin sync \
  --liftosaur-api-key "$LIFTOSAUR_API_KEY" \
  --garmin-email "$GARMIN_EMAIL" \
  --garmin-password "$GARMIN_PASSWORD"
```

## First Run

One clean way to get started is:

```bash
uv venv
source .venv/bin/activate
UV_CACHE_DIR=.uv-cache uv pip install -e ".[dev]"
export LIFTOSAUR_API_KEY="your-liftosaur-api-key"
liftosaur2garmin init
liftosaur2garmin list
liftosaur2garmin sync --dry-run
```

## Tests

```bash
pytest
```

## Notes

- The Liftosaur client normalizes history records into the workout structure expected by the existing Garmin sync pipeline.
- Exercise mapping still uses the large Hevy-derived Garmin mapping table, with compatibility logic for Liftosaur-style names such as `Bench Press, Barbell`.
