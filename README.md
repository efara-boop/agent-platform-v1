# Agent Platform v1

Local Windows 11 development orchestration with nine roles. Analyst, architect, reviewer and orchestrator use the installed Codex CLI with its existing ChatGPT sign-in. Backend, frontend, database, QA and DevOps use a Qwen Coder model served by LM Studio. No OpenAI API key is used.

## Start

Requires Python 3.11+, Git, Codex CLI signed in with ChatGPT, and LM Studio for local agents. Engram is bundled locally when installed with the script below. From this directory:

```powershell
python -m agent_platform doctor
python -m agent_platform agents
python -m agent_platform skills "REST API Symfony Kafka"
python -m agent_platform run analyst "Analyze requirement ..."
python -m agent_platform run backend "Analyze repository and implement ..."
python -m agent_platform run qa "Review current diff"
python -m agent_platform run reviewer "Review current changes"
python -m agent_platform workflow "Implement feature ..."
python -m agent_platform chat
python -m agent_platform memory status
python -m agent_platform memory search "retry decision"
python -m agent_platform memory save "API retry convention" "Use request IDs for idempotency."
```

Use `--workspace C:\path\to\project` before the subcommand to target another local repository. Python can also be installed with `python -m pip install -e .`, but installation is not required when run from this directory.

## Local chat

Install the optional Chainlit UI once, keep LM Studio running, then start the chat:

```powershell
python -m pip install -e .[chat]
python -m agent_platform doctor
python -m agent_platform --workspace C:\path\to\project chat
```

Open `http://127.0.0.1:8765`. The orchestrator uses Codex first and falls back to the local Fable model
when Codex is unavailable. It classifies questions and tasks, asks up to three
material clarifying questions, and starts the existing workflow in the background. New chat messages
during a run are queued as guidance for the next agent stage. Use `/status` to inspect progress and
`/stop` to request cancellation after the current agent call. The chat UI does not depend on an
available ChatGPT/Codex quota; Codex-backed stages retain their configured LM Studio fallback.

## Mission Control dashboard

The platform can report local agent runs to Builderz Labs Mission Control through its direct CLI integration API. The integration is off by default and does not change orchestration behavior when Mission Control is unavailable.

Start Mission Control separately:

```powershell
git clone https://github.com/builderz-labs/mission-control.git C:\mission-control
cd C:\mission-control
corepack enable
pnpm install
pnpm dev
```

On Windows without administrator rights, `corepack.enable` may fail because it tries to write shims into `C:\Program Files\nodejs`. Use a local pnpm instead:

```powershell
git clone https://github.com/builderz-labs/mission-control.git C:\PROMONEYv1\mission-control
New-Item -ItemType Directory -Force C:\PROMONEYv1\.tools | Out-Null
npm install --prefix C:\PROMONEYv1\.tools pnpm@10.29.3
cd C:\PROMONEYv1\mission-control
node C:\PROMONEYv1\.tools\node_modules\pnpm\bin\pnpm.cjs install
node scripts/check-node-version.mjs
node .\node_modules\next\dist\bin\next dev --hostname 127.0.0.1 --port 3000
```

Open `http://localhost:3000`, complete setup, and copy the API key from Settings. Then enable reporting for this platform session:

```powershell
$env:MC_ENABLED="true"
$env:MC_URL="http://localhost:3000"
$env:MC_API_KEY="paste-api-key-here"
python -m agent_platform mission-control status
python -m agent_platform mission-control connect-test --agent backend
```

Run agents or workflows normally:

```powershell
python -m agent_platform run backend "Analyze repository and implement ..."
python -m agent_platform workflow "Implement feature ..."
```

Each completed agent stage registers a short-lived Mission Control connection named `promoney-<role>`, sends a heartbeat with token usage when available, then disconnects. Workflow start, stage, finish, and Mission Control reporting diagnostics are also written to `traces/runs.jsonl`. To make the integration persistent, edit `config/mission_control.toml` or keep the three `MC_*` environment variables in your shell profile.

