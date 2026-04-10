"""Garmin Connect auth and API helpers used by liftosaur2garmin."""

from __future__ import annotations

import base64
import io
import json
import logging
import os
import secrets
import time
from pathlib import Path
from typing import Any

import requests

logger = logging.getLogger("liftosaur2garmin")

TOKEN_SCHEMA_VERSION = 1
TOKEN_FILE_NAME = "garmin_tokens.json"

MOBILE_SSO_CLIENT_ID = "GCM_ANDROID_DARK"
MOBILE_SSO_SERVICE_URL = "https://mobile.integration.garmin.com/gcm/android"
MOBILE_SSO_USER_AGENT = (
    "Mozilla/5.0 (Linux; Android 13; sdk_gphone64_arm64 Build/TE1A.220922.025; wv) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/132.0.0.0 Mobile Safari/537.36"
)
PORTAL_SSO_CLIENT_ID = "GarminConnect"
PORTAL_SSO_SERVICE_URL = "https://connect.garmin.com/app"
DESKTOP_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)
NATIVE_API_USER_AGENT = "GCM-Android-5.23"
NATIVE_X_GARMIN_USER_AGENT = (
    "com.garmin.android.apps.connectmobile/5.23; ; Google/sdk_gphone64_arm64/google; "
    "Android/33; Dalvik/2.1.0"
)
DI_TOKEN_URL = "https://diauth.garmin.com/di-oauth2-service/oauth/token"
DI_GRANT_TYPE = "https://connectapi.garmin.com/di-oauth2-service/oauth/grant/service_ticket"
DI_CLIENT_IDS = (
    "GARMIN_CONNECT_MOBILE_ANDROID_DI_2025Q2",
    "GARMIN_CONNECT_MOBILE_ANDROID_DI_2024Q4",
    "GARMIN_CONNECT_MOBILE_ANDROID_DI",
)


class GarminConnectConnectionError(Exception):
    """Raised when Garmin communication fails."""


class GarminConnectTooManyRequestsError(Exception):
    """Raised when Garmin rate limits requests."""


class GarminConnectAuthenticationError(Exception):
    """Raised when Garmin authentication fails."""


class GarminConnectInvalidFileFormatError(Exception):
    """Raised when a file format is unsupported."""


class GarminNeedsMFA(Exception):
    """Raised when Garmin requires a second factor to complete login."""

    def __init__(self, login_state: "GarminAuthSession"):
        super().__init__("Garmin requires a verification code")
        self.login_state = login_state


class RateLimiter:
    """Small retry helper for Garmin API calls."""

    def __init__(self, delay: float = 1.0, max_retries: int = 0, base_wait: float = 30.0):
        self.delay = delay
        self.max_retries = max_retries
        self.base_wait = base_wait

    def call(self, func, *args, **kwargs):
        attempt = 0
        while True:
            if self.delay and attempt > 0:
                time.sleep(self.delay)
            try:
                return func(*args, **kwargs)
            except GarminConnectTooManyRequestsError:
                if attempt >= self.max_retries:
                    raise
                wait_time = self.base_wait * (attempt + 1)
                logger.warning("Rate limited by Garmin. Retrying in %ss (attempt %d/%d)...", wait_time, attempt + 1, self.max_retries)
                time.sleep(wait_time)
                attempt += 1


_limiter = RateLimiter(delay=1.0, max_retries=3, base_wait=30)


def _native_headers(extra: dict[str, str] | None = None) -> dict[str, str]:
    headers = {
        "User-Agent": NATIVE_API_USER_AGENT,
        "X-Garmin-User-Agent": NATIVE_X_GARMIN_USER_AGENT,
        "X-Garmin-Paired-App-Version": "10861",
        "X-Garmin-Client-Platform": "Android",
        "X-App-Ver": "10861",
        "X-Lang": "en",
        "X-GCExperience": "GC5",
        "Accept-Language": "en-US,en;q=0.9",
    }
    if extra:
        headers.update(extra)
    return headers


def _browser_headers() -> dict[str, str]:
    return {"User-Agent": DESKTOP_USER_AGENT}


def _build_basic_auth(client_id: str) -> str:
    return "Basic " + base64.b64encode(f"{client_id}:".encode()).decode()


def _token_path(token_dir: str = "~/.garminconnect") -> Path:
    path = Path(token_dir).expanduser()
    if path.is_dir() or not path.name.endswith(".json"):
        return path / TOKEN_FILE_NAME
    return path


