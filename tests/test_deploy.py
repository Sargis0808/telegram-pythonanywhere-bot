"""Tests for the /api/deploy auto-deploy webhook.

Covers the security-critical path (secret verification, fail-closed),
the fetch + reset --hard convergence flow with subprocess.run mocked,
dependency sync, reload reporting, error handling, and the file-lock
concurrency guard.

Why reset --hard and not pull --ff-only: a pull wedges permanently once
the server-side worktree diverges (hand-edited file, force-push) and
every deploy after that 500s while the bot keeps running old code —
reproduced live on PA on 2026-07-02. Reset converges to origin no
matter what, so these tests treat "pull" as a regression.
"""

import sys
from unittest.mock import MagicMock, patch


def _fake_git(
    old_sha="aaaaaaa",
    new_sha="bbbbbbb",
    branch="main",
    fetch_rc=0,
    reset_rc=0,
    diff_out="",
    pip_rc=0,
):
    """subprocess.run replacement that routes on the git subcommand.

    Records every argv in `.calls` for assertions. Non-git argv (the
    pip invocation) gets `pip_rc`.
    """
    state = {"rev_calls": 0}

    def run(cmd, **kwargs):
        run.calls.append(cmd)
        if cmd[0] != "git":
            return MagicMock(returncode=pip_rc, stdout="", stderr="pip exploded")
        sub = cmd[3]
        if sub == "rev-parse" and "--short=7" in cmd:
            sha = old_sha if state["rev_calls"] == 0 else new_sha
            state["rev_calls"] += 1
            return MagicMock(returncode=0, stdout=sha + "\n", stderr="")
        if sub == "rev-parse":  # --abbrev-ref HEAD
            return MagicMock(returncode=0, stdout=branch + "\n", stderr="")
        if sub == "fetch":
            return MagicMock(
                returncode=fetch_rc,
                stdout="",
                stderr="fatal: unable to access '/secret/path/on/server'",
            )
        if sub == "reset":
            return MagicMock(
                returncode=reset_rc,
                stdout=f"HEAD is now at {new_sha}\n",
                stderr="fatal: reset broke in '/secret/path/on/server'",
            )
        if sub == "diff":
            return MagicMock(returncode=0, stdout=diff_out, stderr="")
        return MagicMock(returncode=0, stdout="", stderr="")

    run.calls = []
    return run


def _deploy(fake_run, wsgi_path=""):
    """Run deploy() with the standard mock stack; returns (body, status)."""
    mock_request = MagicMock()
    mock_request.headers.get.return_value = "correct"
    with (
        patch("bot.config.DEPLOY_SECRET", "correct"),
        patch("api.index.request", mock_request),
        patch("api.index.subprocess.run", side_effect=fake_run),
        patch("api.index._pa_wsgi_path", return_value=wsgi_path),
        patch("api.index.os.utime"),
        patch("bot.clients.register_webhook", return_value="webhook ok"),
    ):
        from api.index import deploy

        return deploy()


def test_deploy_fails_closed_when_secret_unset():
    """If DEPLOY_SECRET is empty, /api/deploy MUST refuse all requests.
    This is the safety property — a misconfigured deploy can't accidentally
    expose code execution to anonymous callers."""
    mock_request = MagicMock()
    mock_request.headers.get.return_value = "anything"
    with (
        patch("bot.config.DEPLOY_SECRET", ""),
        patch("api.index.request", mock_request),
    ):
        from api.index import deploy

        body, status = deploy()
        assert status == 403


def test_deploy_rejects_bad_secret():
    mock_request = MagicMock()
    mock_request.headers.get.return_value = "wrong"
    with (
        patch("bot.config.DEPLOY_SECRET", "correct"),
        patch("api.index.request", mock_request),
    ):
        from api.index import deploy

        body, status = deploy()
        assert status == 403


def test_deploy_converges_to_origin_with_fetch_and_reset():
    """Happy path: fetch origin, hard-reset to origin/<branch>, report
    old -> new SHA. No `git pull` anywhere — pull is the command that
    wedges on a diverged worktree."""
    fake = _fake_git()
    body, status = _deploy(fake)
    assert status == 200
    git_subs = [c[3] for c in fake.calls if c[0] == "git"]
    assert "fetch" in git_subs
    assert "pull" not in git_subs
    reset_cmd = next(c for c in fake.calls if c[0] == "git" and c[3] == "reset")
    assert reset_cmd[3:] == ["reset", "--hard", "origin/main"]
    assert "aaaaaaa -> bbbbbbb" in body


def test_deploy_resets_to_main_when_head_is_detached():
    """A detached HEAD (e.g. after a manual checkout on the server) must
    not break deploys — fall back to origin/main."""
    fake = _fake_git(branch="HEAD")
    body, status = _deploy(fake)
    assert status == 200
    reset_cmd = next(c for c in fake.calls if c[0] == "git" and c[3] == "reset")
    assert reset_cmd[3:] == ["reset", "--hard", "origin/main"]


