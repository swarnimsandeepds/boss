# Boss: build spec for Claude Code

You are finishing a hackathon project. Hard deadline: submission closes at 4:30 PM PT today.
Work in priority order, keep the app runnable at every step, and stop adding features at 4:10 PM.

## What we are building

**Boss** is a personal agent that manages other buying agents.

- The shopper types one request and their rules.
- Three real buying agents search the live web, each with a different strategy, and each submits one real bid (real product, real price, real link).
- Boss judges the bids against the shopper's rules and past lessons, rejects bad ones, and picks a winner with a reason for each agent.
- Every step by every agent is written to an audit trail the shopper can inspect.
- When the shopper overrules Boss, the reason becomes a lesson that all future runs use, and that agent's trust score drops.

Hackathon challenge statement: "Build a shopping agent with an audit trail." Agents should use
reliable context, take a meaningful action, and show the information and steps behind their decisions.

Pitch line: "Every shopping agent works for a platform. Boss works for you, and makes them compete."

## Non-negotiable rules

1. **Nothing mocked.** No hardcoded products, prices, bids, or fake checkout. Every bid comes from a live web search. If a call fails, show the failure in the trace; never substitute fake data.
2. **Every action is logged.** Any new step you add must call `log(run_id, step, actor, payload)`.
3. **The action is real but stops at the shopper.** Approval logs the decision and opens the real product page. Do not automate payment or checkout.
4. **Do not break the working core** to add a sponsor. Each integration must be optional behind an env var and fail soft.
5. Never commit secrets. Read all keys from environment variables. Add a `.env.example`.

## Current state (already written, tested without live keys)

```
main.py            FastAPI app: agents, judge, audit trail, scoreboard, lessons, Amplitude tracking
index.html         Single page UI served at /
schema.sql         SingleStore table
requirements.txt
README.md
```

### Endpoints

| Method | Path | Purpose |
|---|---|---|
| GET | `/` | The page |
| POST | `/shop` | Body `{request, rules, run_id?}`. Runs the three agents and the judge. |
| POST | `/approve/{run_id}` | Logs approval, returns the winning product URL |
| POST | `/feedback/{run_id}` | Body `{agent, reason}`. Overrule, creates a lesson |
| GET | `/trail/{run_id}` | All audit events for a run (the page polls this every second while agents work) |
| GET | `/runs` | Recent runs |
| GET | `/state` | Scoreboard, lessons, agent list |

### Audit event steps

`request_received`, `context_loaded`, `agent_started`, `web_search`, `bid_submitted`,
`judging_started`, `bid_won`, `bid_runner_up`, `bid_rejected`, `decision_made`,
`order_approved`, `user_rejected`, `lesson_learned`, `judge_failed`

Actors are `user`, `boss`, `A`, `B`, `C`.

### UI (already built)

- Request and rules inputs
- Three agent lanes with live status, searches run, bid, link, duration, and Boss's verdict
- Decision card with Approve, and Overrule with a reason
- Trace panel with per-actor filters and expandable payloads
- Sidebar: agent track record (bids, wins, rejected, overruled, trust), lessons learned, past runs

## Step 0: get the core running (do this first, 10 minutes max)

1. `pip install -r requirements.txt`, set `ANTHROPIC_API_KEY`, run `uvicorn main:app --reload --port 8000`.
2. **Verify against current Anthropic docs** before trusting these two constants in `main.py`:
   - `MODEL` (default `claude-sonnet-5-5`)
   - `WEB_SEARCH_TOOL` (`{"type": "web_search_20250305", "name": "web_search", "max_uses": 3}`). The tool version string may be outdated. Fix it if the API rejects it.
3. Run one real request end to end. Confirm three real bids with working links and a judge decision.
4. If agents are slow (over 40 seconds), lower `max_uses` to 2.
5. If the model wraps JSON in prose and parsing fails, tighten the prompt or parse more defensively in `extract_json`.

If a different LLM provider key is the only one available, swap the two `llm.messages.create` calls
for that provider, keeping live web search on the buying agents.

## Step 1: SingleStore (required, target prize)

Role: the audit trail, the live scoreboard, and the lesson store.

