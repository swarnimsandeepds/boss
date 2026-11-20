# Boss

One agent that manages your buying agents. Three real buying agents search the live web and bid.
Boss judges the bids against your rules, shows the full audit trail, and learns when you overrule it.

## Run

```bash
pip install -r requirements.txt
cp .env.example .env      # then fill in the keys; .env is git-ignored
uvicorn main:app --reload --port 8000
```

Open http://localhost:8000

Without `SINGLESTORE_URL` the trail is kept in process memory so you can test the flow.
Set it before recording the demo so the trail, scoreboard, and lessons persist.

## How it works

1. `POST /shop` logs the request, loads the agent track record and past lessons.
2. Agents A, B, and C each run a live web search with a different strategy and submit one real bid.
3. Boss scores every bid, rejects rule breakers, and picks a winner with a one line reason each.
4. `POST /approve/{run_id}` logs your approval and opens the real product page.
5. `POST /feedback/{run_id}` records an overrule. The reason becomes a lesson that every
   agent and the judge receive on all future runs, and the agent's trust score drops.

`GET /analytics` runs SQL over the trail: average time and price per agent, the average
price gap between winning and losing bids, recent rejection reasons, and approve versus
overrule counts. The sidebar shows it.

Every step is a row in the `events` table. The page polls `GET /trail/{run_id}` so you
can watch each agent's searches, bid, and verdict as they happen.

## Sponsor tools

- SingleStore: audit trail, live agent scoreboard, lesson store
- Amplitude: decision events (`bid_won`, `bid_rejected`, `user_rejected`, `order_approved`)
