# Telegram Bot — PythonAnywhere Starter Template

A minimal Python Telegram bot running on PythonAnywhere (free tier) with persistent conversation memory in SQLite and AI powered by Cerebras (defaults to `gpt-oss-120b` — strong reasoning at Cerebras speed; `qwen-3-235b-a22b-instruct-2507` is also available).

**Stack:** Python · Flask · pyTelegramBotAPI · OpenAI SDK · SQLite · PythonAnywhere

**All services used are free. No credit card required.**

Deployment smoke test: 2026-07-02.

> **Live demo:** <a href="https://t.me/tele_pythonanywhere_bot" target="_blank"><img src="https://img.shields.io/badge/Chat%20on-Telegram-2CA5E0?style=for-the-badge&logo=telegram&logoColor=white" alt="Chat on Telegram"/></a>

---

## What you will need

| Service | Purpose | Needed for | Free tier |
|---|---|---|---|
| [Telegram](https://telegram.org) | The bot platform | Everything | Always free |
| [Cerebras](https://cloud.cerebras.ai) | AI API — `gpt-oss-120b` (default), `qwen-3-235b-a22b-instruct-2507`, and more | Everything | 1M tokens/day, 30 req/min |
| [GitHub](https://github.com) | Source code | Everything | Always free |
| [PythonAnywhere](https://www.pythonanywhere.com) | Hosting the bot | Deployment | 1 web app, 512MB disk, monthly renewal click required |

> **Age requirements (check before signing up).** Each of the services above has a minimum age in its Terms of Service. As a rule of thumb: **Telegram, Cerebras, GitHub, PythonAnywhere, Hugging Face** are 13+ globally (16+ in the EU/UK for some, due to GDPR). If you're under 13, or in a region where the minimum is 16+, the safest path is to walk through the signup steps with a parent or teacher — they create the accounts and share the API keys with you. You can still do all of the coding, testing, and deployment work yourself.

---

# Part 1 — Run it on your laptop

You can have the bot replying to your messages on Telegram in about 10 minutes without touching PythonAnywhere or any deployment. Perfect for getting started and iterating on changes.

## Step 1 — Create a Telegram bot

1. Open Telegram and search for **@BotFather**
2. Send `/newbot`
3. Choose a name (e.g. `My AI Bot`) and a username ending in `bot` (e.g. `myai_bot`)
4. BotFather will reply with a **bot token** that looks like `7123456789:AAF...`
5. Save this token — you will need it in Step 4

---

## Step 2 — Get a Cerebras API key

1. Go to [cloud.cerebras.ai](https://cloud.cerebras.ai) and sign up (free, no credit card)
2. Verify your email and log in
3. Click your profile icon (top right) → **API Keys**
4. Click **Create new API key**, give it a name
5. Copy the key (looks like `csk-...`)
6. Save it — you will need it in Step 4

> **Using a different provider?** Any OpenAI-compatible API works. Set `AI_API_KEY` to your provider's key, `AI_BASE_URL` to their base URL, and `AI_MODEL` to the model name.

---

## Step 3 — Fork and clone the repo

1. Create a [GitHub account](https://github.com) if you don't have one
2. Go to the template repo and click **Fork** (top right) to copy it to your account
3. Clone your fork to your computer:

```bash
git clone https://github.com/<your-username>/telegram-pythonanywhere-bot.git
cd telegram-pythonanywhere-bot
```

---

## Step 4 — Install dependencies and configure `.env`

Create the virtualenv and install Python dependencies:

```bash
make install
```

Then copy the template and fill in the values you saved in Steps 1 and 2:

```bash
cp .env.example .env
```

Open `.env` in your editor and set these two lines:

```
TELEGRAM_BOT_TOKEN=<paste your BotFather token here>
AI_API_KEY=<paste your Cerebras API key here>
```

Leave everything else as-is for now. SQLite memory is optional — without it the bot runs in **stateless mode** (no conversation memory, no rate limit), which is fine for initial testing.

---

## Step 5 — Run the bot locally

```bash
make run
```

You should see something like:

```
Storage not configured — running in stateless mode (no memory, no rate limit).
Bot @your_bot_username starting in polling mode.
Send your bot a message on Telegram to try it out.
Press Ctrl+C to stop.
```

Open Telegram, find your bot, and send it a message. You'll see each exchange logged in your terminal:

```
[14:32:15] @alice → @your_bot: hello, who are you?
[14:32:17] @your_bot → @alice: Hi! I'm an AI assistant powered by Cerebras.
```

This is the same bot code you'll eventually deploy to PythonAnywhere — the only difference is how Telegram delivers messages. Locally we poll; in production Telegram pushes to a webhook. Edit any file in `bot/`, `Ctrl+C` the bot, rerun `make run`, and you'll see your changes immediately.

---

# Part 2 — Deploy it to PythonAnywhere

Once the bot works locally, the next step is to put it on PythonAnywhere so it keeps running when your laptop is closed. PythonAnywhere (PA) runs the same Flask app via a long-lived WSGI worker. The free tier supports everything this template needs.

> **PA free-tier note.** PA restricts outbound HTTPS on the free plan to a whitelist of domains. The services this template uses (Telegram, Cerebras, Hugging Face) are all whitelisted, so no extra setup is needed. Persistent state lives in SQLite on PA's disk — no external Redis or database is required.

## Fast path — one-command deploy *(optional, skip Steps 7–12)*

If you just want to ship and don't care to learn what each step does, you can do the whole PA setup from your laptop with a single command. **You still have to do Step 6 (sign up + email verify) in the browser — PA has no API for account creation.**

After signing up:

1. Grab a PA API token: <https://www.pythonanywhere.com/account/#api_token> → **Create new API token** → copy the value
2. Add two lines to your local `.env`:

```
PA_USERNAME=<your PA username>
PA_API_TOKEN=<the token you just copied>
```

3. Run:

```bash
make deploy-pa
```

The script (`scripts/pa_deploy.sh`) creates the PA web app, opens a bash console, clones your repo, creates the virtualenv, installs deps, uploads a PA-flavoured `.env` (with `SQLITE_PATH` + `WEBHOOK_URL` filled in), uploads the WSGI shim, configures the web app, and reloads. It pauses once and asks you to **open one URL in your browser** — PA requires that each new bash console be visited once before its API will accept commands. After that it's hands-off.

The script is idempotent — re-running heals partial state (e.g. if you closed the terminal while pip was still installing), or pushes an updated `.env`. For ongoing code updates, Step 14 below (GitHub Actions auto-deploy on push) is still the smoothest path; this script is most useful for the first deploy and for recovery.

If you'd rather understand what the script is doing, do Steps 7–12 manually instead — they're the same work, step by step.

## Step 6 — Create a PythonAnywhere account

1. Sign up at [pythonanywhere.com](https://www.pythonanywhere.com) (free Beginner tier — no card)
2. Verify your email and log in
3. Your bot will be hosted at `https://<your-pa-username>.pythonanywhere.com`

---

## Step 7 — Clone the repo on PA

Open a Bash console from the PA dashboard (Dashboard → **New console** → **Bash**) and run:

```bash
git clone https://github.com/<your-github-username>/telegram-pythonanywhere-bot.git
```

---

## Step 8 — Create a virtualenv and install dependencies

Still in the PA Bash console:

```bash
python3.13 -m venv ~/.virtualenvs/telegram-bot
~/.virtualenvs/telegram-bot/bin/pip install -r ~/telegram-pythonanywhere-bot/requirements.txt
```

This takes ~1–2 minutes. The virtualenv path `/home/<your-pa-username>/.virtualenvs/telegram-bot` is what you'll point the web app at in Step 10.

---

## Step 9 — Upload your `.env` to PA

The PA WSGI shim (`pythonanywhere_wsgi.py` in this repo) reads `.env` from the project root, the same way `make run` does locally:

```bash
cd ~/telegram-pythonanywhere-bot
nano .env
```

Paste in:

```
TELEGRAM_BOT_TOKEN=<your BotFather token>
AI_API_KEY=<your Cerebras API key>
AI_BASE_URL=https://api.cerebras.ai/v1
AI_MODEL=gpt-oss-120b
SQLITE_PATH=/home/<your-pa-username>/bot.db
WEBHOOK_URL=https://<your-pa-username>.pythonanywhere.com/api/webhook
```

`SQLITE_PATH` enables persistent memory + rate limit + dedupe. The file is created on first use; nothing to set up. If you skip it, the bot runs in stateless mode (no memory between messages).

`WEBHOOK_URL` enables auto-registration: every time the PA worker boots, the bot calls Telegram's `setWebhook` against this URL. No manual `curl setWebhook` needed in production (Step 12 below becomes optional).

Save with `Ctrl+O`, `Enter`, then exit with `Ctrl+X`. `.env` is in `.gitignore`, so it never gets committed even though you edited it inside a checked-out repo.

---

## Step 10 — Create the PA web app

1. In the PA dashboard, go to the **Web** tab → **Add a new web app**
2. Click **Next** to accept the default domain (`<your-pa-username>.pythonanywhere.com`)
3. Choose **Manual configuration** (not the Flask wizard — that scaffolds a different layout)
4. Pick **Python 3.13** to match the virtualenv
5. After the app is created, scroll down on the Web tab and configure:
   - **Source code:** `/home/<your-pa-username>/telegram-pythonanywhere-bot`
   - **Working directory:** `/home/<your-pa-username>/telegram-pythonanywhere-bot`
   - **Virtualenv:** `/home/<your-pa-username>/.virtualenvs/telegram-bot`

---

## Step 11 — Wire up the WSGI file

Still in the Web tab, click **WSGI configuration file** (the link looks like `/var/www/<your-pa-username>_pythonanywhere_com_wsgi.py`). Delete everything in the editor and replace it with:

```python
import sys

project_home = "/home/<your-pa-username>/telegram-pythonanywhere-bot"
if project_home not in sys.path:
    sys.path.insert(0, project_home)

from pythonanywhere_wsgi import application  # noqa: F401
```

Substitute your actual PA username on the `project_home` line. Save the file, then go back to the Web tab and click the green **Reload** button.

Test that the worker booted by visiting `https://<your-pa-username>.pythonanywhere.com/api/health` in a browser — it should return `OK` followed by the short commit ID the bot is running (e.g. `OK 4ea0ce2`). That commit ID is how you can always tell exactly which version of your code is live.

---

## Step 12 — Send your bot its first message

If you set `WEBHOOK_URL` in Step 9, the bot auto-registers the webhook the first time the PA worker boots. Visit `https://<your-pa-username>.pythonanywhere.com/api/health` in a browser to force the worker to start, then open Telegram, find your bot, and send a message. Replies will come from PythonAnywhere.

If you'd prefer to register the webhook manually (or skipped `WEBHOOK_URL`), run this from PA's Bash console (it reads `.webhook_secret`, which is auto-generated on first worker boot):

```bash
cd ~/telegram-pythonanywhere-bot
curl -X POST "https://api.telegram.org/bot<TELEGRAM_BOT_TOKEN>/setWebhook" \
  --data-urlencode "url=https://<your-pa-username>.pythonanywhere.com/api/webhook" \
  --data-urlencode "secret_token=$(cat .webhook_secret)" \
  --data-urlencode "max_connections=1"
```

The `secret_token` matches the bot's auto-generated `WEBHOOK_SECRET` — without it, every update gets rejected with 403. You should see `{"ok":true,...}` in the response.

---

## Step 13 — Keep the bot alive (monthly renewal)

PA free-tier web apps must be renewed every month by clicking a button in the dashboard — otherwise they auto-disable. PA emails you a week before the expiry date. To renew manually:

1. Go to the **Web** tab
2. Find the "Run until N days from today" button near the top
3. Click it — your bot gets another month

If you ever need to update the bot by hand after pushing new code to GitHub, run this in a PA Bash console:

```bash
cd ~/telegram-pythonanywhere-bot && git fetch origin && git reset --hard origin/main && touch /var/www/<your-pa-username>_pythonanywhere_com_wsgi.py
```

This is the same convergence the auto-deploy endpoint performs: it puts the checkout exactly at your latest pushed commit (discarding any local edits to tracked files — a plain `git pull` would refuse and get stuck instead), and the `touch` forces PA to reload the worker without needing to click Reload in the dashboard.

---

## Step 14 — Auto-deploy on every push *(optional but recommended)*

The bot ships with a `/api/deploy` endpoint and a GitHub Actions workflow that work together to redeploy the bot every time you push to `main` — no more manual `git pull`.

1. Generate a random secret:

```bash
openssl rand -hex 32
```

2. Add it to your PA `.env`:

```
DEPLOY_SECRET=<the secret you just generated>
```

3. Reload your PA web app (Web tab → green **Reload** button) so the new env var is picked up.

4. On GitHub, go to your fork → **Settings** → **Secrets and variables** → **Actions** → **New repository secret**, and add two secrets:

| Name | Value |
|---|---|
| `DEPLOY_SECRET` | the same value you put in PA's `.env` |
| `PA_DEPLOY_URL` | `https://<your-pa-username>.pythonanywhere.com/api/deploy` |

5. Push any change to `main`. The `Deploy to PythonAnywhere` GitHub Action triggers automatically, hits `/api/deploy` with the secret header, and PA syncs the checkout to your pushed commit and reloads the worker. The workflow then polls `/api/health` until it reports your new commit's ID — so a green run means *your code is actually live*, not just "the server answered". End-to-end takes ~30 seconds.

You can also trigger a deploy manually from GitHub: **Actions** tab → **Deploy to PythonAnywhere** → **Run workflow**.

If the secrets aren't set, the workflow skips with a warning instead of failing — so this is fully optional, the rest of the repo keeps working without it.

Two things worth knowing about how deploys work:

- **GitHub is the source of truth.** Each deploy runs `git fetch` + `git reset --hard` on PA, so the server always ends up exactly at the pushed commit — even after force-pushes, rebases, or if a file on PA was edited by hand. The flip side: don't edit the bot's *code* files directly on PA (via the Files tab or a console) and expect the edits to survive — the next deploy overwrites them. Your `.env`, `.webhook_secret`, and database are untracked files and always survive deploys.
- **If you change `requirements.txt`,** the deploy installs the new dependencies into the virtualenv automatically before reloading.

---

# Part 3 — Customize it

## Secure the webhook

**Already automated.** On first boot, the bot generates a 64-hex-character random secret, stores it in `.webhook_secret` (gitignored, mode `0600`), and registers it with Telegram via `setWebhook` so every incoming request must present a matching `X-Telegram-Bot-Api-Secret-Token` header. Forged updates are rejected with 403.

You don't need to do anything for this to work. The first PA worker boot prints:

```
Generated webhook secret at /home/<your-pa-username>/telegram-pythonanywhere-bot/.webhook_secret (auto-bootstrap)
Webhook registered: https://<your-pa-username>.pythonanywhere.com/api/webhook
```

The secret persists across deploys (file lives on PA's disk, outside the git worktree's tracked files), so the value the bot verifies against stays stable.

**To override (optional):** set `WEBHOOK_SECRET=<your value>` explicitly in `.env`. The env var wins over the auto-bootstrapped file. Useful if you want to share a known secret across environments.

**To rotate the secret:** in PA's Bash console, `rm ~/telegram-pythonanywhere-bot/.webhook_secret` and reload the web app. Boot generates a new one and re-registers with Telegram automatically.

---

## Add a second AI provider *(optional)*

If you set `HF_SPACE_ID` in your `.env`, the bot registers a `/model` command that lets users switch between the default provider (`main`) and a Hugging Face Gradio Space (`hf`). Useful for demoing multiple models in the same bot.

```
HF_SPACE_ID=username/space-name
HF_TOKEN=your_hf_token_here   # only for private/gated Spaces
```

Users can now run `/model main` or `/model hf` to switch per-user.

---

## Lock down to specific users *(optional)*

By default the bot replies to anyone on Telegram. To restrict it to a private allow-list, set `ALLOWED_USERS` in `.env` to a comma-separated list of usernames (with or without `@`) or numeric user IDs:

```
ALLOWED_USERS=@alice,bob,123456789
```

When the variable is set, everyone outside the list gets **silence** — no rejection message, no `/start` response, nothing. This is deliberate: any reply would confirm to a scanner that the bot exists. Whitelisted users see normal behavior.

To find your numeric user ID, message [@userinfobot](https://t.me/userinfobot) on Telegram — it replies with your ID. Useful when you have no public username, or want to whitelist by an identifier that can't change later.

Reload (or push) for the change to take effect: the list is read at worker boot.

---

## Customization reference

| What to change | How |
|---|---|
| Bot personality / instructions | Edit `SYSTEM_PROMPT` in `bot/config.py` |
| AI model | Set `AI_MODEL` env var (free-tier tested: `gpt-oss-120b` (default), `qwen-3-235b-a22b-instruct-2507`) |
| AI provider | Set `AI_BASE_URL` env var (any OpenAI-compatible endpoint) |
| Secure the webhook | Auto-generated on first boot — see "Secure the webhook" above |
| Restrict who can use the bot | Set `ALLOWED_USERS` env var |
| Daily message limit | Set `RATE_LIMIT` env var (default `250`) |
| Add a second provider | Set `HF_SPACE_ID` (and optionally `HF_TOKEN`) — enables `/model` command |
| Conversation memory length | Edit `MAX_HISTORY` in `bot/config.py` |
| Hosting label shown by `/about` | Set `HOSTING_LABEL` env var |
| Add a new command | Add a handler in `bot/handlers.py` |

---

# Reference

## Project structure

```
telegram-pythonanywhere-bot/
├── api/
│   └── index.py          # Entry point — Flask app, webhook route, /api/health, secret verification
├── bot/
│   ├── config.py         # All env vars and constants
│   ├── clients.py        # bot, ai, store instances (store is optional)
│   ├── store.py          # SqliteStore — KV with TTL, backed by sqlite3
│   ├── ai.py             # ask_ai orchestration — history, AI dispatch
│   ├── providers.py      # Provider dispatch: OpenAI-compatible (with retry) or HF Gradio space
│   ├── preferences.py    # Per-user provider preference (via store)
│   ├── history.py        # Conversation memory (via store, graceful degradation)
│   ├── rate_limit.py     # Per-user rate limiting (via store, graceful degradation)
│   ├── dedupe.py         # Drops repeated update_ids when Telegram retries
│   ├── helpers.py        # Utilities (send_reply, keep_typing, should_respond)
│   └── handlers.py       # Telegram commands — add new commands here
├── tests/
│   ├── conftest.py       # Mocks for running tests without real API keys
│   ├── test_ai.py
│   ├── test_providers.py
│   ├── test_preferences.py
│   ├── test_handlers.py
│   ├── test_helpers.py
│   ├── test_history.py
│   ├── test_rate_limit.py
│   ├── test_dedupe.py
│   ├── test_store.py
│   └── test_webhook.py
├── .github/
│   └── workflows/
│       ├── ci.yml        # Runs tests on every push and pull request
│       └── deploy.yml    # Triggers PA auto-deploy via /api/deploy on push to main
├── .env.example          # Copy to .env for local dev (never commit .env)
├── .gitignore
├── Makefile              # install / run / test shortcuts
├── run_local.py          # Local polling entry point (used by `make run`)
├── pythonanywhere_wsgi.py # WSGI entry point for PythonAnywhere
├── requirements.txt
├── CLAUDE.md             # Agent-readable project guide
└── README.md
```

---

## Make commands

```bash
make install    # set up virtual environment and install dependencies
make run        # run the bot locally via polling (no PA needed, reads .env)
make test       # run all tests
make deploy-pa  # one-command PythonAnywhere deploy (see "Fast path" in Part 2)
```

---

## Bot commands

| Command | Description |
|---|---|
| `/start` | Welcome message |
| `/help` | List all commands |
| `/reset` | Clear your conversation history |
| `/about` | Show model, storage, and hosting info |
| `/sha` | Show the live git commit SHA |
| `/model` | Switch AI provider (only available when `HF_SPACE_ID` is set) |

---

## Running tests

```bash
make test
```

Tests run offline against mocked Telegram and OpenAI clients — no real API keys or network access required. The same suite runs automatically via GitHub Actions on every push and pull request.