def _serialize_cookies(jar) -> list[dict[str, Any]]:
    cookies: list[dict[str, Any]] = []
    for cookie in jar:
        cookies.append(
            {
                "name": cookie.name,
                "value": cookie.value,
                "domain": cookie.domain,
                "path": cookie.path,
                "secure": cookie.secure,
                "expires": cookie.expires,
            }
        )
    return cookies


def _load_cookies(session: requests.Session, items: list[dict[str, Any]] | None) -> None:
    if not items:
        return
    for item in items:
        session.cookies.set(
            item["name"],
            item["value"],
            domain=item.get("domain"),
            path=item.get("path", "/"),
            secure=bool(item.get("secure")),
            expires=item.get("expires"),
        )


class GarminAuthSession:
    """Requests-based Garmin auth session with MFA support."""

    def __init__(self, domain: str = "garmin.com"):
        self.domain = domain
        self._sso = f"https://sso.{domain}"
        self._connect = f"https://connect.{domain}"
        self._connectapi = f"https://connectapi.{domain}"
        self.session = requests.Session()
        adapter = requests.adapters.HTTPAdapter(pool_connections=20, pool_maxsize=20)
        self.session.mount("https://", adapter)
        self.di_token: str | None = None
        self.di_refresh_token: str | None = None
        self.di_client_id: str | None = None
        self.jwt_web: str | None = None
        self.csrf_token: str | None = None
        self.display_name: str | None = None
        self.full_name: str | None = None
        self._mfa_method = "email"
        self._mfa_url: str | None = None
        self._mfa_params: dict[str, str] | None = None
        self._mfa_headers: dict[str, str] | None = None

    @property
    def is_authenticated(self) -> bool:
        return bool(self.di_token or self.jwt_web)

    def login(
        self,
        email: str,
        password: str,
        *,
        prompt_mfa=None,
        return_on_mfa: bool = False,
    ) -> tuple[str | None, "GarminAuthSession" | None]:
        last_error: Exception | None = None
        for method in (self._portal_web_login, self._mobile_login):
            try:
                return _limiter.call(
                    method,
                    email,
                    password,
                    prompt_mfa=prompt_mfa,
                    return_on_mfa=return_on_mfa,
                )
            except GarminNeedsMFA:
                raise
            except Exception as exc:
                last_error = exc
                logger.warning("Garmin login strategy failed: %s", exc)
        
        if last_error:
            raise last_error
            
        raise GarminConnectConnectionError("All Garmin login strategies failed.")

    def resume_login(self, mfa_code: str) -> None:
        if not self._mfa_url or not self._mfa_params or not self._mfa_headers:
            raise GarminConnectAuthenticationError("No Garmin MFA login is pending")
        payload = {
            "mfaMethod": self._mfa_method,
            "mfaVerificationCode": mfa_code,
            "rememberMyBrowser": True,
            "reconsentList": [],
            "mfaSetup": False,
        }
        def _do_resume():
            resp = self.session.post(
                self._mfa_url,
                params=self._mfa_params,
                headers=self._mfa_headers,
                json=payload,
                timeout=30,
            )
            if resp.status_code == 429:
                raise GarminConnectTooManyRequestsError("Garmin MFA verification was rate limited")
            return _json_or_raise(resp, "Garmin MFA verification failed")
            
        data = _limiter.call(_do_resume)
        if data.get("responseStatus", {}).get("type") != "SUCCESSFUL":
            raise GarminConnectAuthenticationError(f"MFA verification failed: {data}")
        ticket = data.get("serviceTicketId")
        if not ticket:
            raise GarminConnectAuthenticationError("Garmin MFA succeeded but no service ticket was returned")
        service_url = (
            PORTAL_SSO_SERVICE_URL
            if "/portal/" in self._mfa_url
            else MOBILE_SSO_SERVICE_URL
        )
        self._establish_session(ticket, service_url=service_url)
        self._load_profile()

    def token_payload(self, email: str | None = None) -> dict[str, Any]:
        return {
            "schema_version": TOKEN_SCHEMA_VERSION,
            "kind": "garmin_native_auth",
            "email": email or "",
            "auth": {
                "di_token": self.di_token,
                "di_refresh_token": self.di_refresh_token,
                "di_client_id": self.di_client_id,
                "jwt_web": self.jwt_web,
                "csrf_token": self.csrf_token,
                "display_name": self.display_name,
                "full_name": self.full_name,
                "cookies": _serialize_cookies(self.session.cookies),
            },
        }

    def load_payload(self, payload: dict[str, Any]) -> None:
        if payload.get("schema_version") != TOKEN_SCHEMA_VERSION:
            raise GarminConnectConnectionError("Unsupported Garmin token schema version")
        auth = payload.get("auth")
        if not isinstance(auth, dict):
            raise GarminConnectConnectionError("Garmin token payload is missing auth data")
        self.di_token = auth.get("di_token")
        self.di_refresh_token = auth.get("di_refresh_token")
        self.di_client_id = auth.get("di_client_id")
        self.jwt_web = auth.get("jwt_web")
        self.csrf_token = auth.get("csrf_token")
        self.display_name = auth.get("display_name")
        self.full_name = auth.get("full_name")
        _load_cookies(self.session, auth.get("cookies"))
        if not self.is_authenticated:
            raise GarminConnectAuthenticationError("Garmin token payload does not contain usable auth state")

    def save(self, path: str, email: str | None = None) -> Path:
        token_path = _token_path(path)
        token_path.parent.mkdir(parents=True, exist_ok=True)
        token_path.write_text(json.dumps(self.token_payload(email), indent=2))
        return token_path

    def load(self, path: str) -> None:
        self.load_payload(json.loads(_token_path(path).read_text()))

    def _portal_web_login(
        self,
        email: str,
        password: str,
        *,
        prompt_mfa=None,
        return_on_mfa: bool = False,
    ) -> tuple[str | None, "GarminAuthSession" | None]:
        signin_url = f"{self._sso}/portal/sso/en-US/sign-in"
        browser_headers = _browser_headers()
        get_headers = {
            **browser_headers,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        }
        self.session.get(
            signin_url,
            params={"clientId": PORTAL_SSO_CLIENT_ID, "service": PORTAL_SSO_SERVICE_URL},
            headers=get_headers,
            timeout=30,
        )
        params = {
            "clientId": PORTAL_SSO_CLIENT_ID,
            "locale": "en-US",
            "service": PORTAL_SSO_SERVICE_URL,
        }
        headers = {
            **browser_headers,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
            "Content-Type": "application/json",
            "Origin": self._sso,
            "Referer": f"{signin_url}?clientId={PORTAL_SSO_CLIENT_ID}&service={PORTAL_SSO_SERVICE_URL}",
        }
        response = self.session.post(
            f"{self._sso}/portal/api/login",
            params=params,
            headers=headers,
            json={
                "username": email,
                "password": password,
                "rememberMe": True,
                "captchaToken": "",
            },
            timeout=30,
        )
        if response.status_code == 429:
            raise GarminConnectTooManyRequestsError("Garmin portal login was rate limited")
        data = _json_or_raise(response, "Garmin portal login failed")
        response_type = data.get("responseStatus", {}).get("type")
        if response_type == "MFA_REQUIRED":
            self._mfa_method = data.get("customerMfaInfo", {}).get("mfaLastMethodUsed", "email")
            self._mfa_url = f"{self._sso}/portal/api/mfa/verifyCode"
            self._mfa_params = params
            self._mfa_headers = headers
            if return_on_mfa:
                return "needs_mfa", self
            if prompt_mfa:
                self.resume_login(prompt_mfa())
                return None, None
            raise GarminNeedsMFA(self)
        if response_type == "SUCCESSFUL":
            ticket = data.get("serviceTicketId")
            if not ticket:
                raise GarminConnectAuthenticationError("Garmin login succeeded but no service ticket was returned")
            self._establish_session(ticket, service_url=PORTAL_SSO_SERVICE_URL)
            self._load_profile()
            return None, None
        if response_type == "INVALID_USERNAME_PASSWORD":
            raise GarminConnectAuthenticationError("Invalid Garmin email or password")
        raise GarminConnectConnectionError(f"Garmin portal login failed: {data}")

    def _mobile_login(
        self,
        email: str,
        password: str,
        *,
        prompt_mfa=None,
        return_on_mfa: bool = False,
    ) -> tuple[str | None, "GarminAuthSession" | None]:
        signin_url = f"{self._sso}/mobile/sso/en_US/sign-in"
        self.session.headers.update(
            {
                "User-Agent": MOBILE_SSO_USER_AGENT,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            }
        )
        self.session.get(
            signin_url,
            params={"clientId": MOBILE_SSO_CLIENT_ID, "service": MOBILE_SSO_SERVICE_URL},
            timeout=30,
        )
        params = {
            "clientId": MOBILE_SSO_CLIENT_ID,
            "locale": "en-US",
            "service": MOBILE_SSO_SERVICE_URL,
        }
        headers = {
            "User-Agent": MOBILE_SSO_USER_AGENT,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
            "Content-Type": "application/json",
            "Origin": self._sso,
            "Referer": f"{signin_url}?clientId={MOBILE_SSO_CLIENT_ID}&service={MOBILE_SSO_SERVICE_URL}",
        }
        response = self.session.post(
            f"{self._sso}/mobile/api/login",
            params=params,
            headers=headers,
            json={
                "username": email,
                "password": password,
                "rememberMe": True,
                "captchaToken": "",
            },
            timeout=30,
        )
        if response.status_code == 429:
            raise GarminConnectTooManyRequestsError("Garmin mobile login was rate limited")
        data = _json_or_raise(response, "Garmin mobile login failed")
        response_type = data.get("responseStatus", {}).get("type")
        if response_type == "MFA_REQUIRED":
            self._mfa_method = data.get("customerMfaInfo", {}).get("mfaLastMethodUsed", "email")
            self._mfa_url = f"{self._sso}/mobile/api/mfa/verifyCode"
            self._mfa_params = params
            self._mfa_headers = headers
            if return_on_mfa:
                return "needs_mfa", self
            if prompt_mfa:
                self.resume_login(prompt_mfa())
                return None, None
            raise GarminNeedsMFA(self)
        if response_type == "SUCCESSFUL":
            ticket = data.get("serviceTicketId")
            if not ticket:
                raise GarminConnectAuthenticationError("Garmin login succeeded but no service ticket was returned")
            self._establish_session(ticket, service_url=MOBILE_SSO_SERVICE_URL)
            self._load_profile()
            return None, None
        if response_type == "INVALID_USERNAME_PASSWORD":
            raise GarminConnectAuthenticationError("Invalid Garmin email or password")
        raise GarminConnectConnectionError(f"Garmin mobile login failed: {data}")

    def _establish_session(self, ticket: str, *, service_url: str) -> None:
        self._exchange_service_ticket(ticket, service_url=service_url)

    def _exchange_service_ticket(self, ticket: str, *, service_url: str) -> None:
        last_error: str | None = None
        for client_id in DI_CLIENT_IDS:
            def _do_exchange():
                resp = requests.post(
                    DI_TOKEN_URL,
                    headers=_native_headers(
                        {
                            "Authorization": _build_basic_auth(client_id),
                            "Accept": "application/json,text/html;q=0.9,*/*;q=0.8",
                            "Content-Type": "application/x-www-form-urlencoded",
                            "Cache-Control": "no-cache",
                        }
                    ),
                    data={
                        "client_id": client_id,
                        "service_ticket": ticket,
                        "grant_type": DI_GRANT_TYPE,
                        "service_url": service_url,
                    },
                    timeout=30,
                )
                if resp.status_code == 429:
                    raise GarminConnectTooManyRequestsError("Garmin DI token exchange was rate limited")
                return resp
                
            response = _limiter.call(_do_exchange)
            if not response.ok:
                last_error = f"{response.status_code} {response.text[:200]}"
                continue
            data = _json_or_raise(response, "Garmin DI token exchange failed")
            self.di_token = data.get("access_token")
            self.di_refresh_token = data.get("refresh_token")
            self.di_client_id = self._extract_client_id_from_jwt(self.di_token) or client_id
            if self.di_token:
                return
        raise GarminConnectAuthenticationError(
            f"Garmin token exchange failed for all client IDs: {last_error}"
        )

    def _extract_client_id_from_jwt(self, token: str | None) -> str | None:
        if not token:
            return None
        try:
            parts = token.split(".")
            if len(parts) < 2:
                return None
            payload_b64 = parts[1] + "=" * (-len(parts[1]) % 4)
            payload = json.loads(base64.urlsafe_b64decode(payload_b64).decode())
            value = payload.get("client_id")
            return str(value) if value else None
        except Exception:
            return None

    def _token_expires_soon(self) -> bool:
        if not self.di_token:
            return False
        try:
            parts = self.di_token.split(".")
            if len(parts) < 2:
                return False
            payload_b64 = parts[1] + "=" * (-len(parts[1]) % 4)
            payload = json.loads(base64.urlsafe_b64decode(payload_b64).decode())
            exp = payload.get("exp")
            return bool(exp and time.time() > (int(exp) - 900))
        except Exception:
            return False

    def _refresh_di_token(self) -> None:
        if not self.di_refresh_token or not self.di_client_id:
            raise GarminConnectAuthenticationError("No Garmin refresh token is available")
        response = requests.post(
            DI_TOKEN_URL,
            headers=_native_headers(
                {
                    "Authorization": _build_basic_auth(self.di_client_id),
                    "Accept": "application/json",
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Cache-Control": "no-cache",
                }
            ),
            data={
                "grant_type": "refresh_token",
                "client_id": self.di_client_id,
                "refresh_token": self.di_refresh_token,
            },
            timeout=30,
        )
        if not response.ok:
            raise GarminConnectAuthenticationError(
                f"Garmin token refresh failed: {response.status_code} {response.text[:200]}"
            )
        data = _json_or_raise(response, "Garmin token refresh failed")
        self.di_token = data.get("access_token")
        self.di_refresh_token = data.get("refresh_token", self.di_refresh_token)
        self.di_client_id = self._extract_client_id_from_jwt(self.di_token) or self.di_client_id

    def _load_profile(self) -> None:
        for path in ("/userprofile-service/socialProfile", "/userprofile-service/userprofile/user-settings"):
            try:
                profile = self.request("GET", path).json()
                if isinstance(profile, dict):
                    if "userData" in profile:
                        self.display_name = profile["userData"].get("displayName", self.display_name)
                        self.full_name = profile["userData"].get("fullName", self.full_name)
                    else:
                        self.display_name = profile.get("displayName", self.display_name)
                        self.full_name = profile.get("fullName", self.full_name)
                if self.display_name:
                    break
            except Exception as e:
                logger.debug("Could not load profile from %s: %s", path, e)

    def _api_headers(self) -> dict[str, str]:
        if self.di_token:
            return _native_headers(
                {
                    "Authorization": f"Bearer {self.di_token}",
                    "Accept": "application/json",
                }
            )
        raise GarminConnectAuthenticationError("No Garmin bearer token is available")

    def request(self, method: str, path: str, **kwargs):
        if self._token_expires_soon():
            self._refresh_di_token()
        headers = self._api_headers()
        custom_headers = kwargs.pop("headers", {})
        headers.update(custom_headers)
        url = f"{self._connectapi}/{path.lstrip('/')}"
        timeout = kwargs.pop("timeout", 15)
        response = requests.request(method, url, headers=headers, timeout=timeout, **kwargs)
        if response.status_code == 401 and self.di_refresh_token:
            self._refresh_di_token()
            headers = self._api_headers()
            headers.update(custom_headers)
            response = requests.request(method, url, headers=headers, timeout=timeout, **kwargs)
        if response.status_code == 429:
            raise GarminConnectTooManyRequestsError("Garmin API rate limit exceeded")
        if response.status_code >= 400:
            _raise_api_error(response)
        return response