def test_deploy_does_not_leak_stderr_on_git_failure():
    """Failed git commands must not echo raw stderr to the caller (which
    could leak local paths or remote details). Generic message externally,
    details only in server logs."""
    for kwargs in ({"fetch_rc": 1}, {"reset_rc": 1}):
        fake = _fake_git(**kwargs)
        body, status = _deploy(fake)
        assert status == 500
        assert "/secret/path/on/server" not in body


def test_deploy_installs_requirements_when_changed():
    fake = _fake_git(diff_out="requirements.txt\nbot/ai.py\n")
    mock_request = MagicMock()
    mock_request.headers.get.return_value = "correct"
    with (
        patch("bot.config.DEPLOY_SECRET", "correct"),
        patch("api.index.request", mock_request),
        patch("api.index.subprocess.run", side_effect=fake),
        patch("api.index._pa_wsgi_path", return_value=""),
        patch("api.index.os.path.exists", return_value=True),
        patch("bot.clients.register_webhook", return_value="webhook ok"),
    ):
        from api.index import deploy

        body, status = deploy()
    assert status == 200
    pip_calls = [c for c in fake.calls if c[0] != "git"]
    assert len(pip_calls) == 1
    assert pip_calls[0][1:3] == ["install", "-r"]
    assert "Dependencies: installed" in body


def test_deploy_skips_pip_when_requirements_unchanged():
    fake = _fake_git(diff_out="bot/ai.py\n")
    body, status = _deploy(fake)
    assert status == 200
    assert all(c[0] == "git" for c in fake.calls)
    assert "Dependencies: unchanged" in body


def test_deploy_fails_and_skips_reload_when_pip_fails():
    """If pip can't install the new requirements, deploy must 500 and NOT
    touch the WSGI file — reloading would boot new code onto old deps and
    crash the worker. Old worker keeps serving instead."""
    fake = _fake_git(diff_out="requirements.txt\n", pip_rc=1)
    mock_request = MagicMock()
    mock_request.headers.get.return_value = "correct"
    with (
        patch("bot.config.DEPLOY_SECRET", "correct"),
        patch("api.index.request", mock_request),
        patch("api.index.subprocess.run", side_effect=fake),
        patch("api.index._pa_wsgi_path", return_value="/var/www/x_wsgi.py"),
        patch("api.index.os.path.exists", return_value=True),
        patch("api.index.os.utime") as mock_utime,
        patch("bot.clients.register_webhook", return_value="webhook ok"),
    ):
        from api.index import deploy

        body, status = deploy()
    assert status == 500
    assert "pip exploded" not in body  # stderr stays in server logs
    mock_utime.assert_not_called()


def test_deploy_touches_wsgi_file_and_reports_it():
    mock_request = MagicMock()
    mock_request.headers.get.return_value = "correct"
    fake = _fake_git()
    with (
        patch("bot.config.DEPLOY_SECRET", "correct"),
        patch("api.index.request", mock_request),
        patch("api.index.subprocess.run", side_effect=fake),
        patch(
            "api.index._pa_wsgi_path",
            return_value="/var/www/edisimon_pythonanywhere_com_wsgi.py",
        ),
        patch("api.index.os.utime") as mock_utime,
        patch("bot.clients.register_webhook", return_value="webhook ok"),
    ):
        from api.index import deploy

        body, status = deploy()
        assert status == 200
        mock_utime.assert_called_once_with(
            "/var/www/edisimon_pythonanywhere_com_wsgi.py", None
        )
        assert "Reload: touched" in body


def test_deploy_warns_loudly_when_wsgi_file_not_found():
    """The old behavior silently skipped the reload — a 'green' deploy
    that left the old worker running. Now the response must say the
    worker was NOT restarted so the GitHub Actions log shows it."""
    fake = _fake_git()
    body, status = _deploy(fake, wsgi_path="")
    assert status == 200
    assert "NOT restarted" in body
    assert "PA_WSGI_PATH" in body


def test_deploy_reports_utime_failure_without_breaking_deploy():
    """A failure to touch the WSGI file must not break the deploy (the
    fetch/reset succeeded) but must be called out in the response."""
    mock_request = MagicMock()
    mock_request.headers.get.return_value = "correct"
    fake = _fake_git()
    with (
        patch("bot.config.DEPLOY_SECRET", "correct"),
        patch("api.index.request", mock_request),
        patch("api.index.subprocess.run", side_effect=fake),
        patch(
            "api.index._pa_wsgi_path",
            return_value="/var/www/x_pythonanywhere_com_wsgi.py",
        ),
        patch("api.index.os.utime", side_effect=PermissionError("read-only")),
        patch("bot.clients.register_webhook", return_value="webhook ok"),
    ):
        from api.index import deploy

        body, status = deploy()
        assert status == 200
        assert "NOT restarted" in body


