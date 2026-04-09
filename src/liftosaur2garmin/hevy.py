"""Compatibility wrapper for older imports."""

from liftosaur2garmin import liftosaur as _impl

LiftosaurAuthError = _impl.LiftosaurAuthError
LiftosaurClient = _impl.LiftosaurClient
HevyAuthError = LiftosaurAuthError
HevyClient = LiftosaurClient
time = _impl.time

__all__ = ["HevyAuthError", "HevyClient", "LiftosaurAuthError", "LiftosaurClient"]