class GarminClient:
    """Small Garmin API wrapper for the endpoints this app needs."""

    activities_url = "/activitylist-service/activities/search/activities"
    activity_url = "/activity-service/activity"
    upload_url = "/upload-service/upload"
    user_settings_url = "/userprofile-service/userprofile/user-settings"
    heart_rates_url = "/wellness-service/wellness/dailyHeartRate"

    def __init__(self, auth: GarminAuthSession):
        self.auth = auth
        self.display_name = auth.display_name
        self.full_name = auth.full_name

    def upload_activity(self, activity_path: str) -> Any:
        path = Path(activity_path)
        if not path.exists():
            raise FileNotFoundError(f"File not found: {activity_path}")
        if not path.is_file():
            raise ValueError(f"path is not a file: {activity_path}")
        extension = path.suffix.lstrip(".").upper()
        if extension != "FIT":
            raise GarminConnectInvalidFileFormatError(f"Invalid file format '{path.suffix}'")
        with path.open("rb") as handle:
            response = self.auth.request(
                "POST",
                self.upload_url,
                files={"file": (path.name, handle)},
            )
        return _json_response(response)

    def set_activity_name(self, activity_id: str | int, title: str) -> Any:
        response = self.auth.request(
            "PUT",
            f"{self.activity_url}/{activity_id}",
            json={"activityId": activity_id, "activityName": title},
        )
        return _json_response(response)

    def get_activities(self, start: int = 0, limit: int = 20, activitytype: str | None = None) -> list[Any]:
        params = {"start": str(start), "limit": str(limit)}
        if activitytype:
            params["activityType"] = str(activitytype)
        response = self.auth.request("GET", self.activities_url, params=params)
        data = _json_response(response)
        return data or []

    def get_activity(self, activity_id: str | int) -> dict[str, Any]:
        response = self.auth.request("GET", f"{self.activity_url}/{activity_id}")
        return _json_response(response)

    def get_exercise_sets(self, activity_id: str | int) -> dict[str, Any]:
        response = self.auth.request("GET", f"{self.activity_url}/{activity_id}/exerciseSets")
        return _json_response(response)

    def put_exercise_sets(self, activity_id: str | int, exercise_sets: list[dict[str, Any]]) -> Any:
        payload = {"activityId": int(activity_id), "exerciseSets": exercise_sets}
        response = self.auth.request("PUT", f"{self.activity_url}/{activity_id}/exerciseSets", json=payload)
        return _json_response(response)

    def get_heart_rates(self, cdate: str) -> dict[str, Any]:
        if not self.display_name:
            self.auth._load_profile()
            self.display_name = self.auth.display_name
        if not self.display_name:
            raise GarminConnectAuthenticationError("Garmin display name is not available")
        response = self.auth.request(
            "GET",
            f"{self.heart_rates_url}/{self.display_name}",
            params={"date": cdate},
        )
        return _json_response(response)

    def get_user_profile(self) -> dict[str, Any]:
        response = self.auth.request("GET", self.user_settings_url)
        return _json_response(response)

    def delete_activity(self, activity_id: int) -> Any:
        response = self.auth.request("DELETE", f"{self.activity_url}/{activity_id}")
        return _json_response(response)

    def put_json(self, path: str, payload: dict[str, Any]) -> Any:
        response = self.auth.request("PUT", path, json=payload)
        return _json_response(response)

    def post_files(self, path: str, files: dict[str, Any]) -> Any:
        response = self.auth.request("POST", path, files=files)
        return _json_response(response)

    def export_payload(self, email: str | None = None) -> dict[str, Any]:
        return self.auth.token_payload(email)


