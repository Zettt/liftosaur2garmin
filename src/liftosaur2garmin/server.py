"""FastAPI web dashboard for liftosaur2garmin."""

from __future__ import annotations

import logging
import os
import re
import secrets
import threading
import time
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader

from liftosaur2garmin import db
from liftosaur2garmin.config import is_configured, load_config, save_config
from liftosaur2garmin.sync import sync

logger = logging.getLogger("liftosaur2garmin")

TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"


def _get_cat_names() -> dict[int, str]:
    """Canonical Garmin FIT exercise category names."""
    return {
        0: "Bench Press", 1: "Calf Raise", 2: "Cardio", 3: "Carry", 4: "Chop",
        5: "Core", 6: "Crunch", 7: "Curl", 8: "Deadlift", 9: "Flye",
        10: "Hip Raise", 11: "Hip Stability", 12: "Hip Swing", 13: "Hyperextension",
        14: "Lateral Raise", 15: "Leg Curl", 16: "Leg Raise", 17: "Lunge",
        18: "Olympic Lift", 19: "Plank", 20: "Plyo", 21: "Pull Up", 22: "Push Up",
        23: "Row", 24: "Shoulder Press", 25: "Shoulder Stability", 26: "Shrug",
        27: "Sit Up", 28: "Squat", 29: "Total Body", 30: "Triceps Extension",
        31: "Warm Up", 32: "Run", 33: "Cycling", 36: "Yoga", 38: "Battle Ropes",
        39: "Elliptical", 41: "Indoor Bike", 42: "Indoor Row", 47: "Stair Machine",
        52: "Treadmill", 65534: "Unknown",
    }
_jinja_env = Environment(loader=FileSystemLoader(str(TEMPLATES_DIR)), autoescape=True)


def _render(template_name: str, **ctx) -> HTMLResponse:
    t = _jinja_env.get_template(template_name)
    return HTMLResponse(t.render(**ctx))


def _has_garmin_tokens(config: dict[str, Any] | None = None) -> bool:
    cfg = config or load_config()
    try:
        if db.get_database_url():
            _db = db.get_db()
            if hasattr(_db, "_get_conn"):
                with _db._get_conn() as conn:
                    with conn.cursor() as cur:
                        cur.execute(
                            "SELECT 1 FROM platform_credentials WHERE platform = 'garmin_tokens' LIMIT 1"
                        )
                        return cur.fetchone() is not None
            return False
        from liftosaur2garmin.garmin import token_file_path

        return token_file_path(cfg.get("garmin_token_dir", "~/.garminconnect")).exists()
    except Exception:
        return False


def _persist_cloud_credentials(hevy_api_key: str = "", garmin_email: str = "") -> None:
    if not db.get_database_url():
        return
    try:
        _db = db.get_db()
        if hasattr(_db, "_get_conn"):
            import json as _json

            with _db._get_conn() as conn:
                with conn.cursor() as cur:
                    hevy_key = hevy_api_key or os.environ.get("HEVY_API_KEY", "")
                    if hevy_key:
                        cur.execute(
                            """
                            INSERT INTO platform_credentials (platform, auth_type, credentials, status)
                            VALUES ('hevy', 'api_key', %s, 'active')
                            ON CONFLICT (platform) DO UPDATE
                            SET credentials = EXCLUDED.credentials, status = 'active'
                            """,
                            (_json.dumps({"api_key": hevy_key}),),
                        )
                    if garmin_email:
                        cur.execute(
                            """
                            INSERT INTO platform_credentials (platform, auth_type, credentials, status)
                            VALUES ('garmin', 'email', %s, 'active')
                            ON CONFLICT (platform) DO UPDATE
                            SET auth_type = EXCLUDED.auth_type,
                                credentials = EXCLUDED.credentials,
                                status = 'active'
                            """,
                            (_json.dumps({"email": garmin_email}),),
                        )
                conn.commit()
    except Exception as exc:
        logger.warning("Failed to persist cloud credentials: %s", exc)


