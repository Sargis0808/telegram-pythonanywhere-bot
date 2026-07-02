import hmac
import os
import subprocess

try:
    import fcntl  # POSIX advisory file locking (PythonAnywhere / Linux).
except ImportError:  # pragma: no cover - Windows has no fcntl; deploy is PA-only.
    fcntl = None

from flask import Flask, request

app = Flask(__name__)


@app.route("/api/health")
@app.route("/api/index")
def health():
    # Keep this endpoint dependency-free so uptime pings don't trigger
    # Telegram/store/AI client init.
    return "OK", 200


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


# Project root — used by /api/deploy to scope `git pull` correctly.
# api/index.py is at <repo>/api/index.py, so two dirname() calls give us the repo root.
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

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


def _pa_wsgi_path() -> str:
    """Return the PythonAnywhere WSGI file path for the current user, or
    empty string if not running on PA. Touching this file triggers a
    graceful worker reload on the next request.

    Honors PA_WSGI_PATH as an explicit override for non-default PA
    layouts; otherwise derives from $USER.
    """
    override = os.environ.get("PA_WSGI_PATH", "").strip()
    if override:
        return override if os.path.exists(override) else ""
    user = os.environ.get("USER") or os.environ.get("LOGNAME") or ""
    if not user:
        return ""
    candidate = f"/var/www/{user}_pythonanywhere_com_wsgi.py"
    return candidate if os.path.exists(candidate) else ""


@app.route("/api/deploy", methods=["POST"])
def deploy():
    """Auto-deploy webhook. Pulls the latest commit and reloads the PA worker.

    Verifies an X-Deploy-Secret header against DEPLOY_SECRET. Fail-closed:
    returns 403 if the env var is unset, so a misconfigured deploy can't
    accidentally allow arbitrary callers to trigger code execution.

    Serialized via fcntl.flock so overlapping GitHub Actions runs or
    replayed valid requests can't race `git pull` in the same worktree.
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

        try:
            result = subprocess.run(
                ["git", "-C", _PROJECT_ROOT, "pull", "--ff-only"],
                capture_output=True,
                text=True,
                timeout=30,
            )
        except subprocess.TimeoutExpired:
            return "git pull timed out", 504

        if result.returncode != 0:
            # Don't echo raw stderr to the caller — it can leak local
            # paths or remote details. Log server-side, return generic.
            print(f"git pull failed (rc={result.returncode}):\n{result.stderr}")
            return "git pull failed (see server log for details)", 500

        # Re-register webhook in case WEBHOOK_URL or WEBHOOK_SECRET
        # changed, or a local polling session cleared the registration.
        # Best-effort: never fail the deploy on a webhook registration
        # error since the worker reload below will retry it.
        webhook_status = ""
        try:
            from bot.clients import register_webhook

            webhook_status = "\n" + register_webhook()
        except Exception as e:
            webhook_status = f"\nWebhook registration failed: {e}"

        # Touch the PA WSGI file so the next request boots a fresh
        # worker with the new code. No-op when not running on PA;
        # don't fail the deploy if the touch itself errors (the pull
        # already succeeded).
        wsgi_path = _pa_wsgi_path()
        if wsgi_path:
            try:
                os.utime(wsgi_path, None)
            except OSError as e:
                webhook_status += f"\nWSGI reload (os.utime) failed: {e}"

        return f"OK\n{result.stdout}{webhook_status}", 200
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