def _json_or_raise(response: requests.Response, message: str) -> dict[str, Any]:
    try:
        return response.json()
    except Exception as exc:
        raise GarminConnectConnectionError(f"{message}: HTTP {response.status_code}") from exc


def _json_response(response: requests.Response) -> Any:
    if response.status_code == 204 or not response.content:
        return {}
    return response.json()


def _raise_api_error(response: requests.Response) -> None:
    message = f"Garmin API Error {response.status_code}"
    try:
        data = response.json()
        if isinstance(data, dict):
            detail = (
                data.get("message")
                or data.get("content")
                or data.get("detailedImportResult", {}).get("failures", [{}])[0].get("messages", [""])[0]
            )
            if detail:
                message = f"{message} - {detail}"
            else:
                message = f"{message} - {data}"
    except Exception:
        if response.text and len(response.text) < 500:
            message = f"{message} - {response.text}"
    if response.status_code == 401:
        raise GarminConnectAuthenticationError(message)
    raise GarminConnectConnectionError(message)


def _load_db_token_payload() -> dict[str, Any] | None:
    from liftosaur2garmin import db

    database_url = db.get_database_url()
    if not database_url:
        return None
    try:
        _db = db.get_db()
        if hasattr(_db, "_get_conn"):
            with _db._get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT credentials FROM platform_credentials WHERE platform = 'garmin_tokens' LIMIT 1"
                    )
                    row = cur.fetchone()
                    if row and row.get("credentials"):
                        return row["credentials"] if isinstance(row["credentials"], dict) else json.loads(row["credentials"])
    except Exception as exc:
        logger.warning("Failed to load Garmin tokens from DB: %s", exc)
    return None


