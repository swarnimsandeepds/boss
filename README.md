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

1. `POST /shop` logs the request, loads the agent track record and past lessons. If the request
   is too vague to buy the right thing (for example no quantity), Boss asks one question instead
   of guessing, and no agent runs.
2. Agents A, B, and C each run a live web search with a different strategy and submit one real bid.
3. Boss scores every bid, rejects rule breakers, and picks a winner with a one line reason each.
4. `POST /approve/{run_id}` first re-reads the live product page. If the price rose by $5 or more,
   Boss stops and asks. If the item is out of stock, Boss falls back to the best runner-up and
   asks you to review it. Otherwise it logs your approval and opens the real product page.
   Boss never pays or checks out for you.
5. `POST /feedback/{run_id}` records an overrule. The reason becomes a lesson that every
   agent and the judge receive on all future runs, and the agent's trust score drops.

`GET /analytics` runs SQL over the trail: average time and price per agent, the average
price gap between winning and losing bids, recent rejection reasons, and approve versus
overrule counts. The sidebar shows it.

Every step is a row in the `events` table. The page polls `GET /trail/{run_id}` so you
can watch each agent's searches, bid, and verdict as they happen.

## Drills

```bash
python drills.py
```

Four drills from the build spec: a $30 price jump before checkout, a wrong-size bid, the winner
going out of stock, and a vague request. The first two checkout drills test Boss's rules directly.
The wrong-size and vague-request drills call the real judge and pre-check, so they need
`ANTHROPIC_API_KEY`. Drills are tests and are not part of the product path.

## Sponsor tools

- SingleStore: audit trail, live agent scoreboard, lesson store
- Amplitude: decision events (`bid_won`, `bid_rejected`, `user_rejected`, `order_approved`)