app = FastAPI(title="liftosaur2garmin", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ── Auto-sync state ─────────────────────────────────────────────────────────

_autosync_timer: threading.Timer | None = None
_autosync_lock = threading.Lock()
_sync_executing = threading.Lock()  # Prevents concurrent sync execution
_sync_lock_acquired_at: float = 0  # time.time() when lock was acquired
_SYNC_LOCK_TIMEOUT = 300  # 5 minutes — force-release if exceeded
_last_sync_time: datetime | None = None
_unmapped_cache: list[tuple[str, int]] | None = None
_unmapped_cache_time: float = 0
_failed_ids: set[str] = set()  # Workouts that failed upload this session (retried next session)
_VALID_AUTOSYNC_INTERVALS = (30, 60, 120, 240, 360, 720, 1440)
_pending_garmin_logins: dict[str, dict[str, Any]] = {}


def _acquire_sync_lock() -> bool:
    """Try to acquire the sync lock. Force-release if held too long (hung sync)."""
    global _sync_lock_acquired_at
    if _sync_executing.acquire(blocking=False):
        _sync_lock_acquired_at = time.time()
        return True
    # Check if the lock has been held too long (hung sync)
    if _sync_lock_acquired_at and (time.time() - _sync_lock_acquired_at) > _SYNC_LOCK_TIMEOUT:
        logger.warning("Sync lock held for >%ds — force-releasing (likely hung)", _SYNC_LOCK_TIMEOUT)
        try:
            _sync_executing.release()
        except RuntimeError:
            pass
        if _sync_executing.acquire(blocking=False):
            _sync_lock_acquired_at = time.time()
            return True
    return False


def _get_unmapped_exercises() -> list[tuple[str, int]]:
    """Get unmapped exercises. Uses DB cache (updated during sync)."""
    # Try DB cache first (instant)
    try:
        _db = db.get_db()
        cached = _db.get_app_config("unmapped_exercises")
        if cached and isinstance(cached, dict):
            return sorted(cached.items(), key=lambda x: -x[1])
    except Exception:
        pass

    # Fallback: in-memory cache (local installs)
    global _unmapped_cache, _unmapped_cache_time
    import time as _t
    if _unmapped_cache is not None and (_t.time() - _unmapped_cache_time) < 600:
        return _unmapped_cache

    config = load_config()
    unmapped: dict[str, int] = {}
    try:
        from liftosaur2garmin.hevy import HevyClient
        from liftosaur2garmin.mapper import lookup_exercise
        hevy = HevyClient(api_key=config.get("hevy_api_key"))
        for pg in range(1, 6):
            data = hevy.get_workouts(page=pg, page_size=10)
            for w in data.get("workouts", []):
                for ex in w.get("exercises", []):
                    name = ex.get("title") or ex.get("name", "")
                    if name and lookup_exercise(name)[0] == 65534:
                        unmapped[name] = unmapped.get(name, 0) + 1
            if pg >= data.get("page_count", 1):
                break
    except Exception:
        pass

    _unmapped_cache = sorted(unmapped.items(), key=lambda x: -x[1])
    _unmapped_cache_time = _t.time()
    return _unmapped_cache


def _run_autosync() -> None:
    """Execute a sync and reschedule if still enabled."""
    global _last_sync_time
    config = load_config()
    auto_cfg = config.get("auto_sync", {})
    if not auto_cfg.get("enabled", False):
        return

    if not _acquire_sync_lock():
        logger.info("Auto-sync: skipped — another sync is running")
        _schedule_autosync(auto_cfg.get("interval_minutes", 30))
        return

    logger.info("Auto-sync: running scheduled sync")
    hevy_auth_failed = False
    try:
        result = sync(limit=10, dry_run=False)
    except Exception as e:
        from liftosaur2garmin.hevy import HevyAuthError
        if isinstance(e, HevyAuthError):
            logger.error("Auto-sync: Hevy API key invalid — disabling auto-sync. %s", e)
            config["auto_sync"]["enabled"] = False
            save_config(config)
            # Also persist to DB (Vercel filesystem is read-only)
            if db.get_database_url():
                try:
                    import json as _json
                    _db = db.get_db()
                    if hasattr(_db, '_get_conn'):
                        with _db._get_conn() as conn:
                            with conn.cursor() as cur:
                                cur.execute("""
                                    INSERT INTO platform_credentials (platform, auth_type, credentials, status)
                                    VALUES ('auto_sync', 'config', %s, 'active')
                                    ON CONFLICT (platform) DO UPDATE SET credentials = EXCLUDED.credentials
                                """, (_json.dumps({"enabled": False, "interval_minutes": config.get("auto_sync", {}).get("interval_minutes", 120)}),))
                            conn.commit()
                except Exception:
                    pass
            hevy_auth_failed = True
        result = {"synced": 0, "skipped": 0, "failed": 1, "error": str(e)}
    finally:
        _sync_executing.release()

    if hevy_auth_failed:
        return  # Don't reschedule

    _last_sync_time = datetime.now(timezone.utc)
    _record_sync_log(result, trigger="auto")

    # Reschedule
    _schedule_autosync(auto_cfg.get("interval_minutes", 30))


def _schedule_autosync(interval_minutes: int) -> None:
    """Schedule the next auto-sync run."""
    global _autosync_timer
    with _autosync_lock:
        if _autosync_timer is not None:
            _autosync_timer.cancel()
        _autosync_timer = threading.Timer(interval_minutes * 60, _run_autosync)
        _autosync_timer.daemon = True
        _autosync_timer.start()


def _stop_autosync() -> None:
    """Cancel any pending auto-sync timer."""
    global _autosync_timer
    with _autosync_lock:
        if _autosync_timer is not None:
            _autosync_timer.cancel()
            _autosync_timer = None


def _parse_autosync_interval(raw: Any, default: int = 120) -> int:
    """Parse the posted auto-sync interval and clamp to known values."""
    try:
        interval = int(raw)
    except (TypeError, ValueError):
        return default
    if interval not in _VALID_AUTOSYNC_INTERVALS:
        return default
    return interval


def _record_sync_log(result: dict, trigger: str = "manual") -> None:
    """Record a sync result to SQLite."""
    db.record_sync_log(
        synced=result.get("synced", 0),
        skipped=result.get("skipped", 0),
        failed=result.get("failed", 0),
        trigger=trigger,
    )


def _get_autosync_status() -> dict[str, Any]:
    """Build auto-sync status dict for templates."""
    config = load_config()
    auto_cfg = config.get("auto_sync", {})
    enabled = auto_cfg.get("enabled", False)
    interval = auto_cfg.get("interval_minutes", 30)

    # On cloud, read persisted state from DB (filesystem config doesn't persist)
    if db.get_database_url():
        try:
            import json as _json
            _db = db.get_db()
            if hasattr(_db, '_get_conn'):
                with _db._get_conn() as conn:
                    with conn.cursor() as cur:
                        cur.execute("SELECT credentials FROM platform_credentials WHERE platform = 'auto_sync' LIMIT 1")
                        row = cur.fetchone()
                        if row and row.get("credentials"):
                            creds = row["credentials"] if isinstance(row["credentials"], dict) else _json.loads(row["credentials"])
                            enabled = creds.get("enabled", False)
                            interval = creds.get("interval_minutes", 120)
        except Exception:
            pass

    status: dict[str, Any] = {
        "enabled": enabled,
        "interval_minutes": interval,
        "last_sync": None,
        "next_sync": None,
    }

    if _last_sync_time:
        elapsed = datetime.now(timezone.utc) - _last_sync_time
        minutes_ago = int(elapsed.total_seconds() / 60)
        if minutes_ago < 1:
            status["last_sync"] = "just now"
        elif minutes_ago < 60:
            status["last_sync"] = f"{minutes_ago} min ago"
        else:
            hours_ago = minutes_ago // 60
            status["last_sync"] = f"{hours_ago}h {minutes_ago % 60}m ago"

        if enabled:
            remaining = interval - minutes_ago
            if remaining <= 0:
                status["next_sync"] = "soon"
            elif remaining < 60:
                status["next_sync"] = f"in {remaining} min"
            else:
                status["next_sync"] = f"in {remaining // 60}h {remaining % 60}m"

    return status


@app.on_event("startup")
async def _startup_autosync() -> None:
    """Start auto-sync timer on server startup if enabled."""
    config = load_config()
    auto_cfg = config.get("auto_sync", {})
    if auto_cfg.get("enabled", False):
        interval = auto_cfg.get("interval_minutes", 30)
        logger.info("Auto-sync enabled on startup: every %d min", interval)
        _schedule_autosync(interval)


_is_configured_cache: bool | None = None

@app.middleware("http")
async def check_setup(request: Request, call_next):
    global _is_configured_cache
    path = request.url.path
    if path in (
        "/setup",
        "/favicon.ico",
        "/api/sync-one",
        "/api/cron/sync",
        "/api/setup-actions",
        "/api/validate-hevy",
        "/api/garmin/login/start",
        "/api/garmin/login/finish",
        "/api/garmin/import-token-file",
        "/api/garmin/export-token-file",
    ) \
       or path.startswith("/static"):
        return await call_next(request)
    # Cache is_configured result (set to True after first successful setup)
    if _is_configured_cache is None:
        _is_configured_cache = is_configured()
    if not _is_configured_cache:
        _is_configured_cache = is_configured()  # Re-check in case setup just completed
        if not _is_configured_cache:
            return RedirectResponse("/setup")
    return await call_next(request)


# ── Pages ────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    config = load_config()
    synced_count = db.get_synced_count()
    recent = db.get_recent_synced(5)

    garmin_connected = _has_garmin_tokens(config)

    hevy_total = 0
    matched_count = synced_count  # Use DB count (fast) instead of Garmin API (slow)
    try:
        # Try cached count from DB first (instant), fall back to Hevy API
        _db = db.get_db()
        cached = _db.get_app_config("hevy_total")
        if cached and isinstance(cached, dict):
            hevy_total = cached.get("count", 0)
        else:
            from liftosaur2garmin.hevy import HevyClient
            hevy = HevyClient(api_key=config.get("hevy_api_key"))
            hevy_total = hevy.get_workout_count()
            _db.set_app_config("hevy_total", {"count": hevy_total})
    except Exception:
        pass
    mapping_count = 0
    try:
        from liftosaur2garmin.mapper import HEVY_TO_GARMIN, _custom_mappings, _ensure_custom_loaded
        _ensure_custom_loaded()
        mapping_count = len(HEVY_TO_GARMIN) + len(_custom_mappings)
    except Exception:
        pass
    return _render(
        "dashboard.html",
        synced_count=synced_count,
        matched_count=matched_count,
        hevy_total=hevy_total,
        recent=recent,
        auto_sync=_get_autosync_status(),
        sync_log=db.get_sync_log(10),
        mapping_count=mapping_count,
        garmin_connected=garmin_connected,
        needs_actions_setup=False,
    )



@app.get("/setup", response_class=HTMLResponse)
async def setup_page(request: Request):
    config = load_config()
    return _render(
        "setup.html",
        config=config,
        is_cloud=bool(db.get_database_url()),
        garmin_connected=_has_garmin_tokens(config),
        setup_saved=request.query_params.get("saved") == "1",
    )


@app.post("/setup")
async def setup_save(
    hevy_api_key: str = Form(""),
    garmin_email: str = Form(""),
    weight_kg: float = Form(80.0),
    birth_year: int = Form(1990),
    sex: str = Form("male"),
):
    config = load_config()
    if hevy_api_key:
        config["hevy_api_key"] = hevy_api_key
    if garmin_email:
        config["garmin_email"] = garmin_email
    config["user_profile"]["weight_kg"] = weight_kg
    config["user_profile"]["birth_year"] = birth_year
    config["user_profile"]["sex"] = sex
    save_config(config)
    _persist_cloud_credentials(hevy_api_key=hevy_api_key, garmin_email=garmin_email)
    if db.get_database_url() and not _has_garmin_tokens(config):
        return RedirectResponse("/setup?saved=1", status_code=303)
    return RedirectResponse("/", status_code=303)


# ── Garmin auth APIs ───────────────────────────────────────────────────────


def _garmin_auth_error(error: Exception) -> str:
    cleaned = re.sub(r"<[^>]+>", " ", str(error))
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip()
    return cleaned[:200] or "Unknown Garmin authentication error"


@app.post("/api/garmin/login/start")
async def api_garmin_login_start(request: Request):
    body = await request.json()
    email = str(body.get("email", "")).strip()
    password = str(body.get("password", "")).strip()
    if not email or not password:
        return JSONResponse({"error": "Garmin email and password are required"}, status_code=400)

    from liftosaur2garmin.garmin import GarminNeedsMFA, start_login

    config = load_config()
    config["garmin_email"] = email
    save_config(config)
    try:
        result = start_login(email, password, config.get("garmin_token_dir", "~/.garminconnect"))
        return JSONResponse({"ok": True, "status": result["status"], "display_name": result.get("display_name")})
    except GarminNeedsMFA as exc:
        login_id = secrets.token_urlsafe(16)
        _pending_garmin_logins[login_id] = {"state": exc.login_state, "email": email, "created_at": time.time()}
        return JSONResponse({"ok": True, "status": "needs_mfa", "login_id": login_id})
    except Exception as exc:
        logger.warning("Garmin login start failed: %s", exc)
        return JSONResponse({"error": _garmin_auth_error(exc)}, status_code=400)


@app.post("/api/garmin/login/finish")
async def api_garmin_login_finish(request: Request):
    body = await request.json()
    login_id = str(body.get("login_id", "")).strip()
    mfa_code = str(body.get("mfa_code", "")).strip()
    pending = _pending_garmin_logins.get(login_id)
    if not login_id or not pending:
        return JSONResponse({"error": "Garmin login session expired. Start again."}, status_code=400)
    if not mfa_code:
        return JSONResponse({"error": "Verification code is required"}, status_code=400)

    from liftosaur2garmin.garmin import finish_login

    try:
        config = load_config()
        result = finish_login(
            mfa_code,
            pending["state"],
            pending.get("email"),
            config.get("garmin_token_dir", "~/.garminconnect"),
        )
        _pending_garmin_logins.pop(login_id, None)
        return JSONResponse({"ok": True, "status": result["status"], "display_name": result.get("display_name")})
    except Exception as exc:
        logger.warning("Garmin login finish failed: %s", exc)
        return JSONResponse({"error": _garmin_auth_error(exc)}, status_code=400)


@app.post("/api/garmin/import-token-file")
async def api_garmin_import_token_file(
    token_file: UploadFile = File(...),
    garmin_email: str = Form(""),
):
    from liftosaur2garmin.garmin import GarminAuthSession, save_token_payload

    try:
        payload = json.loads((await token_file.read()).decode())
        if not isinstance(payload, dict):
            raise ValueError("Garmin token file must be a JSON object")
        if garmin_email:
            payload["email"] = garmin_email
        GarminAuthSession().load_payload(payload)
        save_token_payload(payload)
        if garmin_email:
            config = load_config()
            config["garmin_email"] = garmin_email
            save_config(config)
            _persist_cloud_credentials(garmin_email=garmin_email)
        return JSONResponse({"ok": True})
    except Exception as exc:
        logger.warning("Garmin token import failed: %s", exc)
        return JSONResponse({"error": _garmin_auth_error(exc)}, status_code=400)


@app.get("/api/garmin/export-token-file")
async def api_garmin_export_token_file():
    from liftosaur2garmin.garmin import token_file_path

    path = token_file_path(load_config().get("garmin_token_dir", "~/.garminconnect"))
    if not path.exists():
        return JSONResponse({"error": "No local Garmin token file found"}, status_code=404)
    return FileResponse(path, filename=path.name, media_type="application/json")


@app.get("/workouts", response_class=HTMLResponse)
async def workouts_page(request: Request):
    config = load_config()
    workouts = []
    page = int(request.query_params.get("page", 1))
    page_count = 1
    fetch_error = None
    try:
        from liftosaur2garmin.hevy import HevyClient

        _db = db.get_db()
        cache_key = f"hevy_workouts_page_{page}"

        # Try DB cache first (populated during sync). Fall back to Hevy API on miss.
        cached = _db.get_app_config(cache_key)
        if cached:
            workouts_raw = cached.get("workouts", [])
            page_count = cached.get("page_count", 1)
        else:
            data = HevyClient(api_key=config.get("hevy_api_key")).get_workouts(page=page, page_size=10)
            workouts_raw = data.get("workouts", [])
            page_count = data.get("page_count", 1)
            _db.set_app_config(cache_key, {"workouts": workouts_raw, "page_count": page_count})

        # Batch check sync status (1 query instead of N)
        hevy_ids = [w.get("id", "") for w in workouts_raw]
        synced_map = _db.get_synced_ids(hevy_ids) if hasattr(_db, 'get_synced_ids') else {
            wid: db.get_garmin_id(wid) for wid in hevy_ids if db.is_synced(wid)
        }
        # Check for workouts edited on Hevy since last sync
        stale_ids = set(_db.get_stale_synced(workouts_raw))

        # Get profile for calorie calculation
        profile = config.get("user_profile", {})
        weight_kg = profile.get("weight_kg", 80.0)
        birth_year = profile.get("birth_year", 1990)
        vo2max = profile.get("vo2max", 45.0)

        for w in workouts_raw:
            w["start_time"] = w.get("start_time") or w.get("startTime", "")
            w["end_time"] = w.get("end_time") or w.get("endTime", "")
            if w["id"] in synced_map:
                w["status"] = "uploaded"
                gid = synced_map[w["id"]]
                if gid:
                    w["garmin_match"] = {"garmin_id": gid, "garmin_name": w.get("title", "")}
                if w["id"] in stale_ids:
                    w["edited_since_sync"] = True
            else:
                w["status"] = "pending"

            # Calculate calorie breakdown for display
            try:
                start = w["start_time"]
                end = w["end_time"]
                if start and end:
                    from liftosaur2garmin.fit import _parse_timestamp, _DEFAULT_HR_BPM
                    start_dt = _parse_timestamp(start)
                    end_dt = _parse_timestamp(end)
                    duration_s = (end_dt - start_dt).total_seconds()
                    workout_year = start_dt.year
                    age = workout_year - birth_year
                    # Default HR (no samples available in listing)
                    hr = _DEFAULT_HR_BPM
                    kcal_per_min = (
                        -95.7735 + 0.634 * hr + 0.404 * vo2max
                        + 0.394 * weight_kg + 0.271 * age
                    ) / 4.184
                    total_kcal = max(0, round(max(0.0, kcal_per_min) * (duration_s / 60.0)))
                    duration_min = int(duration_s // 60)
                    w["cal_info"] = {
                        "duration_min": duration_min,
                        "avg_hr": hr,
                        "hr_source": "default 90 bpm",
                        "weight_kg": weight_kg,
                        "age": age,
                        "vo2max": vo2max,
                        "kcal_per_min": round(kcal_per_min, 2),
                        "total_kcal": total_kcal,
                    }
            except Exception:
                pass

        workouts = workouts_raw
    except Exception as e:
        logger.error("Failed to fetch workouts: %s", e)
        fetch_error = str(e)
    hr_fusion = config.get("hr_fusion", {}).get("enabled", True)
    return _render("workouts.html", workouts=workouts, hr_fusion_enabled=hr_fusion, page=page, page_count=page_count, fetch_error=fetch_error)


@app.get("/api/workout/{hevy_id}/hr", response_class=HTMLResponse)
async def api_workout_hr(request: Request, hevy_id: str):
    """Fetch HR data for a workout's matched Garmin activity. Returns JSON for Chart.js.

    Results are cached in SQLite — first load hits Garmin API, subsequent loads are instant.
    """
    from fastapi.responses import JSONResponse

    config = load_config()

    # Check if HR fusion is enabled
    if not config.get("hr_fusion", {}).get("enabled", True):
        return JSONResponse({"error": "HR fusion disabled in settings"}, status_code=404)

    # Check cache first
    cached = db.get_cached_hr(hevy_id)
    if cached:
        return JSONResponse(cached)

    try:
        from liftosaur2garmin.hevy import HevyClient
        from liftosaur2garmin.garmin import RateLimiter, get_client
        from liftosaur2garmin.matcher import fetch_garmin_activities, match_workouts_to_garmin

        hevy = HevyClient(api_key=config.get("hevy_api_key"))
        data = hevy.get_workouts(page=1, page_size=10)
        workouts = data.get("workouts", [])
        workout = next((w for w in workouts if w["id"] == hevy_id), None)
        if not workout:
            return JSONResponse({"error": "Workout not found"}, status_code=404)

        garmin_client = get_client(config.get("garmin_email"))
        garmin_acts = fetch_garmin_activities(garmin_client, count=1000)
        matches = match_workouts_to_garmin([workout], garmin_acts)

        if hevy_id not in matches:
            return JSONResponse({"error": "No matching Garmin activity"}, status_code=404)

        garmin_id = matches[hevy_id]["garmin_id"]
        limiter = RateLimiter(delay=1.0)

        # Fetch activity summary for avg/max HR
        details = limiter.call(garmin_client.get_activity, garmin_id)

        # Get workout start/end timestamps to slice daily HR
        from liftosaur2garmin.fit import _parse_timestamp
        w_start = workout.get("start_time") or workout.get("startTime", "")
        w_end = workout.get("end_time") or workout.get("endTime", "")
        start_dt = _parse_timestamp(w_start)
        end_dt = _parse_timestamp(w_end)
        start_ms = int(start_dt.timestamp() * 1000)
        end_ms = int(end_dt.timestamp() * 1000)
        total_duration_s = max(1, (end_ms - start_ms) / 1000)

        # Fetch daily HR data and slice to workout window
        date_str = w_start[:10]
        daily_hr = limiter.call(garmin_client.get_heart_rates, date_str)
        hr_values = daily_hr.get("heartRateValues", []) if isinstance(daily_hr, dict) else []

        hr_samples = []
        for entry in hr_values:
            if isinstance(entry, list) and len(entry) >= 2 and entry[1] is not None:
                ts, bpm = entry[0], entry[1]
                if start_ms - 60000 <= ts <= end_ms + 60000:  # ±1 min buffer
                    secs_from_start = (ts - start_ms) / 1000
                    hr_samples.append({"time": max(0, secs_from_start), "hr": bpm})

        hr_samples.sort(key=lambda x: x["time"])

        # Build exercise segments — proportional to actual workout duration
        exercises = workout.get("exercises", [])
        seg_colors = ["#3b82f6", "#22c55e", "#f97316", "#a855f7", "#ef4444", "#06b6d4", "#eab308", "#ec4899"]
        total_sets = sum(len(ex.get("sets", [])) for ex in exercises)
        segments = []
        cursor = 0.0
        for i, ex in enumerate(exercises):
            n_sets = len(ex.get("sets", []))
            if total_sets > 0:
                ex_duration = total_duration_s * (n_sets / total_sets)
            else:
                ex_duration = total_duration_s / max(1, len(exercises))
            segments.append({
                "name": ex.get("title") or ex.get("name", f"Exercise {i+1}"),
                "start": round(cursor),
                "end": round(cursor + ex_duration),
                "color": seg_colors[i % len(seg_colors)],
            })
            cursor += ex_duration

        result = {
            "hr_samples": hr_samples,
            "segments": segments,
            "garmin_id": garmin_id,
            "garmin_name": matches[hevy_id].get("garmin_name", ""),
            "avg_hr": details.get("averageHR") or details.get("summaryDTO", {}).get("averageHR"),
            "max_hr": details.get("maxHR") or details.get("summaryDTO", {}).get("maxHR"),
            "calories": details.get("calories") or details.get("summaryDTO", {}).get("calories"),
        }

        # Cache for instant subsequent loads
        db.cache_hr(hevy_id, result)

        return JSONResponse(result)

    except Exception as e:
        logger.error("HR data fetch failed: %s", e)
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/sync")
async def sync_page(request: Request):
    return RedirectResponse("/")


@app.get("/mappings", response_class=HTMLResponse)
async def mappings_page(request: Request):
    from liftosaur2garmin.mapper import HEVY_TO_GARMIN, _custom_mappings, _ensure_custom_loaded

    _ensure_custom_loaded()

    CAT_NAMES = _get_cat_names()

    mappings = []
    for name, (cat, subcat) in sorted(HEVY_TO_GARMIN.items()):
        cat_name = CAT_NAMES.get(cat, f"Category {cat}")
        mappings.append((name, cat, subcat, cat_name))
    for name, (cat, subcat) in sorted(_custom_mappings.items()):
        cat_name = CAT_NAMES.get(cat, f"Category {cat}")
        mappings.append((name, cat, subcat, f"{cat_name} (custom)"))

    # Find unmapped exercises from recent workouts (cached)
    unmapped = _get_unmapped_exercises()

    custom_list = [(name, cat, subcat, CAT_NAMES.get(cat, f"Category {cat}"))
                   for name, (cat, subcat) in sorted(_custom_mappings.items())]

    return _render(
        "mappings.html",
        mappings=mappings,
        total=len(mappings),
        custom_count=len(_custom_mappings),
        custom_list=custom_list,
        unmapped=unmapped,
    )


@app.get("/history", response_class=HTMLResponse)
async def history_page(request: Request):
    return _render("history.html", total=db.get_synced_count(), history=db.get_recent_synced(50))


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    config = load_config()
    unmapped: dict[str, int] = {}
    try:
        # Use cached unmapped from DB (no Hevy API call)
        for name, count in _get_unmapped_exercises():
            unmapped[name] = count
    except Exception:
        pass
    return _render(
        "settings.html",
        config=config,
        unmapped=sorted(unmapped.items(), key=lambda x: -x[1]),
        garmin_connected=_has_garmin_tokens(config),
        is_cloud=bool(db.get_database_url()),
    )


@app.post("/settings")
async def settings_save(
    hevy_api_key: str = Form(""), garmin_email: str = Form(""),
    weight_kg: float = Form(80.0), birth_year: int = Form(1990), sex: str = Form("male"), vo2max: float = Form(45.0),
    working_set_seconds: int = Form(40), warmup_set_seconds: int = Form(25),
    rest_between_sets_seconds: int = Form(75), rest_between_exercises_seconds: int = Form(120),
    hr_fusion_enabled: str = Form("off"),
):
    config = load_config()
    if hevy_api_key:
        config["hevy_api_key"] = hevy_api_key
    if garmin_email:
        config["garmin_email"] = garmin_email
    config["user_profile"].update(weight_kg=weight_kg, birth_year=birth_year, sex=sex, vo2max=vo2max)
    config["timing"].update(
        working_set_seconds=working_set_seconds, warmup_set_seconds=warmup_set_seconds,
        rest_between_sets_seconds=rest_between_sets_seconds,
        rest_between_exercises_seconds=rest_between_exercises_seconds,
    )
    config.setdefault("hr_fusion", {})["enabled"] = hr_fusion_enabled == "on"
    save_config(config)
    _persist_cloud_credentials(hevy_api_key=hevy_api_key, garmin_email=garmin_email)

    # Persist settings to DB on cloud (filesystem is read-only on Vercel)
    if db.get_database_url():
        try:
            _db = db.get_db()
            _db.set_app_config("user_profile", config["user_profile"])
            _db.set_app_config("timing", config["timing"])
            _db.set_app_config("hr_fusion", config.get("hr_fusion", {}))
        except Exception as e:
            logger.warning("Failed to persist settings to DB: %s", e)

    return RedirectResponse("/settings", status_code=303)


# ── API (HTMX) ──────────────────────────────────────────────────────────────


@app.post("/api/mapping", response_class=HTMLResponse)
async def api_save_mapping(request: Request):
    """Save a custom exercise mapping."""
    form = await request.form()
    hevy_name = form.get("hevy_name", "").strip()
    category = int(form.get("category", 65534))
    subcategory = int(form.get("subcategory", 0))

    if not hevy_name:
        return HTMLResponse('<div class="toast toast-error">Exercise name required</div>')

    # Validate category ID exists
    valid_cats = set(_get_cat_names().keys())
    if category not in valid_cats:
        return HTMLResponse(f'<div class="toast toast-error">Invalid category ID {category}</div>')

    # Save to DB on cloud, filesystem locally
    if db.get_database_url():
        _db = db.get_db()
        if hasattr(_db, 'save_custom_mapping'):
            _db.save_custom_mapping(hevy_name, category, subcategory)
    else:
        from liftosaur2garmin.mapper import save_custom_mapping
        save_custom_mapping(hevy_name, category, subcategory)

    global _unmapped_cache
    _unmapped_cache = None

    cat_label = _get_cat_names().get(category, f"Category {category}")
    return HTMLResponse(f'<div class="toast toast-success">Mapped "{hevy_name}" → {cat_label} ({category}:{subcategory}). <a href="/mappings">Reload</a></div>')


@app.post("/api/mapping/delete", response_class=HTMLResponse)
async def api_delete_mapping(request: Request):
    """Delete a custom exercise mapping."""
    form = await request.form()
    hevy_name = form.get("hevy_name", "").strip()
    if not hevy_name:
        return HTMLResponse('<div class="toast toast-error">Exercise name required</div>')

    from liftosaur2garmin.mapper import _custom_mappings
    if db.get_database_url():
        _db = db.get_db()
        if hasattr(_db, 'delete_custom_mapping'):
            _db.delete_custom_mapping(hevy_name)
    else:
        import json
        from pathlib import Path
        path = Path("~/.liftosaur2garmin/custom_mappings.json").expanduser()
        if path.exists():
            try:
                data = json.loads(path.read_text())
                data.pop(hevy_name, None)
                path.write_text(json.dumps(data, indent=2))
            except Exception:
                pass
    _custom_mappings.pop(hevy_name, None)

    global _unmapped_cache
    _unmapped_cache = None

    return HTMLResponse(f'<div class="toast toast-success">Deleted mapping for "{hevy_name}". <a href="/mappings">Reload</a></div>')


@app.get("/api/validate-hevy")
async def api_validate_hevy(request: Request):
    """Test a Hevy API key. Used by setup page."""
    from fastapi.responses import JSONResponse
    key = request.query_params.get("key", "")
    if not key:
        return JSONResponse({"error": "No key provided"}, status_code=400)
    try:
        from liftosaur2garmin.hevy import HevyClient
        count = HevyClient(api_key=key).get_workout_count()
        return JSONResponse({"valid": True, "workout_count": count})
    except Exception as e:
        return JSONResponse({"valid": False, "error": str(e)}, status_code=400)


@app.get("/api/garmin-categories")
async def api_garmin_categories(request: Request):
    """Return Garmin FIT exercise categories for the mapping UI."""
    from fastapi.responses import JSONResponse
    return JSONResponse({str(k): v for k, v in _get_cat_names().items()})


@app.post("/api/pull-garmin-profile", response_class=HTMLResponse)
async def api_pull_garmin_profile(request: Request):
    """Pull weight, birth date, and gender from Garmin Connect."""
    config = load_config()
    try:
        from liftosaur2garmin.garmin import RateLimiter, get_client

        garmin_client = get_client(config.get("garmin_email"))
        limiter = RateLimiter(delay=1.0)
        raw = limiter.call(garmin_client.get_user_profile)
        profile = raw.get("userData", {}) if isinstance(raw, dict) else {}

        weight = profile.get("weight")  # grams
        birth = profile.get("birthDate")  # "YYYY-MM-DD"
        gender = profile.get("gender")  # "MALE" / "FEMALE"
        vo2max = profile.get("vo2MaxRunning")

        updates = []
        if weight:
            weight_kg = round(weight / 1000, 1)
            config["user_profile"]["weight_kg"] = weight_kg
            updates.append(f"{weight_kg} kg")
        if birth:
            birth_year = int(birth[:4])
            config["user_profile"]["birth_year"] = birth_year
            updates.append(f"born {birth_year}")
        if gender:
            sex = gender.lower()
            config["user_profile"]["sex"] = sex
            updates.append(sex)
        if vo2max:
            config["user_profile"]["vo2max"] = float(vo2max)
            updates.append(f"VO2max {vo2max}")

        if updates:
            save_config(config)
            msg = "Pulled from Garmin: " + ", ".join(updates)
            return HTMLResponse(f'<div class="toast toast-success" style="margin-bottom: 12px;">{msg}</div><script>setTimeout(()=>location.reload(),1500)</script>')
        return HTMLResponse('<div class="toast toast-error" style="margin-bottom: 12px;">No profile data found on Garmin.</div>')
    except Exception as e:
        return HTMLResponse(f'<div class="toast toast-error" style="margin-bottom: 12px;">Failed: {e}</div>')


@app.post("/api/sync", response_class=HTMLResponse)
async def api_sync(request: Request):
    global _last_sync_time

    # If GitHub PAT + repo are set (Vercel deploy), trigger sync via GitHub Actions
    github_pat = os.environ.get("GITHUB_PAT")
    github_repo = os.environ.get("GITHUB_REPO")
    if github_pat and github_repo:
        import requests as req

        resp = req.post(
            f"https://api.github.com/repos/{github_repo}/dispatches",
            headers={
                "Authorization": f"Bearer {github_pat}",
                "Accept": "application/vnd.github+json",
            },
            json={"event_type": "sync-trigger"},
            timeout=10,
        )
        if resp.ok:
            return HTMLResponse(
                '<div class="toast toast-success">Sync triggered via GitHub Actions.'
                " Workouts will appear in a few minutes.</div>"
            )
        return HTMLResponse(
            f'<div class="toast toast-error">Failed to trigger sync: HTTP {resp.status_code}</div>'
        )

    form = await request.form()
    scope = form.get("scope", "recent")

    # Map scope to sync args
    sync_kwargs: dict = {"dry_run": False}
    if scope == "all":
        sync_kwargs["fetch_all"] = True
    elif scope.isdigit():
        sync_kwargs["limit"] = int(scope)
    else:
        # Time-based: compute "since" date
        from datetime import timedelta
        now = datetime.now(timezone.utc)
        deltas = {
            "24h": timedelta(hours=24),
            "7d": timedelta(days=7),
            "30d": timedelta(days=30),
            "6mo": timedelta(days=180),
            "1y": timedelta(days=365),
        }
        delta = deltas.get(scope, timedelta(hours=24))
        since_dt = now - delta
        sync_kwargs["since"] = since_dt.strftime("%Y-%m-%dT%H:%M:%S+00:00")
        sync_kwargs["fetch_all"] = True  # paginate until we hit the date

    if not _acquire_sync_lock():
        return HTMLResponse('<div class="toast toast-error">Another sync is already running. Please wait.</div>')

    try:
        result = sync(**sync_kwargs)
    except Exception as e:
        result = {"synced": 0, "skipped": 0, "failed": 1, "unmapped": [], "error": str(e)}
    finally:
        _sync_executing.release()
    _last_sync_time = datetime.now(timezone.utc)
    _record_sync_log(result, trigger=f"manual ({scope})")
    return _render("partials/sync_result.html", result=result)


@app.post("/api/sync/{workout_id}", response_class=HTMLResponse)
async def api_sync_single(request: Request, workout_id: str):
    try:
        from liftosaur2garmin.hevy import HevyClient
        from liftosaur2garmin.fit import generate_fit
        from liftosaur2garmin.garmin import get_client, rename_activity, set_description, upload_fit, generate_description, find_activity_by_start_time
        import tempfile

        # force_upload=true skips dedup (used by re-sync after edit)
        force_upload = request.query_params.get("force") == "1"

        config = load_config()
        data = HevyClient(api_key=config.get("hevy_api_key")).get_workouts(page=1, page_size=10)
        workout = next((w for w in data.get("workouts", []) if w["id"] == workout_id), None)
        if not workout:
            return HTMLResponse('<td colspan="5">Workout not found</td>')

        garmin_client = get_client(config.get("garmin_email"))
        workout_start = workout.get("start_time")

        # Dedup: check if activity already exists on Garmin (skip if force)
        existing_id = None
        if not force_upload and workout_start:
            existing_id = find_activity_by_start_time(garmin_client, workout_start)

        with tempfile.TemporaryDirectory() as tmp:
            fit_path = f"{tmp}/{workout_id}.fit"
            result = generate_fit(workout, hr_samples=None, output_path=fit_path)
            if existing_id:
                aid = existing_id
                logger.info("Activity already on Garmin (%s), skipping upload", aid)
            else:
                upload_result = upload_fit(garmin_client, fit_path, workout_start=workout_start)
                aid = upload_result.get("activity_id")
            if aid:
                rename_activity(garmin_client, aid, workout["title"])
                set_description(garmin_client, aid, generate_description(workout, calories=result.get("calories"), avg_hr=result.get("avg_hr")))
            db.mark_synced(hevy_id=workout_id, garmin_activity_id=str(aid) if aid else None, title=workout["title"], calories=result.get("calories"), avg_hr=result.get("avg_hr"), hevy_updated_at=workout.get("updated_at"))

        start = (workout.get("start_time") or "")[:16]
        return HTMLResponse(f'<tr><td><span class="badge badge-success">✓ Synced</span></td><td>{start}</td><td><strong>{workout["title"]}</strong></td><td>{len(workout.get("exercises", []))}</td><td></td></tr>')
    except Exception as e:
        return HTMLResponse(f'<td colspan="5" style="color: var(--pico-del-color);">Failed: {e}</td>')


@app.post("/api/unsync/{hevy_id}")
async def api_unsync(request: Request, hevy_id: str):
    """Remove a workout's sync record so it can be re-synced."""
    from fastapi.responses import JSONResponse

    garmin_id = db.get_garmin_id(hevy_id)
    deleted = db.unsync(hevy_id)
    if not deleted:
        return JSONResponse({"ok": False, "error": "Not found"}, status_code=404)

    # Optionally delete the Garmin activity too
    form = await request.form()
    delete_garmin = form.get("delete_garmin") in ("true", "1", True)
    garmin_deleted = False
    if delete_garmin and garmin_id:
        try:
            config = load_config()
            from liftosaur2garmin.garmin import get_client
            client = get_client(config.get("garmin_email"))
            client.delete_activity(int(garmin_id))
            garmin_deleted = True
            logger.info("Deleted Garmin activity %s for hevy workout %s", garmin_id, hevy_id)
        except Exception as e:
            logger.warning("Failed to delete Garmin activity %s: %s", garmin_id, e)

    # Clear cached workout pages so the workouts page reflects the change
    _db = db.get_db()
    for page in range(1, 11):
        _db.set_app_config(f"hevy_workouts_page_{page}", {})

    logger.info("Unsynced workout %s (garmin_id=%s, garmin_deleted=%s)", hevy_id, garmin_id, garmin_deleted)
    return JSONResponse({"ok": True, "garmin_deleted": garmin_deleted})


@app.post("/api/unsync-all")
async def api_unsync_all(request: Request):
    """Remove ALL sync records. Does not delete from Garmin."""
    from fastapi.responses import JSONResponse

    form = await request.form()
    confirm = form.get("confirm", "")
    if confirm != "RESET":
        return JSONResponse({"ok": False, "error": "Send confirm=RESET to proceed"}, status_code=400)

    count = db.unsync_all()

    # Clear cached workout pages
    _db = db.get_db()
    for page in range(1, 11):
        _db.set_app_config(f"hevy_workouts_page_{page}", {})

    logger.info("Unsynced all %d workouts", count)
    return JSONResponse({"ok": True, "count": count})


@app.post("/api/toggle-autosync", response_class=HTMLResponse)
async def api_toggle_autosync(request: Request):
    form = await request.form()
    enabled_raw = form.get("enabled", "false")
    enabled = enabled_raw in ("true", "True", "1", True)
    interval = _parse_autosync_interval(form.get("interval", 120))

    config = load_config()
    config.setdefault("auto_sync", {})
    config["auto_sync"]["enabled"] = enabled
    config["auto_sync"]["interval_minutes"] = interval
    save_config(config)

    # Persist auto-sync state to DB on cloud deployments (filesystem is read-only)
    if db.get_database_url():
        try:
            import json as _json
            _db = db.get_db()
            if hasattr(_db, '_get_conn'):
                with _db._get_conn() as conn:
                    with conn.cursor() as cur:
                        cur.execute("""
                            INSERT INTO platform_credentials (platform, auth_type, credentials, status)
                            VALUES ('auto_sync', 'config', %s, 'active')
                            ON CONFLICT (platform) DO UPDATE SET credentials = EXCLUDED.credentials
                        """, (_json.dumps({"enabled": enabled, "interval_minutes": interval}),))
                    conn.commit()
        except Exception as e:
            logger.warning("Failed to persist auto-sync state: %s", e)

    if enabled:
        if os.environ.get("VERCEL") and os.environ.get("GITHUB_PAT"):
            ok, msg = await _setup_github_actions(interval_minutes=interval)
            if ok:
                logger.info("GitHub Actions auto-sync configured (interval=%dmin)", interval)
            else:
                logger.warning("Failed to set up GitHub Actions: %s", msg)
        else:
            _schedule_autosync(interval)
        logger.info("Auto-sync enabled: every %d min", interval)
    else:
        _stop_autosync()
        # On Vercel: delete the sync workflow to stop the cron
        if os.environ.get("VERCEL") and os.environ.get("GITHUB_PAT"):
            try:
                import requests as req
                pat = os.environ.get("GITHUB_PAT")
                owner = os.environ.get("VERCEL_GIT_REPO_OWNER")
                repo_name = os.environ.get("VERCEL_GIT_REPO_SLUG")
                gh_headers = {"Authorization": f"Bearer {pat}", "Accept": "application/vnd.github+json"}
                wf = req.get(f"https://api.github.com/repos/{owner}/{repo_name}/contents/.github/workflows/sync.yml",
                             headers=gh_headers, timeout=10)
                if wf.status_code == 200:
                    req.delete(f"https://api.github.com/repos/{owner}/{repo_name}/contents/.github/workflows/sync.yml",
                               headers=gh_headers, json={"message": "disable auto-sync", "sha": wf.json()["sha"]}, timeout=10)
                    logger.info("Deleted sync workflow from %s/%s", owner, repo_name)
            except Exception as e:
                logger.warning("Failed to delete sync workflow: %s", e)
        logger.info("Auto-sync disabled")

    auto_sync = _get_autosync_status()
    return _render("partials/autosync_status.html", auto_sync=auto_sync)


# ── Vercel / Cloud endpoints ──────────────────────────────────────────────


def _minutes_to_cron(minutes: int) -> str:
    """Convert an interval in minutes to a GitHub Actions cron expression.

    Supports the discrete values exposed in the dashboard select:
    30, 60, 120, 240, 360, 720, 1440. Falls back to '0 */2 * * *' for
    anything unexpected.
    """
    if minutes == 30:
        return "*/30 * * * *"
    if minutes == 60:
        return "0 * * * *"
    if minutes == 1440:
        return "0 0 * * *"
    if minutes >= 60 and minutes % 60 == 0:
        hours = minutes // 60
        return f"0 */{hours} * * *"
    return "0 */2 * * *"


def _build_sync_workflow_yaml(interval_minutes: int) -> str:
    """Build the sync.yml workflow content with the given cron interval."""
    cron = _minutes_to_cron(interval_minutes)
    return (
        "name: Sync Workouts\n\n"
        "on:\n"
        "  schedule:\n"
        f"    - cron: '{cron}'\n"
        "  workflow_dispatch: {}\n"
        "  repository_dispatch:\n"
        "    types: [sync-trigger]\n\n"
        "concurrency:\n"
        "  group: sync\n"
        "  cancel-in-progress: false\n\n"
        "jobs:\n"
        "  sync:\n"
        "    runs-on: ubuntu-latest\n"
        "    timeout-minutes: 30\n"
        "    steps:\n"
        "      - uses: actions/checkout@v4\n"
        "      - uses: actions/setup-python@v5\n"
        "        with:\n"
        "          python-version: '3.12'\n"
        "      - name: Install\n"
        "        run: pip install \".[cloud]\"\n"
        "      - name: Sync\n"
        "        env:\n"
        "          DATABASE_URL: ${{ secrets.DATABASE_URL }}\n"
        "        run: liftosaur2garmin sync\n"
    )


def _format_interval_label(minutes: int) -> str:
    """Human-friendly label for interval (e.g., '30 minutes', '1 hour', '2 hours')."""
    if minutes < 60:
        return f"{minutes} minutes"
    if minutes == 60:
        return "1 hour"
    if minutes == 1440:
        return "24 hours"
    if minutes % 60 == 0:
        return f"{minutes // 60} hours"
    return f"{minutes} minutes"


async def _setup_github_actions(interval_minutes: int = 120) -> tuple[bool, str]:
    """Configure GitHub Actions on the user's fork.

    Parallelizes independent GitHub API calls (PATCH repo, PUT actions,
    GET public-key, GET workflow) to keep latency low. Returns (ok, message).
    """
    import asyncio
    from base64 import b64encode

    pat = os.environ.get("GITHUB_PAT")
    owner = os.environ.get("VERCEL_GIT_REPO_OWNER")
    repo = os.environ.get("VERCEL_GIT_REPO_SLUG")
    database_url = db.get_database_url()

    if not pat:
        return False, "GITHUB_PAT not set"
    if not owner or not repo:
        return False, "Not deployed via Vercel (missing repo info)"
    if not database_url:
        return False, "DATABASE_URL not set"

    import requests as req

    headers = {
        "Authorization": f"Bearer {pat}",
        "Accept": "application/vnd.github+json",
    }
    base = f"https://api.github.com/repos/{owner}/{repo}"
    wf_url = f"{base}/contents/.github/workflows/sync.yml"

    # Round 1 (parallel): independent calls
    def _patch_public():
        return req.patch(base, headers=headers, json={"private": False}, timeout=10)

    def _enable_actions():
        return req.put(f"{base}/actions/permissions", headers=headers, json={"enabled": True}, timeout=10)

    def _get_public_key():
        return req.get(f"{base}/actions/secrets/public-key", headers=headers, timeout=10)

    def _get_workflow():
        return req.get(wf_url, headers=headers, timeout=10)

    try:
        _, actions_resp, pk_resp, wf_resp = await asyncio.gather(
            asyncio.to_thread(_patch_public),
            asyncio.to_thread(_enable_actions),
            asyncio.to_thread(_get_public_key),
            asyncio.to_thread(_get_workflow),
        )

        if actions_resp.status_code not in (200, 204):
            return False, f"Failed to enable Actions: HTTP {actions_resp.status_code}"
        if not pk_resp.ok:
            return False, f"Failed to get repo public key: HTTP {pk_resp.status_code}"

        # Encrypt the secret with the public key (CPU-bound, fast)
        from nacl import encoding, public

        pk_data = pk_resp.json()
        pk = public.PublicKey(pk_data["key"].encode("utf-8"), encoding.Base64Encoder())
        sealed = public.SealedBox(pk).encrypt(database_url.encode("utf-8"))
        encrypted_value = b64encode(sealed).decode("utf-8")

        sync_yml = _build_sync_workflow_yaml(interval_minutes)
        wf_payload: dict = {
            "message": f"feat: auto-sync every {_format_interval_label(interval_minutes)}",
            "content": b64encode(sync_yml.encode()).decode(),
        }
        if wf_resp.status_code == 200:
            wf_payload["sha"] = wf_resp.json().get("sha")

        # Round 2 (parallel): writes
        def _put_secret():
            return req.put(
                f"{base}/actions/secrets/DATABASE_URL",
                headers=headers,
                json={"encrypted_value": encrypted_value, "key_id": pk_data["key_id"]},
                timeout=10,
            )

        def _put_workflow():
            return req.put(wf_url, headers=headers, json=wf_payload, timeout=10)

        secret_resp, _ = await asyncio.gather(
            asyncio.to_thread(_put_secret),
            asyncio.to_thread(_put_workflow),
        )

        if secret_resp.status_code not in (200, 201, 204):
            return False, f"Failed to set DATABASE_URL secret: HTTP {secret_resp.status_code}"

        # Fire-and-forget initial sync trigger (don't block on it)
        async def _trigger_initial_sync():
            try:
                await asyncio.to_thread(
                    lambda: req.post(
                        f"{base}/dispatches",
                        headers=headers,
                        json={"event_type": "sync-trigger"},
                        timeout=10,
                    )
                )
            except Exception:
                pass

        asyncio.create_task(_trigger_initial_sync())

        return True, f"Auto-sync enabled! Workouts will sync every {_format_interval_label(interval_minutes)}."
    except Exception as e:
        return False, f"Failed to set up auto-sync: {e}"


@app.post("/api/setup-actions", response_class=HTMLResponse)
async def api_setup_actions(request: Request):
    """Auto-configure GitHub Actions on the user's fork."""
    interval = 120
    try:
        form = await request.form()
        interval = int(form.get("interval", 120))
    except Exception:
        pass
    ok, msg = await _setup_github_actions(interval_minutes=interval)
    cls = "toast-success" if ok else "toast-error"
    return HTMLResponse(f'<div class="toast {cls}">{msg}</div>')


@app.post("/api/sync-one")
async def api_sync_one(request: Request):
    """Sync exactly 1 unsynced workout. Returns JSON with status."""
    from fastapi.responses import JSONResponse

    if not _acquire_sync_lock():
        return JSONResponse({"error": "Sync already running", "busy": True})

    try:
        return await _do_sync_one(request)
    finally:
        _sync_executing.release()


async def _do_sync_one(request: Request):
    """Inner sync logic, called with _sync_executing lock held."""
    from fastapi.responses import JSONResponse

    config = load_config()
    hevy_api_key = config.get("hevy_api_key")

    if not hevy_api_key:
        return JSONResponse({"error": "Hevy API key not configured"}, status_code=400)

    from liftosaur2garmin.hevy import HevyClient
    from liftosaur2garmin.garmin import get_client, upload_fit, rename_activity, set_description, generate_description
    from liftosaur2garmin.fit import generate_fit
    import tempfile

    hevy = HevyClient(api_key=hevy_api_key)

    # Find first unsynced workout, paginating through recent history
    total_count = hevy.get_workout_count()
    # Cache total for dashboard
    _db = db.get_db()
    _db.set_app_config("hevy_total", {"count": total_count})
    synced_count = db.get_synced_count()
    remaining = max(0, total_count - synced_count)

    unsynced = None
    unmapped_found: dict[str, int] = {}
    page = 1
    max_pages = min(10, (remaining // 10) + 2)  # Don't search forever
    while page <= max_pages:
        data = hevy.get_workouts(page=page, page_size=10)
        workouts = data.get("workouts", [])
        if not workouts:
            break
        # Refresh the workouts-page cache while we already have the data
        _db.set_app_config(
            f"hevy_workouts_page_{page}",
            {"workouts": workouts, "page_count": data.get("page_count", 1)},
        )
        for w in workouts:
            if not unsynced and not db.is_synced(w["id"]) and w["id"] not in _failed_ids:
                unsynced = w
            # Track unmapped exercises while we're iterating
            from liftosaur2garmin.mapper import lookup_exercise
            for ex in w.get("exercises", []):
                name = ex.get("title") or ex.get("name", "")
                if name and lookup_exercise(name)[0] == 65534:
                    unmapped_found[name] = unmapped_found.get(name, 0) + 1
        if unsynced:
            break
        if page >= data.get("page_count", page):
            break
        page += 1
    # Update unmapped cache in DB
    if unmapped_found:
        _db.set_app_config("unmapped_exercises", unmapped_found)

    if not unsynced:
        return JSONResponse({"synced": 0, "remaining": 0, "done": True})

    # Sync this one workout
    try:
        from liftosaur2garmin.garmin import find_activity_by_start_time
        garmin_client = get_client(config.get("garmin_email"))
        workout_start = unsynced.get("start_time")

        # Dedup: check if this workout already exists on Garmin before uploading.
        # Prevents duplicates when a prior sync uploaded successfully but crashed
        # before marking the workout as synced in the DB.
        existing_id = None
        if workout_start:
            existing_id = find_activity_by_start_time(garmin_client, workout_start)

        if existing_id:
            logger.info("Activity already on Garmin (%s), skipping upload for %s", existing_id, unsynced["title"])
            aid = existing_id
            # Still generate FIT to get calorie estimate
            with tempfile.TemporaryDirectory() as tmp:
                fit_path = f"{tmp}/{unsynced['id']}.fit"
                result = generate_fit(unsynced, hr_samples=None, output_path=fit_path)
            rename_activity(garmin_client, aid, unsynced["title"])
            desc = generate_description(unsynced, calories=result.get("calories"), avg_hr=result.get("avg_hr"))
            set_description(garmin_client, aid, desc)
        else:
            with tempfile.TemporaryDirectory() as tmp:
                fit_path = f"{tmp}/{unsynced['id']}.fit"
                result = generate_fit(unsynced, hr_samples=None, output_path=fit_path)
                upload_result = upload_fit(garmin_client, fit_path, workout_start=workout_start)
                aid = upload_result.get("activity_id")
                if aid:
                    rename_activity(garmin_client, aid, unsynced["title"])
                    desc = generate_description(unsynced, calories=result.get("calories"), avg_hr=result.get("avg_hr"))
                    set_description(garmin_client, aid, desc)

        db.mark_synced(
            hevy_id=unsynced["id"],
            garmin_activity_id=str(aid) if aid else None,
            title=unsynced["title"],
            calories=result.get("calories"),
            avg_hr=result.get("avg_hr"),
            hevy_updated_at=unsynced.get("updated_at"),
        )

        remaining = hevy.get_workout_count() - db.get_synced_count()
        return JSONResponse({"synced": 1, "title": unsynced["title"], "remaining": max(0, remaining), "done": remaining <= 0})
    except Exception as e:
        logger.error("Sync failed for %s: %s", unsynced.get("title", "?"), str(e)[:300])
        err = str(e)

        # Hevy API key invalid — hard stop, point to setup
        from liftosaur2garmin.hevy import HevyAuthError
        if isinstance(e, HevyAuthError):
            return JSONResponse({"synced": 0, "error": "Hevy API key is invalid or expired. Go to Setup to enter a new key.", "remaining": -1, "done": False}, status_code=401)

        # Auth errors are hard stops — user needs to reconnect
        if "Login failed" in err or "OAuth" in err or "token" in err:
            return JSONResponse({"synced": 0, "error": "Garmin connection expired. Go to Setup to reconnect.", "remaining": -1, "done": False}, status_code=500)

        # EU consent error — hard stop with clear instructions
        if "upload consent" in err.lower() or "EU location" in err:
            return JSONResponse({
                "synced": 0,
                "error": "Garmin requires upload consent. Open connect.garmin.com/modern/settings, scroll to Data, enable Device Upload, then try again.",
                "eu_consent": True,
                "remaining": -1, "done": False
            }, status_code=500)

        # Other upload errors — skip this workout for now, don't mark as synced
        # Track in-memory so we don't retry it in the same sync session
        _failed_ids.add(unsynced["id"])
        remaining = hevy.get_workout_count() - db.get_synced_count() - len(_failed_ids)
        logger.warning("Skipping failed workout %s (will retry next session), %d remaining", unsynced["title"], remaining)
        return JSONResponse({"synced": 0, "skipped_error": True, "title": unsynced["title"], "remaining": max(0, remaining), "done": remaining <= 0})




@app.get("/api/cron/sync")
async def cron_sync(request: Request):
    """Vercel cron endpoint. Syncs 1 workout per invocation."""
    from fastapi.responses import JSONResponse

    # Vercel sets CRON_SECRET to verify cron requests
    cron_secret = os.environ.get("CRON_SECRET")
    if cron_secret:
        auth = request.headers.get("authorization")
        if auth != f"Bearer {cron_secret}":
            return JSONResponse({"error": "Unauthorized"}, status_code=401)

    # Reuse sync-one logic
    return await api_sync_one(request)


def run_server(host: str = "0.0.0.0", port: int = 8000) -> None:
    import uvicorn
    logging.basicConfig(format="%(message)s", level=logging.INFO, force=True)
    logger.info("Starting liftosaur2garmin dashboard at http://localhost:%d", port)
    uvicorn.run(app, host=host, port=port, log_level="warning")