def save_token_payload(payload: dict[str, Any], *, token_dir: str = "~/.garminconnect") -> Path | None:
    from liftosaur2garmin import db

    database_url = db.get_database_url()
    if database_url:
        try:
            _db = db.get_db()
            if hasattr(_db, "_get_conn"):
                with _db._get_conn() as conn:
                    with conn.cursor() as cur:
                        cur.execute(
                            """
                            INSERT INTO platform_credentials (platform, auth_type, credentials, status)
                            VALUES ('garmin_tokens', 'token_file', %s, 'active')
                            ON CONFLICT (platform) DO UPDATE
                            SET auth_type = EXCLUDED.auth_type,
                                credentials = EXCLUDED.credentials,
                                status = 'active'
                            """,
                            (json.dumps(payload),),
                        )
                    conn.commit()
                return None
        except Exception as exc:
            raise GarminConnectConnectionError(f"Failed to save Garmin tokens to DB: {exc}") from exc
    target = _token_path(token_dir)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, indent=2))
    return target


def load_token_payload(token_dir: str = "~/.garminconnect") -> dict[str, Any]:
    from liftosaur2garmin import db

    database_url = db.get_database_url()
    if database_url:
        payload = _load_db_token_payload()
        if payload is None:
            raise FileNotFoundError("No Garmin token payload found in DB")
        return payload
    return json.loads(_token_path(token_dir).read_text())


