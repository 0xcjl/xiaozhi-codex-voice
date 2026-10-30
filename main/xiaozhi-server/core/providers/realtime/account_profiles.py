"""Connection-scoped Codex managed OAuth. Never imports the daily login."""

import asyncio
import os
import re
import stat
from pathlib import Path
from uuid import UUID
from .codex_session import CodexRealtimeSession


class AccountProfileError(RuntimeError):
    pass


class AccountProfiles:
    def __init__(self, root, workspace, *, factory=CodexRealtimeSession, timeout=600):
        original = Path(os.environ.get("CODEX_HOME", Path.home()/".codex")).resolve()
        requested = Path(root).absolute()
        if requested.is_symlink():
            raise AccountProfileError("profile symlinks are not allowed")
        self.root = requested.parent.resolve() / requested.name
        resolved = self.root.resolve()
        if (resolved == Path.home() or resolved in original.parents or resolved == original
                or original in resolved.parents or resolved == Path(workspace).resolve() or resolved in Path(workspace).resolve().parents
                or Path(workspace).resolve() in resolved.parents):
            raise AccountProfileError("dedicated profile root is required")
        self.workspace = Path(workspace).resolve()
        self.factory = factory
        self.timeout = timeout
        self.pending = {}
        self.outcomes = {}
        self.locks = {}
        self.closed = False
        self.status_slots = asyncio.Semaphore(4)
        self.start_slots = asyncio.Semaphore(4)

    def profile(self, owner, connection, *, create=False):
        owner = str(owner)
        if not re.fullmatch(r"[1-9][0-9]{0,19}", owner):
            raise AccountProfileError("invalid owner")
        try:
            if str(UUID(connection)) != connection:
                raise ValueError()
        except (ValueError, TypeError, AttributeError):
            raise AccountProfileError("invalid connection") from None
        # lstat every ancestor, refusing symlinks rather than following them.
        path = self.root / owner / connection
        for part in (self.root, self.root/owner, path):
            if part.is_symlink():
                raise AccountProfileError("profile symlinks are not allowed")
        if create:
            path.mkdir(parents=True, mode=0o700, exist_ok=True)
            for part in (self.root, self.root/owner, path):
                if part.stat().st_uid != os.getuid():
                    raise AccountProfileError("profile must belong to the service user")
                part.chmod(0o700)
        if path.exists() and (not path.is_dir() or stat.S_IMODE(path.stat().st_mode) & 0o077):
            raise AccountProfileError("profile permissions must be private")
        auth = path / "auth.json"
        if auth.is_symlink():
            raise AccountProfileError("credential symlinks are not allowed")
        if auth.exists() and (not auth.is_file() or auth.stat().st_uid != os.getuid() or auth.stat().st_nlink != 1):
            raise AccountProfileError("credential file must be private and service-owned")
        return path

    def _key(self, owner, connection):
        self.profile(owner, connection)
        return (str(owner), connection)

    def _lock(self, key):
        if key not in self.locks:
            if len(self.locks) >= 256:
                raise AccountProfileError("profile limit reached")
            self.locks[key] = asyncio.Lock()
        return self.locks[key]

    async def _account(self, session, *, refresh_token=False):
        result = await session._request("account/read", {"refreshToken": refresh_token}, timeout=20 if refresh_token else 8)
        account = result.get("account") or {}
        if account.get("type") != "chatgpt":
            return {"state": "signed_out"}
        return {"state": "signed_in", "email": account.get("email"), "planType": account.get("planType")}

    async def refresh(self, owner, connection):
        return await self.status(owner, connection, refresh_token=True)

    async def status(self, owner, connection, *, refresh_token=False):
        key = self._key(owner, connection)
        async with self._lock(key), self.status_slots:
            if key in self.pending:
                return {"state": "pending"}
            if not refresh_token and self.outcomes.get(key, {}).get("state") in ("expired", "failed", "cancelled"):
                return self.outcomes[key].copy()
            profile = self.profile(*key)
            if not profile.is_dir():
                return {"state": "signed_out"}
            session = self.factory(self.workspace, codex_home=profile)
            try:
                await session.open()
                result = await self._account(session, refresh_token=refresh_token)
                self._secure_auth(profile)
                if refresh_token:
                    self.outcomes.pop(key, None)
                return result
            finally:
                await session.close()

    def _secure_auth(self, profile):
        auth = profile / "auth.json"
        if auth.is_symlink():
            raise AccountProfileError("credential symlinks are not allowed")
        if auth.exists():
            auth.chmod(0o600)

    async def reauthorize(self, owner, connection):
        return await self.start(owner, connection, force=True)

    async def start(self, owner, connection, *, force=False):
        key = self._key(owner, connection)
        async with self._lock(key), self.start_slots:
            if self.closed:
                raise AccountProfileError("account service is closed")
            if key in self.pending:
                return self.pending[key]["public"].copy()
            if len(self.pending) >= 4:
                raise AccountProfileError("too many pending logins")
            profile = self.profile(*key, create=True)
            session = self.factory(self.workspace, codex_home=profile)
            try:
                await session.open()
                account = await self._account(session) if not force else {"state":"signed_out"}
                if account["state"] == "signed_in":
                    self.outcomes.pop(key, None)
                    await session.close()
                    return account
                response = await session._request("account/login/start", {"type": "chatgptDeviceCode"}, timeout=20)
                if (response.get("type") != "chatgptDeviceCode"
                        or response.get("verificationUrl") != "https://auth.openai.com/codex/device"
                        or not re.fullmatch(r"[A-Z0-9-]{4,32}", response.get("userCode", ""))
                        or not isinstance(response.get("loginId"), str)):
                    raise AccountProfileError("unexpected login response")
                public = {"state": "pending", "verificationUrl": response["verificationUrl"],
                          "userCode": response["userCode"], "expiresInSeconds": self.timeout}
                attempt = {"session": session, "login_id": response["loginId"], "public": public}
                self.pending[key] = attempt
                self.outcomes.pop(key, None)
                attempt["task"] = asyncio.create_task(self._watch(key, attempt, profile))
                return public.copy()
            except BaseException:
                await session.close()
                raise

    async def _watch(self, key, attempt, profile):
        session = attempt["session"]
        outcome = {"state": "failed"}
        try:
            async with asyncio.timeout(self.timeout):
                while True:
                    try:
                        event = await asyncio.wait_for(session.events.get(), 0.1)
                    except asyncio.TimeoutError:
                        if session.process is not None and session.process.returncode is not None:
                            break
                        continue
                    if event.get("method") == "account/login/completed":
                        params = event.get("params", {})
                        if params.get("loginId") != attempt["login_id"]:
                            continue
                        if params.get("success") is True:
                            outcome = await self._account(session)
                            self._secure_auth(profile)
                        break
        except asyncio.TimeoutError:
            outcome = {"state": "expired"}
        except asyncio.CancelledError:
            outcome = {"state": "cancelled"}
            # A user may finish official consent just as the UI cancels waiting.
            # Report an already-completed sign-in accurately; do not log it out.
            try:
                account = await self._account(session)
                if account["state"] == "signed_in":
                    outcome = account
                    self._secure_auth(profile)
            except Exception:
                pass
        except Exception:
            # Never surface upstream errors, codes, or credentials in status/logs.
            outcome = {"state": "failed"}
        finally:
            try:
                if outcome["state"] != "signed_in":
                    try:
                        await session._request("account/login/cancel", {"loginId": attempt["login_id"]}, timeout=3)
                    except Exception:
                        pass
            finally:
                try:
                    await session.close()
                finally:
                    self.outcomes[key] = outcome
                    self.pending.pop(key, None)
                    attempt["public"].clear()

    async def cancel(self, owner, connection):
        key = self._key(owner, connection)
        async with self._lock(key):
            attempt = self.pending.get(key)
            if attempt:
                attempt["task"].cancel()
                await asyncio.gather(attempt["task"], return_exceptions=True)
                # A task cancelled before its first step never runs its finally.
                if self.pending.get(key) is attempt:
                    try:
                        await attempt["session"]._request("account/login/cancel", {"loginId": attempt["login_id"]}, timeout=3)
                    except Exception:
                        pass
                    finally:
                        try:
                            await attempt["session"].close()
                        finally:
                            self.pending.pop(key, None)
                            attempt["public"].clear()
                            self.outcomes[key] = {"state": "cancelled"}
            return self.outcomes.get(key, {"state": "cancelled"}).copy()

    async def close(self):
        self.closed = True
        await asyncio.gather(*(self.cancel(*key) for key in list(self.pending)))
