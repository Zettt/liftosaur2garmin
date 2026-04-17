"""Sync Liftosaur workouts to Garmin Connect."""

from importlib.metadata import version, PackageNotFoundError

from liftosaur2garmin.env import load_local_env

load_local_env()

try:
    __version__ = version("liftosaur2garmin")
except PackageNotFoundError:
    __version__ = "0.0.0-dev"