def token_file_path(token_dir: str = "~/.garminconnect") -> Path:
    return _token_path(token_dir)


def get_client(
    email: str | None = None,
    password: str | None = None,
    token_dir: str = "~/.garminconnect",
) -> GarminClient:
    auth = GarminAuthSession()
    try:
        auth.load_payload(load_token_payload(token_dir))
    except FileNotFoundError:
        if not email or not password:
            raise GarminConnectAuthenticationError("No Garmin tokens found. Connect Garmin first.")
        auth.login(email, password, prompt_mfa=None)
        save_token_payload(auth.token_payload(email), token_dir=token_dir)
    except GarminConnectAuthenticationError:
        if not email or not password:
            raise
        auth.login(email, password, prompt_mfa=None)
        save_token_payload(auth.token_payload(email), token_dir=token_dir)
    return GarminClient(auth)


def start_login(email: str, password: str, token_dir: str = "~/.garminconnect") -> dict[str, Any]:
    auth = GarminAuthSession()
    status, state = auth.login(email, password, return_on_mfa=True)
    if status == "needs_mfa" and state is not None:
        return {"status": "needs_mfa", "state": state}
    payload = auth.token_payload(email)
    save_token_payload(payload, token_dir=token_dir)
    return {"status": "connected", "payload": payload, "display_name": auth.display_name}


