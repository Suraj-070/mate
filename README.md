# AI Groupmate

An AI Discord bot for a private friend group. Converses naturally in mixed English/Nepali, has a stable slightly-witty personality, remembers approved context, and executes practical tasks when asked.

> **Status:** Phase 4 — conversation core + memory + style + tools + custom APIs + topic tracking. See [Architecture & Roadmap](#architecture--roadmap) below.

---

## Quickstart

### Prerequisites

- Python 3.11+
- A Discord bot application with a token (and the **Message Content** privileged intent enabled)
- A Z.ai API key for GLM-4.6

### 1. Clone and install

```bash
git clone <your-repo-url> ai-groupmate
cd ai-groupmate

python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
```

### 2. Configure environment

```bash
cp .env.example .env
```

Edit `.env` and fill in:
- `DISCORD_BOT_TOKEN` — from Discord Developer Portal → your app → Bot → Token
- `DISCORD_APP_ID` — from Discord Developer Portal → your app → General Information → Application ID
- `DISCORD_ADMIN_IDS` — your Discord user ID (right-click your username → Copy User ID)
- `DISCORD_TEST_GUILD_ID` — your test server ID (right-click server name → Copy Server ID)
- `ZAI_API_KEY` — from https://z.ai/ → API Keys

### 3. Enable Discord intents

In the Discord Developer Portal → your app → Bot → **Privileged Gateway Intents**, enable:
- ✅ **MESSAGE CONTENT INTENT** (required — bot cannot read messages without this)
- ✅ **SERVER MEMBERS INTENT** (optional but recommended)
- ✅ **PRESENCE INTENT** (not required)

### 4. Invite the bot to your test server

Generate an OAuth URL in the Developer Portal → OAuth2 → URL Generator:
- Scopes: `bot`, `applications.commands`
- Permissions: `Send Messages`, `Read Message History`, `Add Reactions` (Phase 2)

Open the URL and authorize the bot into your test server.

### 5. Run the database migrations

```bash
alembic upgrade head
```

This creates `./data/groupmate.db` with both Phase 1 (channels, users, messages) and Phase 2 (summaries, memories, style_samples, style_profiles) tables.

### 6. Start the bot

```bash
python -m app.main
```

You should see:
```
✅ Bot logged in as YourBot#1234 (ID: 123456789012345678)
   Connected to 1 guild(s)
   Press Ctrl+C to shut down.
```

Slash commands sync automatically to your test guild on startup.

---

## Phase 2 — Using Memory, Style, and Slash Commands

### Memory (admin-approved only)

Memories are NEVER auto-extracted from chat. Only admins can create them:

- **Slash command:** `/remember kind:<fact|preference|lore|event|joke> text:<what to remember> [expires_in_days:N]`
- **Context menu:** right-click any message → **Apps** → **"Remember this"**

List and forget:
- `/memories list scope:<this channel|all>`
- `/forget <memory_id>`

Memories are:
- Source-tagged (the admin who approved them + the source Discord message ID)
- Confidence-scored (jokes = 0.3, facts = 1.0, by default)
- Expirable by kind (events expire in 30 days, jokes in 180 days, facts/lore never)
- Filtered for PII patterns (phone/email/long tokens) at creation time

### Style Learning (admin-approved only)

The bot never learns style from arbitrary chat. Only from admin-approved samples:

- **Context menu:** right-click any message → **Apps** → **"Use for Style"**

Each approval is checked for privacy:
- The author must NOT have opted out of style learning
- Bot messages cannot be style samples
- Empty messages cannot be style samples

The style profile auto-rebuilds when N new samples accumulate (default 5). You can force a rebuild with:

- `/style rebuild`

Check current status with:

- `/style status`

The bot only ever sees the **distilled structured profile** (formality, humor level, etc.) — never raw message content from individual users. The LLM is given paraphrased style examples, not direct quotes.

### Conversation Summaries

Automatic. Every ~30 messages per channel, the older portion is summarized by the LLM and stored. The most recent summary per channel is injected into the bot's context as "RECENT CONVERSATION SUMMARY".

- Summaries are paraphrased — no direct quotes
- PII patterns are detected and warned about
- Old summaries are archived (kept in DB for audit), not deleted

### Privacy Controls (any user)

- `/privacy flag:<style|memory|reply> state:<on|off>`

Effects:
- `style off` — your messages cannot be approved as style samples
- `memory off` — your messages cannot be proposed as memories (when Phase 3 lands extraction approval UI)
- `reply off` — the bot never replies to your messages (still logs them for audit)

### REACT Outcome

When you mention the bot with a short acknowledgement (e.g. `@Bot ok` or `@Bot thanks`), the bot emoji-reacts instead of sending a contentless "you're welcome!" reply. Toggle with `ENABLE_REACT_OUTCOME` in `.env`.

### LLM-Assisted Decision (gray zone, off by default)

When `ENABLE_LLM_DECISION_ASSIST=true` AND `ENABLE_AUTONOMOUS_PARTICIPATION=true`, the bot will ask the LLM "should I chime in?" for ambiguous messages in active conversations where it wasn't directly addressed. Defaults to IGNORE if the LLM is unsure. Adds ~1s latency and one extra LLM call per gray-zone message.

---

## Phase 3 — Using Tools (Reminders + Timers)

Tools are available to the LLM via the tool-calling framework. The LLM decides what action to take; the backend validates, permission-checks, and executes. The bot **never** claims success until the backend confirms.

### Creating Reminders via Conversation

Just ask the bot naturally:

```
@Bot remind me tomorrow at 8pm to call home
@Bot remind me every weekday at 9am to check the standup notes
@Bot set a reminder for monday 9am: weekly sync
```

The LLM will call the `create_reminder` tool with the natural-language time + recurrence. The backend parses the time using `dateparser` in the configured timezone (`USER_TIMEZONE`, default `Australia/Sydney`), persists the reminder in the DB, and registers it with APScheduler.

### Creating Timers via Conversation

For short durations (minutes/hours, max 24h):

```
@Bot set a timer for 10 minutes
@Bot start a 5 minute timer labeled "tea steeping"
@Bot timer for 2h30m
```

### Managing via Slash Commands

- `/reminders list scope:mine` — list your pending reminders
- `/reminders list scope:channel` — list all reminders in this channel (admin only)
- `/reminders cancel <id>` — cancel a reminder by ID
- `/timers list scope:mine` — list your active timers
- `/timers list scope:channel` — list all timers in this channel (admin only)
- `/timers cancel <id>` — cancel a timer by ID

### Permission Model

- **Create reminders/timers**: any user
- **Cancel your own**: yes
- **Cancel others'**: admin only
- **List all in channel**: admin only

### Restart Safety

All reminders and timers are persisted in the DB. On bot restart:
1. APScheduler starts
2. `BotScheduler._recover_pending_jobs()` reads all unfired + uncancelled rows
3. Each is re-registered with APScheduler
4. Past-due rows fire immediately (with misfire grace)
5. Recurring reminders advance their `trigger_at` after each fire and re-register

You will not lose pending reminders even if the bot crashes.

### How the Tool Loop Works

```
User: "remind me tomorrow at 8pm to call home"
         │
         ▼
LLM emits tool_call: create_reminder({when: "tomorrow at 8pm", message: "call home"})
         │
         ▼
Orchestrator calls tools/executor.execute_tool_call()
         │
         ▼
1. Look up tool by name in registry → found
2. Validate args against Pydantic model (CreateReminderArgs) → valid
3. Check permissions → no restrictions on create
4. Execute:
   - parse_natural_time("tomorrow at 8pm") → UTC datetime
   - repo.create_reminder(...) → row in DB
   - scheduler.schedule_reminder(id) → APScheduler job registered
   - return ToolResult(success=True, data={reminder_id: 42, ...})
         │
         ▼
Orchestrator appends ToolResult as `tool` role message
         │
         ▼
LLM produces final reply: "got it — reminding you tomorrow at 8pm to call home"
         │
         ▼
Bot sends reply to Discord
```

Loop is bounded at `TOOL_MAX_ITERATIONS` (default 3) to prevent runaway. If a tool fails, the LLM is told — it must NOT claim success.

---

## Phase 4 — Custom APIs, Memory Proposals, Topic Tracking, Languages

### Custom HTTP Tools (Weather Example)

The bot includes a `get_weather` tool that hits `wttr.in` (free, no API key). Try:

```
@Bot what's the weather in Sydney?
@Bot weather in Tokyo
```

The LLM calls `get_weather`, the framework fetches `https://wttr.in/Sydney?format=3`, parses the compact response, and the LLM relays it in its own voice.

**Adding a new HTTP tool** = copy `app/tools/custom/weather.py`, change the `host`, `build_url()`, and `parse_response()`. The `BaseHTTPTool` framework handles:
- Whitelisted hosts (defined as class attributes — NOT runtime-configurable, prevents the LLM from discovering new endpoints)
- Per-request timeouts (default 8s, configurable)
- Response size limits (default 16KB, truncated beyond)
- HTTPS-only by default
- URL host validation (path injection defense)
- All errors wrapped into `ToolResult(success=False, ...)`

### Memory Extraction Proposals (Admin-Approved via Reaction)

Every ~15 messages per channel, the LLM proposes memories from recent chat. Each proposal is posted as a Discord message with ✅ / ❌ reactions:

```
🤔 Memory proposal (admin action needed)
> Group plays games every Friday at 8pm
kind: fact | confidence: 0.9
rationale: Stated explicitly and recurring
react ✅ to approve · ❌ to dismiss
```

- **Admins** react ✅ to create a memory row, or ❌ to dismiss
- **Non-admins** reactions are silently removed
- **Proposals are transient** — lost on restart. This is intentional; proposals are short-lived.
- **Privacy**: messages from users who opted out of memory are filtered before sending to the LLM
- **PII filter**: proposals containing phone/email/token patterns are refused at extraction time
- Tune with `MEMORY_EXTRACTION_TRIGGER_MESSAGE_COUNT` (default 15) and `MEMORY_EXTRACTION_MAX_PROPOSALS_PER_RUN` (default 3)

### Topic Tracking

Per-channel keyword extraction runs on every message. The top N keywords (default 5) from the last 10 messages are injected into the LLM context as:

```
═══ ACTIVE TOPIC (recent keywords) ═══
games, friday, 8pm, tonight, playing
(Use this as background context. Don't force these words into your reply.)
```

This gives the bot a sense of "what we're talking about right now" without storing topic state in the DB. Toggle with `ENABLE_TOPIC_TRACKING`.

### Per-User Language Preferences

Set your preferred reply language:

```
/language set:English
/language set:Nepali (Devanagari)
/language set:Nepali (Romanized)
/language set:Mixed (auto-detect per message)
/language clear
/language show
```

When set, the bot injects a "USER LANGUAGE PREFERENCE" block into the system prompt overriding per-message script detection. Useful if you always want replies in Devanagari even when you type in Romanized Nepali.

---

## Running with Docker

```bash
docker compose up --build
```

This builds the image, runs `alembic upgrade head`, and starts the bot. SQLite DB is persisted at `./data/groupmate.db` via a bind mount.

---

## Architecture & Roadmap

### Phase 1 ✅

- Discord connection with proper intents
- GLM-4.6 provider via Z.ai (OpenAI-compatible)
- Provider abstraction (swap-ready for OpenAI/Claude/etc.)
- Stable personality prompt (6-layer system prompt)
- Mention/reply-triggered conversation
- Per-channel rolling context buffer (last 20 msgs, token-budgeted)
- Basic multilingual behavior (per-message script matching via prompt)
- Response decision layer (rule-based: IGNORE/RESPOND)
- Message logging for audit + buffer rebuild

### Phase 2 ✅ (current)

- Conversation summarizer (every ~30 msgs per channel)
- Long-term structured memory (admin-approved only)
- `/remember`, `/forget`, `/memories list` slash commands
- Memory CRUD via context menu ("Remember this")
- Style profile + admin-approved style sampling ("Use for Style")
- Style profile injection into prompt (Layer 2)
- REACT outcome (emoji reactions for acknowledgements)
- User opt-out enforcement everywhere (`/privacy` slash command)
- Memory expiry sweeper (background task, soft-deletes expired memories)
- LLM-assisted response decision for gray zone (behind `ENABLE_LLM_DECISION_ASSIST` flag)
- PII sanitization on memory creation + summary generation

### Phase 3 ✅ (current)

- Tool framework: `BaseTool` abstract class + `ToolRegistry` + `ToolExecutor`
- Pydantic arg validation before any side effect
- Per-tool-call permission checks (creator or admin)
- Reminders tool (natural-language time, timezone-aware, recurring: daily/weekly/weekdays/weekends)
- Timers tool (short-duration, in-channel, max 24h)
- APScheduler integration with DB-persisted jobs
- Restart recovery: scheduler reads pending jobs from DB on startup
- Bounded tool-call loop (max 3 iterations) in orchestrator
- Slash commands: `/reminders list|cancel`, `/timers list|cancel`
- ToolResult serialization — LLM told explicitly when tools fail (never claims success)
- Per-channel context injection of tool schemas
- `USER_TIMEZONE` config (default `Australia/Sydney`)

### Phase 4 ✅ (current)

- Custom HTTP tool framework: `BaseHTTPTool` with whitelisted hosts (defined as class attributes, not runtime-configurable)
- Weather tool example (wttr.in, no API key)
- Per-request timeouts + response size limits + URL host validation (path injection defense)
- Memory extraction proposal UI: LLM proposes every N messages, admins approve via ✅ / ❌ reaction
- Topic tracking: per-channel keyword extraction from buffer, injected into context
- Per-user language preferences: `/language set` slash command, overrides per-message script detection
- `users.preferred_language` column (migration 0004)
- Reaction handler in `events.py` for proposal approval/rejection
- PII filtering on memory proposals

### Future

- Optional `pgvector` for fuzzy memory recall (deferred — current `<100 memories/channel` is fine without it)
- Topic state persistence (currently derived from buffer, no DB)
- FastAPI admin dashboard (slash commands cover admin needs)
- More custom HTTP tools (copy `weather.py` pattern)

---

## Configuration Reference

All settings are in `.env`. See `.env.example` for the full list.

Key Phase 2 settings:

| Variable | Default | Purpose |
|---|---|---|
| `ENABLE_STYLE_LEARNING` | `true` | Admin-approved style sampling + per-channel style profile |
| `ENABLE_LONG_TERM_MEMORY` | `true` | Admin-approved long-term memory |
| `ENABLE_CONVERSATION_SUMMARIES` | `true` | Periodic summarizer |
| `ENABLE_AUTONOMOUS_PARTICIPATION` | `false` | Bot may chime in without being mentioned |
| `ENABLE_REACT_OUTCOME` | `true` | Emoji reactions for short acknowledgements |
| `ENABLE_LLM_DECISION_ASSIST` | `false` | LLM-assisted gray-zone decision (adds latency + cost) |
| `SUMMARY_TRIGGER_MESSAGE_COUNT` | `30` | Summarize when channel has this many unsummarized messages |
| `MAX_MEMORIES_PER_CHANNEL` | `50` | Max memories returned per channel for context |
| `MEMORY_EXPIRY_SWEEP_MINUTES` | `60` | How often to sweep expired memories |
| `STYLE_PROFILE_REBUILD_THRESHOLD` | `5` | Rebuild style profile when N new samples accumulate |
| `ENABLE_TOOLS` | `true` | Enable tool-calling framework |
| `ENABLE_REMINDERS` | `true` | Allow reminders tool |
| `ENABLE_TIMERS` | `true` | Allow timers tool |
| `TOOL_MAX_ITERATIONS` | `3` | Max LLM↔tool round-trips per message |
| `USER_TIMEZONE` | `Australia/Sydney` | IANA timezone for natural-language time parsing |
| `ENABLE_CUSTOM_API_TOOLS` | `true` | Enable HTTP-based tools (weather, etc.) |
| `HTTP_TOOL_TIMEOUT_SECONDS` | `8.0` | Per-request timeout for HTTP tools |
| `HTTP_TOOL_MAX_RESPONSE_BYTES` | `16384` | Max response body size (truncated beyond) |
| `ENABLE_MEMORY_EXTRACTION` | `true` | LLM proposes memories; admins approve via reaction |
| `MEMORY_EXTRACTION_TRIGGER_MESSAGE_COUNT` | `15` | Run extraction every N messages per channel |
| `MEMORY_EXTRACTION_MAX_PROPOSALS_PER_RUN` | `3` | Cap proposals per run |
| `ENABLE_TOPIC_TRACKING` | `true` | Per-channel topic keyword extraction |
| `TOPIC_KEYWORDS_COUNT` | `5` | How many top keywords to extract |
| `ENABLE_PER_USER_LANGUAGE` | `true` | Allow `/language set` slash command |

---

## Project Layout

```
app/
├── main.py              # entrypoint + Phase 2 memory sweeper
├── config/settings.py   # pydantic-settings (Phase 2 flags added)
├── bot/                 # Discord layer
│   ├── client.py        # discord.Client + CommandTree factory
│   ├── events.py        # event wiring + slash command sync
│   ├── message_handler.py  # main pipeline (Phase 2: REACT + summarizer trigger)
│   ├── permissions.py   # admin/opt-out checks
│   ├── commands.py      # Phase 2 slash commands + context menus
│   └── reactions.py     # REACT outcome emoji picker
├── ai/                  # AI layer
│   ├── provider.py      # Z.ai GLM-4.6 (only file importing openai SDK)
│   ├── orchestrator.py  # LLM ↔ tool loop
│   ├── prompts.py       # 6-layer system prompt
│   ├── schemas.py       # Pydantic wire shapes (Phase 2 fields added)
│   └── response_policy.py  # IGNORE/REACT/RESPOND + LLM-assist (Phase 2)
├── context/             # Context assembly
│   ├── builder.py       # token budget + Phase 2 memory/style/summary injection
│   └── conversation_buffer.py  # in-memory rolling buffer
├── personality/         # Stable identity + style
│   ├── base_personality.py
│   ├── style_profile.py     # DB-backed per-channel profile (Phase 2)
│   └── examples.py
├── memory/              # Phase 2 memory subsystem
│   ├── models.py            # Pydantic schemas
│   ├── retrieval.py         # fetch summary + memories for context
│   ├── extraction.py        # LLM-assisted fact proposal (Phase 3 will surface UI)
│   ├── summarizer.py        # periodic conversation summarizer
│   └── policies.py          # retention + privacy rules
├── tools/               # Phase 3+4 tool framework
│   ├── schemas.py           # BaseTool, ToolResult, ToolContext
│   ├── registry.py          # ToolRegistry — central catalog
│   ├── executor.py          # validate + check permissions + execute
│   ├── time_format.py       # shared time/duration helpers (cycle-breaker)
│   ├── reminders/           # reminders tool (create/list/cancel)
│   │   ├── models.py
│   │   └── service.py
│   ├── timers/              # timers tool (start/list/cancel)
│   │   ├── models.py
│   │   └── service.py
│   └── custom/              # Phase 4: HTTP-based tools
│       ├── base_http.py     # BaseHTTPTool — whitelisted hosts + size limits
│       └── weather.py       # Example: wttr.in weather tool
├── context/             # Context assembly
│   ├── builder.py       # token budget + Phase 2-4 injection
│   ├── conversation_buffer.py  # in-memory rolling buffer
│   └── topic_context.py     # Phase 4: per-channel keyword extraction
├── memory/              # Phase 2+4 memory subsystem
│   ├── models.py            # Pydantic schemas
│   ├── retrieval.py         # fetch summary + memories for context
│   ├── extraction.py        # LLM-assisted fact proposal
│   ├── proposals.py         # Phase 4: ✅/❌ reaction approval flow
│   ├── summarizer.py        # periodic conversation summarizer
│   └── policies.py          # retention + privacy rules
├── database/            # SQLAlchemy + Alembic
│   ├── connection.py        # async engine + session
│   ├── models.py            # Phase 4: preferred_language column on users
│   └── repository.py        # CRUD for all tables
└── workers/scheduler.py # Phase 3: real APScheduler + restart recovery
```

---

## Debugging Common Failures

### Slash commands don't show up

1. Make sure `DISCORD_TEST_GUILD_ID` is set in `.env` (commands sync to that guild instantly; global sync takes ~1 hour).
2. Restart the bot — sync happens on `on_ready`.
3. In Discord, fully quit and reopen the client (slash command cache is sticky).
4. Check bot logs for `commands_synced` — should show count > 0.

### Context menu items don't appear

Same as slash commands — they're registered through the same CommandTree. Also ensure the bot has the `applications.commands` scope (check the OAuth invite URL).

### `/remember` returns "couldn't find your user record"

The bot only creates user rows when it sees a message from you. Send any message in a channel the bot can read, then retry.

### "Use for Style" returns "that user has opted out of style learning"

The message author has run `/privacy style on`. Either pick a different message, or ask them to opt back in.

### Style profile rebuild fails

Check logs for `style_rebuild_provider_error` or `style_rebuild_parse_failed`. Common cause: not enough samples (need at least `STYLE_PROFILE_REBUILD_THRESHOLD`, default 5). Force with `/style rebuild`.

### Bot reacts to acknowledgements when you wanted a reply

Disable with `ENABLE_REACT_OUTCOME=false` in `.env`, or just phrase your message as a question (`@Bot ok?`).

### Memory not appearing in context

1. Check `/memories list scope:this channel` — is it there?
2. Check the memory's `expires_at` — past dates are filtered out.
3. Check the memory's `user_id` — user-scoped memories are NEVER injected into shared context (privacy protection).
4. Check `ENABLE_LONG_TERM_MEMORY=true` in `.env`.

### Tool calls not happening

1. Check `ENABLE_TOOLS=true` in `.env`.
2. Set `LOG_LEVEL=DEBUG` and look for `tool_call_dispatched` log entries — if the LLM is calling tools, you'll see them.
3. Make sure the user is mentioning the bot — Phase 3 doesn't change the response decision rules.
4. Verify the tool is in the registry — check `tool_registered` logs at startup. Should see 6 tools: `create_reminder`, `list_reminders`, `cancel_reminder`, `start_timer`, `list_timers`, `cancel_timer`.

### Reminder fires but no message appears

1. Check `scheduler_started` log on bot startup.
2. Check `reminder_fired` log when the trigger time arrives.
3. If you see `scheduler_send_no_client` or `scheduler_send_fetch_channel_failed`, the bot's Discord client doesn't have the channel cached. Restart the bot — the scheduler re-registers and the client is set on `on_ready`.
4. Verify the bot has `Send Messages` permission in the target channel.

### Reminder didn't fire after restart

1. Check the DB: `SELECT * FROM reminders WHERE is_fired=0 AND is_cancelled=0;` — is the row there?
2. Check `recovery_complete` log on startup — should show `reminders_scheduled > 0`.
3. Past-due reminders fire with a 5-minute misfire grace. If you restarted 10+ minutes after the trigger time, the reminder may have been dropped — check `misfire_grace_time` in the scheduler config.

### Bot claims success but tool failed

This shouldn't happen. If it does:
1. Check the logs for `tool_executed` — what was `success`?
2. If `success=false` but the bot said "done" — the LLM ignored the ToolResult. Set `LOG_LEVEL=DEBUG` and inspect the `tool` role message sent back to the LLM. The system prompt explicitly says "If a tool returns failure, NEVER claim it succeeded."

---

## Privacy & Data

- All messages the bot sees are logged to the `messages` table (for audit + buffer rebuild).
- The bot NEVER sends full chat history to the LLM — only the last ~20 messages per channel.
- **Long-term memory is admin-approved only.** The LLM may *propose* memories (Phase 3), but humans approve every write.
- **Style samples are admin-approved only.** The LLM only sees the distilled structured profile, never raw message content.
- Per-user opt-out flags: `opt_out_reply`, `opt_out_style`, `opt_out_memory` — toggle via `/privacy`.
- PII patterns (phone, email, tokens) are detected and refused at memory-creation time.
- **Tools cannot execute arbitrary code.** Each tool is a discrete, audited function. Adding a new tool = create a `BaseTool` subclass + one line in the registry. The LLM cannot access the database, shell, or network directly — only through registered tools.
- **Tool args are validated with Pydantic before any side effect.** Malformed arguments never reach the executor.
- **Permissions are checked per tool call.** Cancel requires creator or admin. List-all-in-channel requires admin.
- Reminders and timers persist in the DB — restart-safe. No external job queue.
- All data stays in your local SQLite DB (or your own Postgres instance). Nothing leaves your infra except the LLM API call to Z.ai.
- Memories are soft-deleted (audit trail preserved) when forgotten or expired.

---

## License

Private — for personal use in a friend group.