def test_deploy_rejects_concurrent_runs():
    """A second deploy while one is in-flight returns 409 instead of
    racing git in the same worktree. Simulated by patching the lock
    helper to raise BlockingIOError on non-blocking acquire.

    Patches api.index._lock_deploy_nb rather than fcntl.flock directly so
    the test is cross-platform: fcntl doesn't exist on Windows, where the
    helper is a no-op."""
    mock_request = MagicMock()
    mock_request.headers.get.return_value = "correct"
    with (
        patch("bot.config.DEPLOY_SECRET", "correct"),
        patch("api.index.request", mock_request),
        patch("api.index._lock_deploy_nb", side_effect=BlockingIOError()),
    ):
        from api.index import deploy

        body, status = deploy()
        assert status == 409


def test_deploy_uses_compare_digest():
    """Constant-time comparison guards against timing-attack secret recovery."""
    import inspect

    from api import index

    src = inspect.getsource(index.deploy)
    assert "hmac.compare_digest" in src


# ── _pa_wsgi_path resolution fallbacks ──────────────────────────────────────
#
# If this returns "" on PA, deploys go "green" while the old worker keeps
# running — the exact bug class this suite exists to prevent. Each layer
# of the fallback chain gets a test.


def test_wsgi_path_explicit_override_wins():
    with (
        patch.dict("os.environ", {"PA_WSGI_PATH": "/custom/wsgi.py"}),
        patch("api.index.os.path.exists", side_effect=lambda p: p == "/custom/wsgi.py"),
    ):
        from api.index import _pa_wsgi_path

        assert _pa_wsgi_path() == "/custom/wsgi.py"


def test_wsgi_path_from_user_env_var():
    with (
        patch.dict("os.environ", {"USER": "alice", "PA_WSGI_PATH": ""}),
        patch(
            "api.index.os.path.exists",
            side_effect=lambda p: p == "/var/www/alice_pythonanywhere_com_wsgi.py",
        ),
    ):
        from api.index import _pa_wsgi_path

        assert _pa_wsgi_path() == "/var/www/alice_pythonanywhere_com_wsgi.py"


def test_wsgi_path_falls_back_to_pwd_when_env_empty():
    """uwsgi worker environments are minimal — USER/LOGNAME may be absent.
    The passwd database still knows who we are."""
    fake_pwd = MagicMock()
    fake_pwd.getpwuid.return_value = MagicMock(pw_name="bob")
    with (
        patch.dict("os.environ", {}, clear=True),
        patch.dict(sys.modules, {"pwd": fake_pwd}),
        patch("api.index.os.getuid", return_value=1000, create=True),
        patch(
            "api.index.os.path.exists",
            side_effect=lambda p: p == "/var/www/bob_pythonanywhere_com_wsgi.py",
        ),
    ):
        from api.index import _pa_wsgi_path

        assert _pa_wsgi_path() == "/var/www/bob_pythonanywhere_com_wsgi.py"


def test_wsgi_path_derived_from_home_checkout_path():
    """PA checkouts live at /home/<user>/... — the path itself names the
    account when everything else fails."""
    with (
        patch.dict("os.environ", {}, clear=True),
        patch.dict(
            sys.modules, {"pwd": MagicMock(getpwuid=MagicMock(side_effect=KeyError))}
        ),
        patch("api.index.os.getuid", return_value=1000, create=True),
        patch("api.index._PROJECT_ROOT", "/home/carol/telegram-pythonanywhere-bot"),
        patch(
            "api.index.os.path.exists",
            side_effect=lambda p: p == "/var/www/carol_pythonanywhere_com_wsgi.py",
        ),
    ):
        from api.index import _pa_wsgi_path

        assert _pa_wsgi_path() == "/var/www/carol_pythonanywhere_com_wsgi.py"


def test_wsgi_path_glob_used_only_when_unambiguous():
    from api.index import _pa_wsgi_path

    common = {
        "patch_env": patch.dict("os.environ", {}, clear=True),
    }
    with (
        common["patch_env"],
        patch.dict(
            sys.modules, {"pwd": MagicMock(getpwuid=MagicMock(side_effect=KeyError))}
        ),
        patch("api.index.os.getuid", return_value=1000, create=True),
        patch("api.index._PROJECT_ROOT", "/srv/checkout"),
        patch("api.index.os.path.exists", return_value=False),
        patch(
            "api.index.glob.glob",
            return_value=["/var/www/dave_pythonanywhere_com_wsgi.py"],
        ),
    ):
        assert _pa_wsgi_path() == "/var/www/dave_pythonanywhere_com_wsgi.py"

    with (
        patch.dict("os.environ", {}, clear=True),
        patch.dict(
            sys.modules, {"pwd": MagicMock(getpwuid=MagicMock(side_effect=KeyError))}
        ),
        patch("api.index.os.getuid", return_value=1000, create=True),
        patch("api.index._PROJECT_ROOT", "/srv/checkout"),
        patch("api.index.os.path.exists", return_value=False),
        patch(
            "api.index.glob.glob",
            return_value=[
                "/var/www/one_pythonanywhere_com_wsgi.py",
                "/var/www/two_pythonanywhere_com_wsgi.py",
            ],
        ),
    ):
        # Two candidates — touching the wrong one would reload the wrong
        # app, so refuse to guess.
        assert _pa_wsgi_path() == ""