def finish_login(
    mfa_code: str,
    login_state: GarminAuthSession,
    email: str | None = None,
    token_dir: str = "~/.garminconnect",
) -> dict[str, Any]:
    login_state.resume_login(mfa_code)
    payload = login_state.token_payload(email)
    save_token_payload(payload, token_dir=token_dir)
    return {"status": "connected", "payload": payload, "display_name": login_state.display_name}


def export_token_payload(token_dir: str = "~/.garminconnect") -> dict[str, Any]:
    return load_token_payload(token_dir)


def upload_fit(client: GarminClient, fit_path: str | Path, workout_start: str | None = None) -> dict:
    fit_path = Path(fit_path)
    if not fit_path.exists():
        raise FileNotFoundError(f"FIT file not found: {fit_path}")
    try:
        resp = _limiter.call(client.upload_activity, str(fit_path))
    except Exception as exc:
        response = getattr(exc, "response", None)
        if response is None and exc.__cause__:
            response = getattr(exc.__cause__, "response", None)
        if response is not None:
            body = response.text[:2000] if hasattr(response, "text") else str(response)
            logger.error("Upload rejected - status=%s body=%s", getattr(response, "status_code", "?"), body)
            raise RuntimeError(
                f"Garmin upload failed ({getattr(response, 'status_code', '?')}): {body}"
            ) from exc
        logger.error("Upload failed (no response): %s", str(exc)[:300])
        raise
    upload_id = None
    activity_id = None
    if isinstance(resp, dict):
        detail = resp.get("detailedImportResult", {})
        upload_id = detail.get("uploadId")
        successes = detail.get("successes", [])
        if successes and isinstance(successes, list):
            activity_id = successes[0].get("internalId")
    if not activity_id and workout_start:
        for attempt, wait in enumerate([3, 5, 10], 1):
            time.sleep(wait)
            activity_id = find_activity_by_start_time(client, workout_start)
            if activity_id:
                break
            logger.info("  Activity not found yet (attempt %d/%d), retrying...", attempt, 3)
    return {"upload_id": upload_id, "activity_id": activity_id}


