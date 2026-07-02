"""Tests for the /api/deploy auto-deploy webhook.

Covers the security-critical path (secret verification, fail-closed),
the happy path with subprocess.run mocked, error handling, and the
file-lock concurrency guard.
"""

from unittest.mock import MagicMock, patch


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


def test_deploy_runs_git_pull_on_correct_secret():
    mock_request = MagicMock()
    mock_request.headers.get.return_value = "correct"
    fake_result = MagicMock(returncode=0, stdout="Already up to date.\n", stderr="")
    with (
        patch("bot.config.DEPLOY_SECRET", "correct"),
        patch("api.index.request", mock_request),
        patch("api.index.subprocess.run", return_value=fake_result) as mock_run,
        patch("api.index._pa_wsgi_path", return_value=""),
        patch("bot.clients.register_webhook", return_value="skipped"),
    ):
        from api.index import deploy

        body, status = deploy()
        assert status == 200
        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "git"
        assert "pull" in cmd
        assert "--ff-only" in cmd


def test_deploy_does_not_leak_stderr_on_git_failure():
    """Failed git pull must not echo raw stderr to the caller (which could
    leak local paths or remote details). Generic message externally,
    details only in server logs."""
    mock_request = MagicMock()
    mock_request.headers.get.return_value = "correct"
    fake_result = MagicMock(
        returncode=1,
        stdout="",
        stderr="fatal: not a git repository '/secret/path/on/server'\n",
    )
    with (
        patch("bot.config.DEPLOY_SECRET", "correct"),
        patch("api.index.request", mock_request),
        patch("api.index.subprocess.run", return_value=fake_result),
        patch("api.index._pa_wsgi_path", return_value=""),
    ):
        from api.index import deploy

        body, status = deploy()
        assert status == 500
        # The sensitive details must NOT appear in the response body.
        assert "/secret/path/on/server" not in body
        assert "not a git repository" not in body


def test_deploy_touches_wsgi_file_when_on_pa():
    mock_request = MagicMock()
    mock_request.headers.get.return_value = "correct"
    fake_result = MagicMock(returncode=0, stdout="", stderr="")
    with (
        patch("bot.config.DEPLOY_SECRET", "correct"),
        patch("api.index.request", mock_request),
        patch("api.index.subprocess.run", return_value=fake_result),
        patch(
            "api.index._pa_wsgi_path",
            return_value="/var/www/edisimon_pythonanywhere_com_wsgi.py",
        ),
        patch("api.index.os.utime") as mock_utime,
        patch("bot.clients.register_webhook", return_value="skipped"),
    ):
        from api.index import deploy

        body, status = deploy()
        assert status == 200
        mock_utime.assert_called_once_with(
            "/var/www/edisimon_pythonanywhere_com_wsgi.py", None
        )


def test_deploy_swallows_os_utime_failure():
    """A failure to touch the WSGI file must not break the deploy — the
    pull succeeded, that's the part that matters."""
    mock_request = MagicMock()
    mock_request.headers.get.return_value = "correct"
    fake_result = MagicMock(returncode=0, stdout="", stderr="")
    with (
        patch("bot.config.DEPLOY_SECRET", "correct"),
        patch("api.index.request", mock_request),
        patch("api.index.subprocess.run", return_value=fake_result),
        patch(
            "api.index._pa_wsgi_path",
            return_value="/var/www/x_pythonanywhere_com_wsgi.py",
        ),
        patch("api.index.os.utime", side_effect=PermissionError("read-only")),
        patch("bot.clients.register_webhook", return_value="skipped"),
    ):
        from api.index import deploy

        body, status = deploy()
        assert status == 200


def test_deploy_rejects_concurrent_runs():
    """A second deploy while one is in-flight returns 409 instead of
    racing git pull in the same worktree. Simulated by patching the
    lock helper to raise BlockingIOError on non-blocking acquire.

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