1. Create a free SingleStore workspace, run `schema.sql`.
2. Set `SINGLESTORE_URL='user:password@host:3306/boss'`.
3. Verify `log`, `events`, and `scoreboard` in `main.py` work against the real database. The code uses the `singlestoredb` package with `%s` parameters. Fix parameter style or JSON handling if the driver differs.
4. Make the use deeper than plain inserts. Add one of:
   - A `/analytics` endpoint with SQL over the trail: average price gap between winner and losers, average agent latency, rejection reasons grouped by agent. Show it in the sidebar.
   - Vector search over past lessons so only lessons relevant to the current request are loaded.

## Step 2: Firedrill (target prize)

Role: prove Boss cannot be fooled before it touches real money.

- Docs: https://firedrill.run/agent-setup.md , https://firedrill.run/run-your-agent.md , https://docs.firedrill.run/llms.txt
- Connect through the remote MCP server at `https://mcp.firedrill.run` (browser approval) or the `firedrill-run` CLI. The agent runs locally; Firedrill hosts the scenarios and reports.
- Build 3 to 4 scenarios and success criteria:
  1. Price jumps $30 between bid and checkout. Boss must stop and ask.
  2. An agent bids the wrong size. Boss must reject it.
  3. The cheapest seller goes out of stock. Boss must fall back to the runner up.
  4. Vague request ("get me wings"). Boss must ask for quantity instead of guessing.
- Save the report link or a screenshot for the demo video. Firedrill tests Boss; it is not part of the product path.
- Time box: 15 minutes. If setup is not working by then, skip.

## Step 3: Cognee (target prize)

Role: long term memory of the shopper's rules, lessons, and preferences.

- Read the current Cognee docs for the Python quickstart. Do not guess the API.
- On `/feedback`, also add the lesson to Cognee. On `/shop`, query Cognee with the request text and pass the relevant memories to the agents and the judge, alongside the SingleStore lessons.
- Log a `memory_recalled` event with what was retrieved so it shows in the trace.
- Env var gated. Time box: 10 minutes.

## Step 4: Amplitude (target prize)

Role: product analytics on agent decisions. Already wired in `track()` in `main.py`.

- Set `AMPLITUDE_API_KEY`, confirm events arrive.
- Build one chart in Amplitude (for example rejections by agent, or approve versus overrule rate) and capture it for the video.
- If time allows, add a Statsig experiment that switches between two judge prompts.

## Step 5: Superlinked SIE Serverless (only if ahead of schedule)

Role: rerank the bids against the request and the shopper's preferences before the judge sees them.

- Docs: https://superlinked.com/docs/quickstart , SDK: `pip install sie-sdk`
- Log a `bids_reranked` event with the rerank scores.
- Time box: 10 minutes.

## Step 6: Airbyte (only if their staff has enabled access)

Role: real purchase history as context, through Airbyte's agent connectors or context layer.

- Docs: https://docs.airbyte.com/ai-agents/get-started
- The context layer is in private beta and needs an org admin to enable agent access. Do not start this unless access is confirmed.
- Log a `history_loaded` event.

## Cut order when behind

Drop in this order: Airbyte, Superlinked, Amplitude extras, Cognee. Never drop SingleStore, the trace UI, or the demo video.

## Acceptance checklist

- [ ] One request returns three real bids with working product links
- [ ] Boss rejects at least one bid with a specific reason when a rule is broken
- [ ] The trace shows every agent step live while agents work
- [ ] Approve logs `order_approved` and opens the product page
- [ ] Overrule creates a lesson, and the next run visibly uses it (the judge cites it)
- [ ] Trust score changes after an overrule
- [ ] Trail, scoreboard, and lessons persist in SingleStore across a server restart
- [ ] No secrets in the repo

## Submission (start at 4:10 PM, due 4:30 PM PT)

Submit at https://tokensand.com/personalagenthack/submit with:

1. Public GitHub repo
2. Short demo video with a shareable link
3. What was built and which sponsor tools were used (update README.md to list only tools that actually work)
4. Team names and contact emails (teams are 2 to 4 people)

### Demo script (under 3 minutes)

1. "Every shopping agent works for a platform. Boss works for me."
2. Type a request with a rule. Show three agents searching live in the trace.
3. Show Boss rejecting one bid with the reason, and picking a winner.
4. Open the trace details for one agent: its searches, its bid, the verdict.
5. Overrule the winner with a reason. Run again. Show the judge citing the lesson and the trust score dropping.
6. Show the Firedrill report: "Boss passed these drills before it touched my money."
7. Approve. The real product page opens. "Every step you saw is a row in SingleStore."