`doctor` checks tools and `GET /v1/models`, selects the available Qwen Coder id, and writes `.state/runtime-config.json`. The default endpoint is `http://localhost:1234`, which was verified from this project machine. Set `LMSTUDIO_BASE_URL` when the reachable endpoint differs. If LM Studio is offline, configuration and skill setup still work. No port or firewall changes are made automatically. `/v1/chat/completions` is implemented; the `future_api` setting documents a later `/v1/responses` adapter.

The local agents use bounded OpenAI-compatible tool calls for listing, reading and writing workspace files. Their `run_tests` tool delegates the fixed unittest command to a separate Codex process in a read-only sandbox; it verifies the executed command and exit code from Codex JSONL events instead of trusting the model's summary. Writes to existing files require a matching SHA256 from a prior read. The platform limits each local run to 12 tool rounds, local concurrency to one, and Codex concurrency to one. Codex runs with `stdin=DEVNULL`, `--ephemeral`, `--output-schema` and `--output-last-message`. Agents cannot execute arbitrary shell commands through the platform. Review actual changes before accepting them.

LM Studio's native model metadata reports the loaded context length when available. The platform uses the lower of that value and `context_target_tokens` (32,768 by default) as its preferred limit, reserving `max_output_tokens` (4,096) for output. It estimates each request before sending, warns at 80% of the input budget, compacts older complete tool rounds, and stops before a predicted overflow. A pessimistic bound based on the exact serialized HTTP request guards the loaded model's hard window before the first server token count arrives. The preferred-limit estimate is calibrated with LM Studio's actual `usage.prompt_tokens` after each response. Each round's numeric usage is recorded in `traces/runs.jsonl`; warnings also appear on stderr. A missing `usage` value leaves the preferred-limit estimate approximate. The loaded Qwen instance was observed with an 88,832-token context, so the project target is currently the tighter limit.
If native loaded-window metadata is unavailable, local agent runs stop until you set `known_context_length` in `config/providers.toml` to a value verified in LM Studio. The default `0` means unknown; the 32,768 target is not treated as proof of the model's actual window.

Skills are cloned into `vendor/claude-skills`, indexed by metadata, ranked per task, and at most four `SKILL.md` files are loaded. Suspicious content is blocked for human review using criteria from the vendored `skill-security-auditor`; third-party scripts never execute automatically. Context is selected before the 32,768-token target; it is not truncated in the middle. `.ai/` is the versioned project knowledge, `.state/current-task.json` is ephemeral working state, and Engram supplies short, relevant prior notes. Before each agent run, Engram search results are capped at 3,000 characters and treated as untrusted context. A completed workflow stores a short completion record after reviewer success if task and review text pass a strict sensitive-content check; manual memory commands are available above. Engram failures do not stop the workflow.

Engram v2.2.1 is installed in `vendor/engram` for this workspace; its SQLite data stays in the selected workspace's `.state/engram`. The binary and platform runtime state are ignored by Git. On another Windows amd64 checkout, run `python scripts/install_engram.py`; the installer refuses to overwrite an existing binary and verifies the official release archive SHA256. The adapter disables Engram update checks and cloud autosync. It does not run `engram setup codex` or modify the global Codex configuration. Engram is optional if its binary is absent.

## Git Configuration

This repository is connected to GitHub at https://github.com/efara-boop/moneymaker-git

To initialize git for this project, you can use the provided setup script:
```powershell
python scripts/setup_git_repo.py
```

Or manually run these commands:
```powershell
git init
git remote add origin https://github.com/efara-boop/moneymaker-git
git add .
git commit -m "Initial commit"
```

**Important Git Policies:**
- Git push and force push are **forbidden** in this setup (as per config/policies.toml)
- All git operations are allowed for read, diff, and branch operations
- Commits require human approval before being pushed
- The repository is already configured with a sensible .gitignore

## Verification

```powershell
python -m unittest discover -s tests -v
python scripts/smoke_test.py
python scripts/check_lmstudio_tools.py
```

The smoke test checks exact `QWEN_OK`, exact `CODEX_OK`, and skill routing. Offline providers are reported as failures; unit tests mock external calls. Production access, deploys, destructive database changes and commits require explicit human approval. Git push and force push are forbidden in this setup.
