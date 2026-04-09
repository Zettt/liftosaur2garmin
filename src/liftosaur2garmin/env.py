"""Environment loading helpers."""

from __future__ import annotations

from dotenv import find_dotenv, load_dotenv


def load_local_env() -> None:
    """Load a local .env file into os.environ if one is present.

    Existing exported environment variables keep precedence.
    """
    dotenv_path = find_dotenv(usecwd=True)
    if dotenv_path:
        load_dotenv(dotenv_path=dotenv_path, override=False)