def find_activity_by_start_time(
    client: GarminClient,
    target_start: str,
    window_minutes: int = 10,
) -> int | None:
    from datetime import datetime

    try:
        target = datetime.fromisoformat(target_start.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    try:
        activities = _limiter.call(client.get_activities, 0, 10)
    except Exception:
        return None
    closest_id = None
    closest_delta = None
    for act in activities:
        act_type = act.get("activityType", {}).get("typeKey", "")
        if act_type and act_type not in ("strength_training", "other"):
            continue
        act_start_str = act.get("startTimeGMT") or act.get("startTimeLocal", "")
        try:
            if "T" not in act_start_str:
                act_start_str = act_start_str.replace(" ", "T")
            act_start = datetime.fromisoformat(act_start_str)
            target_naive = target.replace(tzinfo=None) if target.tzinfo else target
            act_naive = act_start.replace(tzinfo=None) if act_start.tzinfo else act_start
            delta = abs((act_naive - target_naive).total_seconds())
            if delta < window_minutes * 60 and (closest_delta is None or delta < closest_delta):
                closest_delta = delta
                closest_id = act.get("activityId")
        except (ValueError, TypeError):
            continue
    return closest_id


def rename_activity(client: GarminClient, activity_id: int, name: str) -> None:
    _limiter.call(client.set_activity_name, activity_id, name)
    logger.info("  Renamed activity %s to '%s'", activity_id, name)


def set_description(client: GarminClient, activity_id: int, description: str) -> None:
    url = f"/activity-service/activity/{activity_id}"
    payload = {"activityId": activity_id, "description": description}
    client.put_json(url, payload)
    time.sleep(1.0)
    logger.info("  Description set (%d chars)", len(description))


def get_exercise_sets(client: GarminClient, activity_id: int) -> list[dict]:
    data = _limiter.call(client.get_exercise_sets, activity_id)
    return data.get("exerciseSets", [])


def update_exercise_sets(client: GarminClient, activity_id: int, exercise_sets: list[dict]) -> None:
    _limiter.call(client.put_exercise_sets, activity_id, exercise_sets)
    time.sleep(1.0)
    logger.info("  Updated exercise sets for activity %s", activity_id)


def upload_image(client: GarminClient, activity_id: int, image_bytes: bytes, filename: str = "image.png") -> None:
    files = {"file": (filename, io.BytesIO(image_bytes))}
    client.post_files(f"/activity-service/activity/{activity_id}/image", files=files)
    time.sleep(1.0)
    logger.info("  Image uploaded (%dKB)", len(image_bytes) // 1024)


def generate_description(workout: dict, calories: int | None = None, avg_hr: int | None = None) -> str:
    lines: list[str] = []
    title = workout.get("title", "Workout")
    duration_s = 0
    start = workout.get("start_time") or workout.get("startTime", "")
    end = workout.get("end_time") or workout.get("endTime", "")
    if start and end:
        try:
            from datetime import datetime

            t0 = datetime.fromisoformat(start.replace("Z", "+00:00"))
            t1 = datetime.fromisoformat(end.replace("Z", "+00:00"))
            duration_s = int((t1 - t0).total_seconds())
        except Exception:
            pass
    lines.append(f"🏋️ {title}")
    if duration_s > 0:
        lines.append(f"⏱️ {duration_s // 60} min")
    if calories:
        lines.append(f"🔥 {calories} kcal")
    if avg_hr:
        lines.append(f"❤️ avg {avg_hr} bpm")
    exercises = workout.get("exercises", [])
    if exercises:
        lines.append("")
        for ex in exercises:
            name = ex.get("title") or ex.get("name", "Unknown")
            all_sets = ex.get("sets", [])
            normal = [s for s in all_sets if s.get("type") == "normal"]
            warmup = [s for s in all_sets if s.get("type") == "warmup"]
            if normal:
                weights = [s.get("weight_kg") or s.get("weight", 0) for s in normal]
                reps = [s.get("reps", 0) for s in normal]
                lines.append(f"• {name}: {len(normal)} sets · {max(weights):.1f}kg × {max(reps)}")
            elif warmup:
                s_label = "set" if len(warmup) == 1 else "sets"
                lines.append(f"• {name}: {len(warmup)} warmup {s_label}")
    lines.append("\n- synced by liftosaur2garmin")
    return "\n".join(lines)
