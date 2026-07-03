import glob
import hmac
import os
import re
import subprocess
import sys

try:
    import fcntl  # POSIX advisory file locking (PythonAnywhere / Linux).
except ImportError:  # pragma: no cover - Windows has no fcntl; deploy is PA-only.
    fcntl = None

from flask import Flask, request

app = Flask(__name__)

# Project root — used by /api/deploy to scope git commands and by
# /api/health to report the deployed commit. api/index.py is at
# <repo>/api/index.py, so two dirname() calls give us the repo root.
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _commit_sha() -> str:
    """Short SHA of the checked-out commit, or "" when git is unavailable.

    Computed once at import (= worker boot), so it reflects the code the
    worker is actually RUNNING — not whatever a later `git pull` left on
    disk. That makes /api/health the definitive "did the deploy go live?"
    probe: the reported SHA changes only after a successful worker reload.
    """
    try:
        result = subprocess.run(
            ["git", "-C", _PROJECT_ROOT, "rev-parse", "--short=7", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (subprocess.SubprocessError, OSError):
        pass
    return ""


_COMMIT_SHA = _commit_sha()


@app.route("/api/health")
@app.route("/api/index")
def health():
    # Keep this endpoint dependency-free so uptime pings don't trigger
    # Telegram/store/AI client init. Body is "OK <sha>" so one curl
    # answers both "is it up?" and "which commit is live?".
    return ("OK " + _COMMIT_SHA).strip(), 200


@app.route("/api/webhook", methods=["POST"])
def webhook():
    # Verify the secret BEFORE any heavy imports. bot.config only reads
    # env vars, no network. bot.clients/handlers/telebot would otherwise
    # trigger bot.get_me() on every cold start — including for forged or
    # mis-secreted POSTs.
    from bot.config import WEBHOOK_SECRET

    if WEBHOOK_SECRET:
        token = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if not hmac.compare_digest(token, WEBHOOK_SECRET):
            return "Forbidden", 403
    elif not _WARNED_NO_WEBHOOK_SECRET[0]:
        # First-request warning so operators notice the fail-open path.
        # WEBHOOK_SECRET stays optional for backwards compat + local
        # teaching, but anyone running this in production should set it.
        print(
            "WARNING: WEBHOOK_SECRET is not set. /api/webhook accepts "
            "unauthenticated POSTs — anyone who guesses the URL can forge "
            "Telegram updates. Set WEBHOOK_SECRET and re-register the "
            "webhook with secret_token=<your_secret>."
        )
        _WARNED_NO_WEBHOOK_SECRET[0] = True

    # Authenticated — now pull the heavyweight modules.
    import telebot
    import bot.handlers  # noqa: F401 — registers @bot.message_handler decorators
    from bot.clients import bot

    raw = request.get_data(as_text=True)
    try:
        update = telebot.types.Update.de_json(raw)
    except Exception as e:
        print(f"Malformed update: {e}")
        return "Bad Request", 400
    if update is None:
        return "Bad Request", 400

    update_id = getattr(update, "update_id", None)
    if update_id is not None:
        from bot.dedupe import try_acquire

        if not try_acquire(update_id):
            # Already claimed by another delivery or a prior successful run.
            return "OK", 200

    try:
        bot.process_new_updates([update])
    except Exception:
        # Processing crashed — release the dedupe claim so Telegram's
        # retry of this update_id isn't silently dropped, then let the
        # 500 propagate so Telegram knows to retry.
        if update_id is not None:
            from bot.dedupe import release

            release(update_id)
        raise
    return "OK", 200


# Module-level flag so the WEBHOOK_SECRET unset warning logs once per
# worker boot instead of on every request.
_WARNED_NO_WEBHOOK_SECRET = [False]


# Lock file path. fcntl.flock against this file serializes /api/deploy
# calls so two concurrent GitHub Actions runs can't race `git pull` in
# the same worktree.
_DEPLOY_LOCK_PATH = os.path.join(_PROJECT_ROOT, ".deploy.lock")


def _lock_deploy_nb(fd: int) -> None:
    """Take an exclusive, non-blocking advisory lock on `fd`.

    Raises BlockingIOError if another deploy already holds it. No-op on
    platforms without fcntl (Windows): /api/deploy only runs on
    PythonAnywhere/Linux, where overlapping GitHub Actions deploys are
    the race this guards against. Keeping fcntl optional lets the test
    suite (and a local dev install) import api.index on Windows.
    """
    if fcntl is None:
        return
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_deploy(fd: int) -> None:
    """Release the lock taken by _lock_deploy_nb. No-op without fcntl."""
    if fcntl is None:
        return
    fcntl.flock(fd, fcntl.LOCK_UN)


def _git(args: list, timeout: int) -> subprocess.CompletedProcess:
    """Run a git command scoped to the project checkout."""
    return subprocess.run(
        ["git", "-C", _PROJECT_ROOT, *args],
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _sync_requirements(old_sha: str, new_sha: str) -> tuple:
    """Install dependencies when the deploy changed requirements.txt.

    Without this, a push that adds a dependency reloads into a worker
    that crashes on import — a deploy that "succeeded" but took the bot
    down. Returns (status line for the response, ok flag); ok=False
    means the caller must NOT reload the worker.
    """
    if not old_sha or not new_sha or old_sha == new_sha:
        return "Dependencies: unchanged", True
    diff = _git(["diff", "--name-only", f"{old_sha}..{new_sha}"], timeout=10)
    if diff.returncode != 0 or "requirements.txt" not in diff.stdout.split():
        return "Dependencies: unchanged", True
    # sys.prefix is the active virtualenv (PEP 405) — uwsgi's
    # sys.executable points at the uwsgi binary, so don't use that.
    pip = os.path.join(sys.prefix, "bin", "pip")
    if not os.path.exists(pip):
        return (
            "WARNING: requirements.txt changed but no venv pip found — "
            "install dependencies manually",
            True,
        )
    result = subprocess.run(
        [pip, "install", "-r", os.path.join(_PROJECT_ROOT, "requirements.txt")],
        capture_output=True,
        text=True,
        timeout=150,
    )
    if result.returncode != 0:
        print(f"pip install failed (rc={result.returncode}):\n{result.stderr}")
        return "pip install failed", False
    return "Dependencies: installed from requirements.txt", True


def _pa_wsgi_path() -> str:
    """Return the PythonAnywhere WSGI file path for this account, or ""
    if it can't be found. Touching that file is how PA reloads a web
    app's worker, so a resolution failure means "deployed but never
    restarted" — the caller reports it loudly instead of skipping
    silently.

    Resolution order:
      1. PA_WSGI_PATH env var — explicit override for non-default layouts
      2. $USER / $LOGNAME — present in PA's uwsgi workers today, but
         uwsgi environments are minimal and this isn't guaranteed
      3. pwd.getpwuid(os.getuid()) — POSIX, works with an empty env
      4. the /home/<user>/ prefix of the project checkout path
      5. a /var/www/*_pythonanywhere_com_wsgi.py glob — only if the
         match is unambiguous, so we can't touch the wrong app's file
    """
    override = os.environ.get("PA_WSGI_PATH", "").strip()
    if override:
        return override if os.path.exists(override) else ""

    users = []
    env_user = os.environ.get("USER") or os.environ.get("LOGNAME") or ""
    if env_user:
        users.append(env_user)
    try:
        import pwd

        users.append(pwd.getpwuid(os.getuid()).pw_name)
    except (ImportError, AttributeError, KeyError, OSError):
        pass  # Windows or exotic environment — try the next fallback
    home_match = re.match(r"^/home/([^/]+)/", _PROJECT_ROOT + "/")
    if home_match:
        users.append(home_match.group(1))

    for user in users:
        candidate = f"/var/www/{user}_pythonanywhere_com_wsgi.py"
        if os.path.exists(candidate):
            return candidate

    matches = glob.glob("/var/www/*_pythonanywhere_com_wsgi.py")
    if len(matches) == 1:
        return matches[0]
    return ""


@app.route("/api/deploy", methods=["POST"])
def deploy():
    """Auto-deploy webhook. Converges the checkout to origin's tip and
    reloads the PA worker.

    Verifies an X-Deploy-Secret header against DEPLOY_SECRET. Fail-closed:
    returns 403 if the env var is unset, so a misconfigured deploy can't
    accidentally allow arbitrary callers to trigger code execution.

    Uses `git fetch` + `git reset --hard origin/<branch>` rather than
    `git pull --ff-only`: a pull wedges permanently once the server-side
    worktree diverges (a file edited via PA's Files tab, a force-pushed
    branch, a half-finished recovery) and every deploy after that 500s
    until someone fixes the checkout by hand while the bot silently keeps
    running old code. Reset makes origin the single source of truth and
    is idempotent, so retries are always safe. Untracked files (.env,
    .webhook_secret, .deploy.lock) survive a reset — deliberately NO
    `git clean` for that reason. The flip side: edits to TRACKED files
    on the server are discarded by the next deploy; the PA checkout is a
    deploy target, not a workspace.

    Serialized via fcntl.flock so overlapping GitHub Actions runs or
    replayed valid requests can't race git in the same worktree.
    """
    from bot.config import DEPLOY_SECRET

    if not DEPLOY_SECRET:
        return "Deploy endpoint disabled (DEPLOY_SECRET unset)", 403

    provided = request.headers.get("X-Deploy-Secret", "")
    if not hmac.compare_digest(provided, DEPLOY_SECRET):
        return "Forbidden", 403

    # Acquire exclusive lock. Non-blocking — if another deploy is
    # in-flight we return 409 immediately rather than queueing up.
    lock_fd = os.open(_DEPLOY_LOCK_PATH, os.O_CREAT | os.O_RDWR, 0o644)
    locked = False
    try:
        try:
            _lock_deploy_nb(lock_fd)
            locked = True
        except BlockingIOError:
            return "Another deploy is in progress, try again shortly", 409

        def fail(step: str, result) -> tuple:
            # Don't echo raw stderr to the caller — it can leak local
            # paths or remote details. Log server-side, return generic.
            print(f"{step} failed (rc={result.returncode}):\n{result.stderr}")
            return f"{step} failed (see server log for details)", 500

        try:
            old = _git(["rev-parse", "--short=7", "HEAD"], timeout=10)
            old_sha = old.stdout.strip() if old.returncode == 0 else ""

            head = _git(["rev-parse", "--abbrev-ref", "HEAD"], timeout=10)
            branch = head.stdout.strip() if head.returncode == 0 else ""
            if not branch or branch == "HEAD":  # detached or unreadable
                branch = "main"

            fetched = _git(["fetch", "origin"], timeout=60)
            if fetched.returncode != 0:
                return fail("git fetch", fetched)

            reset = _git(["reset", "--hard", f"origin/{branch}"], timeout=30)
            if reset.returncode != 0:
                return fail("git reset", reset)

            new = _git(["rev-parse", "--short=7", "HEAD"], timeout=10)
            new_sha = new.stdout.strip() if new.returncode == 0 else ""

            deps_line, deps_ok = _sync_requirements(old_sha, new_sha)
            if not deps_ok:
                # No WSGI touch: keep the old worker (old code + old
                # deps) serving rather than reload into an ImportError.
                return f"{deps_line} (see server log for details)", 500
        except subprocess.TimeoutExpired:
            return "git/pip command timed out", 504

        # Re-register webhook in case WEBHOOK_URL or WEBHOOK_SECRET
        # changed, or a local polling session cleared the registration.
        # Best-effort: never fail the deploy on a webhook registration
        # error since the worker reload below will retry it at boot.
        try:
            from bot.clients import register_webhook

            webhook_line = register_webhook()
        except Exception as e:
            webhook_line = f"Webhook registration failed: {e}"

        # Touch the PA WSGI file so uwsgi gracefully replaces the worker —
        # that's the moment the fetched code actually goes live. This is
        # the step whose silent failure used to read as "deployed but the
        # bot didn't change", so its outcome is always reported.
        wsgi_path = _pa_wsgi_path()
        if not wsgi_path:
            reload_line = (
                "WARNING: WSGI file not found — code updated on disk but "
                "the worker was NOT restarted. Set PA_WSGI_PATH in .env to "
                "the WSGI file path shown on the PA Web tab."
            )
        else:
            try:
                os.utime(wsgi_path, None)
                reload_line = f"Reload: touched {wsgi_path}"
            except OSError as e:
                reload_line = (
                    f"WARNING: reload touch failed ({e}) — code updated "
                    "on disk but the worker was NOT restarted."
                )

        summary = f"Deployed: {old_sha or '?'} -> {new_sha or '?'} (branch {branch})"
        body = "\n".join(["OK", summary, deps_line, webhook_line, reload_line])
        return body + "\n", 200
    finally:
        # Release lock (if we acquired it) + close fd. Don't unlink the
        # lockfile — leave it for next deploy. Swallow errors from
        # LOCK_UN so they can't mask the response from the try-block.
        if locked:
            try:
                _unlock_deploy(lock_fd)
            except Exception:
                pass
        os.close(lock_fd)
