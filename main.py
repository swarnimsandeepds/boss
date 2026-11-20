"""Boss: one agent that manages your buying agents, learns from you, and logs every step.

Run:  uvicorn main:app --reload --port 8000
Env:  ANTHROPIC_API_KEY   (required)
      SINGLESTORE_URL     user:password@host:3306/database  (audit trail)
      AMPLITUDE_API_KEY   (optional, event tracking)
      BOSS_MODEL          (optional, defaults below)
"""
import asyncio
import json
import os
import re
import time
import uuid
from datetime import datetime, timezone

import httpx
from anthropic import AsyncAnthropic
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel


def _load_dotenv() -> None:
    """Read KEY=value lines from .env next to this file. Real environment variables win."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.exists(path):
        return
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            value = value.strip().strip("'\"")
            if value:
                os.environ.setdefault(key.strip(), value)


_load_dotenv()

MODEL = os.getenv("BOSS_MODEL", "claude-sonnet-5-5")
WEB_SEARCH_TOOL = {
    "type": os.getenv("BOSS_WEB_SEARCH_TYPE", "web_search_20260209"),
    "name": "web_search",
    "max_uses": int(os.getenv("BOSS_MAX_SEARCHES", "3")),
}
WEB_FETCH_TOOL = {"type": os.getenv("BOSS_WEB_FETCH_TYPE", "web_fetch_20260209"), "name": "web_fetch", "max_uses": 2}
VERIFY_CHECKOUT = os.getenv("BOSS_VERIFY_CHECKOUT", "1") != "0"   # re-check price and stock before approval
ASK_WHEN_VAGUE = os.getenv("BOSS_ASK_WHEN_VAGUE", "1") != "0"     # ask instead of guessing on vague requests
PRICE_JUMP_USD = float(os.getenv("BOSS_PRICE_JUMP_USD", "5"))     # a rise this large needs the shopper's yes
AGENT_EFFORT = os.getenv("BOSS_AGENT_EFFORT", "low")    # buying agents: speed matters
JUDGE_EFFORT = os.getenv("BOSS_JUDGE_EFFORT", "medium")
AMPLITUDE_KEY = os.getenv("AMPLITUDE_API_KEY")
S2_URL = os.getenv("SINGLESTORE_URL")

AGENTS = {
    "A": "Find the lowest total price from any reputable seller.",
    "B": "Find the fastest delivery from a major retailer, even if it costs a bit more.",
    "C": "Find the best value refurbished, open box, or used option from a trusted marketplace.",
}
SCORE_STEPS = {"bid_submitted": "bids", "bid_won": "wins", "bid_rejected": "rejected", "user_rejected": "overruled"}

app = FastAPI(title="Boss")
_llm_client: AsyncAnthropic | None = None


def llm() -> AsyncAnthropic:
    """Created on first use so the page still loads, and the trace shows the failure, without a key."""
    global _llm_client
    if _llm_client is None:
        _llm_client = AsyncAnthropic()
    return _llm_client
_memory_events: list[dict] = []  # only used if SINGLESTORE_URL is not set


# ---------- audit trail (SingleStore) ----------

def _db():
    import singlestoredb as s2
    return s2.connect(S2_URL)


def log(run_id: str, step: str, actor: str, payload: dict) -> None:
    """Write one audit event. Every action by every agent goes through here."""
    if not S2_URL:
        _memory_events.append({
            "run_id": run_id, "step": step, "actor": actor, "payload": payload,
            "created_at": datetime.now(timezone.utc).isoformat(),
        })
        return
    with _db() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO events (run_id, step, actor, payload) VALUES (%s, %s, %s, %s)",
            (run_id, step, actor, json.dumps(payload)),
        )
        conn.commit()


def events(run_id: str | None = None, step: str | None = None, limit: int = 500, newest_first: bool = False) -> list[dict]:
    """Read audit events, optionally filtered by run and step."""
    if not S2_URL:
        rows = [e for e in _memory_events
                if (run_id is None or e["run_id"] == run_id) and (step is None or e["step"] == step)]
        return (rows[::-1] if newest_first else rows)[:limit]
    where, params = [], []
    if run_id is not None:
        where.append("run_id = %s"); params.append(run_id)
    if step is not None:
        where.append("step = %s"); params.append(step)
    sql = "SELECT run_id, step, actor, payload, created_at FROM events"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += f" ORDER BY id {'DESC' if newest_first else 'ASC'} LIMIT {int(limit)}"
    with _db() as conn, conn.cursor() as cur:
        cur.execute(sql, tuple(params))
        out = []
        for rid, st, actor, payload, created_at in cur.fetchall():
            if isinstance(payload, str):
                payload = json.loads(payload)
            out.append({"run_id": rid, "step": st, "actor": actor, "payload": payload, "created_at": str(created_at)})
        return out


def scoreboard() -> list[dict]:
    """Live track record per buying agent, computed over the whole audit trail."""
    board = {n: {"agent": n, "bids": 0, "wins": 0, "rejected": 0, "overruled": 0} for n in AGENTS}
    if not S2_URL:
        for e in _memory_events:
            if e["step"] in SCORE_STEPS and e["actor"] in board:
                board[e["actor"]][SCORE_STEPS[e["step"]]] += 1
    else:
        with _db() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT actor, step, COUNT(*) FROM events "
                "WHERE step IN ('bid_submitted', 'bid_won', 'bid_rejected', 'user_rejected') "
                "GROUP BY actor, step"
            )
            for actor, step, n in cur.fetchall():
                if actor in board:
                    board[actor][SCORE_STEPS[step]] = int(n)
    for row in board.values():
        # trust = wins the shopper did not overrule, as a share of all bids
        row["trust"] = round(100 * max(row["wins"] - row["overruled"], 0) / row["bids"]) if row["bids"] else None
    return list(board.values())


def lessons(limit: int = 10) -> list[str]:
    """What Boss has learned from the shopper overruling it. Fed into every future run."""
    return [e["payload"]["lesson"] for e in events(step="lesson_learned", limit=limit, newest_first=True)]


# ---------- analytics over the audit trail ----------

def _num(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _avg(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 2) if values else None


def _analytics_python() -> dict:
    """Same numbers as the SQL below, computed in process. Used when there is no database."""
    bids = events(step="bid_submitted", limit=5000)
    per_agent = []
    for name in AGENTS:
        mine = [b["payload"] for b in bids if b["actor"] == name]
        per_agent.append({
            "agent": name, "bids": len(mine),
            "avg_seconds": _avg([d / 1000 for d in (_num(b.get("duration_ms")) for b in mine) if d is not None]),
            "avg_price_usd": _avg([p for p in (_num(b.get("price_usd")) for b in mine) if p]),
        })
    price = {(b["run_id"], b["actor"]): _num(b["payload"].get("price_usd")) for b in bids}
    gaps = []
    for d in events(step="decision_made", limit=5000):
        if d["payload"].get("fallback"):
            continue  # compare against the judge's own pick, not a later stock fallback
        won = price.get((d["run_id"], d["payload"].get("winner")))
        if not won:
            continue
        gaps += [p - won for (rid, actor), p in price.items()
                 if rid == d["run_id"] and actor != d["payload"].get("winner") and p]
    rejections = [{"agent": e["actor"], "reason": e["payload"].get("reason")}
                  for e in events(step="bid_rejected", limit=6, newest_first=True)]
    return {
        "source": "memory", "per_agent": per_agent, "avg_price_gap_usd": _avg(gaps),
        "rejections": rejections,
        "approved": len(events(step="order_approved", limit=5000)),
        "overruled": len(events(step="user_rejected", limit=5000)),
    }


def _analytics_sql() -> dict:
    """SQL over the SingleStore trail: latency and price per agent, winner price gap, rejections."""
    price = "JSON_EXTRACT_DOUBLE({}.payload, 'price_usd')"
    with _db() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT e.actor, COUNT(*), AVG(JSON_EXTRACT_DOUBLE(e.payload, 'duration_ms')) / 1000, "
            f"AVG(NULLIF({price.format('e')}, 0)) "
            "FROM events e WHERE e.step = 'bid_submitted' GROUP BY e.actor"
        )
        rows = {a: (int(n), secs, usd) for a, n, secs, usd in cur.fetchall()}
        per_agent = [{
            "agent": name, "bids": rows.get(name, (0, None, None))[0],
            "avg_seconds": round(float(rows[name][1]), 2) if name in rows and rows[name][1] is not None else None,
            "avg_price_usd": round(float(rows[name][2]), 2) if name in rows and rows[name][2] is not None else None,
        } for name in AGENTS]
        cur.execute(
            f"SELECT AVG({price.format('l')} - {price.format('w')}) "
            "FROM events d "
            "JOIN events w ON w.run_id = d.run_id AND w.step = 'bid_submitted' "
            "  AND w.actor = JSON_EXTRACT_STRING(d.payload, 'winner') "
            "JOIN events l ON l.run_id = d.run_id AND l.step = 'bid_submitted' AND l.actor <> w.actor "
            "WHERE d.step = 'decision_made' AND JSON_EXTRACT_JSON(d.payload, 'fallback') IS NULL "
            f"AND {price.format('w')} > 0 AND {price.format('l')} > 0"
        )
        gap = cur.fetchone()[0]
        cur.execute(
            "SELECT actor, JSON_EXTRACT_STRING(payload, 'reason') FROM events "
            "WHERE step = 'bid_rejected' ORDER BY id DESC LIMIT 6"
        )
        rejections = [{"agent": a, "reason": r} for a, r in cur.fetchall()]
        cur.execute("SELECT step, COUNT(*) FROM events WHERE step IN ('order_approved', 'user_rejected') GROUP BY step")
        counts = {step: int(n) for step, n in cur.fetchall()}
    return {
        "source": "singlestore", "per_agent": per_agent,
        "avg_price_gap_usd": round(float(gap), 2) if gap is not None else None,
        "rejections": rejections,
        "approved": counts.get("order_approved", 0), "overruled": counts.get("user_rejected", 0),
    }


def analytics() -> dict:
    if not S2_URL:
        return _analytics_python()
    try:
        return _analytics_sql()
    except Exception as exc:  # the sidebar must not break the page; say what happened
        return {**_analytics_python(), "source": "fallback", "sql_error": str(exc)}


# ---------- analytics (Amplitude) ----------

async def track(event_type: str, props: dict) -> None:
    if not AMPLITUDE_KEY:
        return
    body = {
        "api_key": AMPLITUDE_KEY,
        "events": [{"user_id": "boss-demo-user", "event_type": event_type, "event_properties": props}],
    }
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            await client.post("https://api2.amplitude.com/2/httpapi", json=body)
    except Exception:
        pass  # analytics must never break a purchase decision


# ---------- LLM helpers ----------

def extract_json(text: str) -> dict:
    """Return the last top-level JSON object in the text, ignoring any prose around it."""
    decoder, found, pos = json.JSONDecoder(), None, 0
    while (start := text.find("{", pos)) != -1:
        try:
            obj, end = decoder.raw_decode(text, start)
        except json.JSONDecodeError:
            pos = start + 1
            continue
        if isinstance(obj, dict):
            found = obj
        pos = end
    if found is None:
        raise ValueError("no JSON in model output")
    return found


def text_of(response) -> str:
    return "".join(b.text for b in response.content if getattr(b, "type", "") == "text")


async def search_and_answer(run_id: str, name: str, prompt: str, tools: list[dict] | None = None) -> tuple[str, list[str]]:
    """One agent turn with live web search. Each search is logged the moment it runs."""
    messages: list[dict] = [{"role": "user", "content": prompt}]
    searches: list[str] = []
    for _ in range(4):  # the server may pause a long search turn; resume it
        async with llm().messages.stream(
            model=MODEL, max_tokens=8000, tools=tools or [WEB_SEARCH_TOOL],
            output_config={"effort": AGENT_EFFORT}, messages=messages,
        ) as stream:
            async for event in stream:
                if event.type != "content_block_stop":
                    continue
                block = getattr(event, "content_block", None)
                if getattr(block, "type", "") != "server_tool_use":
                    continue
                tool_input = getattr(block, "input", None)
                tool_input = tool_input if isinstance(tool_input, dict) else {}
                if block.name == "web_search" and tool_input.get("query"):
                    searches.append(tool_input["query"])
                    log(run_id, "web_search", name, {"query": tool_input["query"]})
                else:
                    log(run_id, "tool_call", name, {"tool": block.name, "input": json.dumps(tool_input)[:1500]})
            resp = await stream.get_final_message()
        if resp.stop_reason == "refusal":
            raise RuntimeError("the model declined this request")
        if resp.stop_reason != "pause_turn":
            return text_of(resp), searches
        messages.append({"role": "assistant", "content": resp.content})
    raise RuntimeError("search did not finish")


async def buying_agent(run_id: str, name: str, strategy: str, request: str, learned: list[str]) -> dict:
    """A real buying agent: live web search, one real product, real link."""
    log(run_id, "agent_started", name, {"strategy": strategy})
    started = time.monotonic()
    prompt = f"""You are buying agent {name}. Your strategy: {strategy}

Shopping request: {request}
Lessons from this shopper's past rejections (respect them): {json.dumps(learned) if learned else "none yet"}

Use web search to find ONE real product that is for sale right now and matches the request.
Reply with only a JSON object, no other text:
{{"title": str, "price_usd": number, "seller": str, "url": str,
  "delivery": str, "variant": str, "why": str}}
"variant" is the exact size, color, or model you picked. "why" is one sentence.
Never invent a product, price, or link. If you find nothing, return {{"error": "reason"}}."""
    searches: list[str] = []
    try:
        text, searches = await search_and_answer(run_id, name, prompt)
        bid = extract_json(text)
    except Exception as exc:
        bid = {"error": f"agent failed: {exc}"}
    bid.update(agent=name, strategy=strategy, searches=searches,
               duration_ms=round((time.monotonic() - started) * 1000))
    log(run_id, "bid_submitted", name, bid)
    return bid


async def judge(request: str, rules: str, bids: list[dict], board: list[dict], learned: list[str]) -> dict:
    prompt = f"""You are Boss. You work only for the shopper. Three buying agents sent bids.
Pick the bid the shopper would pick, and reject any bid that breaks a rule or a lesson,
misses the request (wrong size, wrong item, over budget), or has an error.

Request: {request}
Shopper rules: {rules or "none given"}
Lessons learned from past rejections by this shopper: {json.dumps(learned) if learned else "none yet"}
Agent track record so far (trust is a percentage): {json.dumps(board)}
Bids: {json.dumps(bids)}

Reply with only a JSON object:
{{"winner": "A" | "B" | "C" | null,
  "verdicts": [{{"agent": str, "decision": "win" | "runner_up" | "reject",
                "score": 0-100, "reason": str}}],
  "summary": str}}
Each reason is one plain sentence citing the specific price, date, rule, lesson, or mismatch.
When two bids are close, prefer the agent with the better track record and say so.
If no bid is acceptable, set winner to null and say what to ask the shopper."""
    resp = await llm().messages.create(
        model=MODEL, max_tokens=8000, output_config={"effort": JUDGE_EFFORT},
        messages=[{"role": "user", "content": prompt}],
    )
    if resp.stop_reason == "refusal":
        raise RuntimeError("the model declined to judge this request")
    return extract_json(text_of(resp))


async def clarify(request: str, rules: str) -> dict:
    """Before any agent runs: is the request specific enough to buy the right thing without guessing?"""
    prompt = f"""You are Boss. You manage buying agents for one shopper. Before sending the agents out,
decide whether this request is specific enough to buy the right thing without guessing.

Request: {request}
Shopper rules: {rules or "none given"}

Ask only when a missing detail would change what gets bought, such as quantity, size, or which kind
of item is meant. Do not ask about things the agents can reasonably choose, such as brand or color.
Reply with only a JSON object: {{"clear": true | false, "question": str}}
"question" is one short question for the shopper, and an empty string when the request is clear."""
    resp = await llm().messages.create(
        model=MODEL, max_tokens=2000, output_config={"effort": "low"},
        messages=[{"role": "user", "content": prompt}],
    )
    if resp.stop_reason == "refusal":
        raise RuntimeError("the model declined this request")
    return extract_json(text_of(resp))


async def verify_bid(run_id: str, bid: dict) -> dict:
    """Right before the shopper pays: read the live product page again for price and stock."""
    prompt = f"""You are Boss, re-checking a product right before the shopper pays.

Product page: {bid.get("url")}
Expected: {bid.get("title")} ({bid.get("variant") or "no variant given"}) from {bid.get("seller")} at ${bid.get("price_usd")}.

Fetch the page and report what it says right now for that exact variant. Search the web only if the
page cannot be read. Reply with only a JSON object:
{{"available": true | false | null, "current_price_usd": number | null, "evidence": str}}
Use null when you cannot tell. Never guess. "evidence" is one sentence on what you saw."""
    text, _ = await search_and_answer(run_id, "boss", prompt, tools=[WEB_FETCH_TOOL, WEB_SEARCH_TOOL])
    return extract_json(text)


def checkout_decision(bid: dict, check: dict) -> dict:
    """The rule Boss applies to a fresh price and stock check. No model call, so it can be drilled."""
    if check.get("available") is False:
        return {"action": "out_of_stock"}
    was, now = _num(bid.get("price_usd")), _num(check.get("current_price_usd"))
    if was is None or now is None:
        return {"action": "unverified"}
    if now - was >= PRICE_JUMP_USD:
        return {"action": "price_changed", "was": was, "now": now}
    return {"action": "ok", "now": now}


def current_pick(trail: list[dict]) -> tuple[str | None, dict | None]:
    """The agent Boss currently backs for this run (the latest decision) and that agent's bid."""
    winner = next((e["payload"].get("winner") for e in reversed(trail) if e["step"] == "decision_made"), None)
    bid = next((e["payload"] for e in trail if e["step"] == "bid_submitted" and e["actor"] == winner), None)
    return winner, bid


def runner_up(trail: list[dict]) -> str | None:
    """Best runner-up that is still in stock and has a real link."""
    gone = {e["actor"] for e in trail if e["step"] == "out_of_stock"}
    bids = {e["actor"]: e["payload"] for e in trail if e["step"] == "bid_submitted"}
    options = [e for e in trail if e["step"] == "bid_runner_up" and e["actor"] not in gone
               and bids.get(e["actor"], {}).get("url") and not bids[e["actor"]].get("error")]
    options.sort(key=lambda e: _num(e["payload"].get("score")) or 0, reverse=True)
    return options[0]["actor"] if options else None


# ---------- API ----------

class ShopRequest(BaseModel):
    request: str
    rules: str = ""
    run_id: str | None = None  # the page sends one so it can poll the trace while agents work


class Approval(BaseModel):
    confirm: bool = False  # the shopper has seen Boss's warning and still wants this pick


class Feedback(BaseModel):
    agent: str
    reason: str


@app.get("/")
def home():
    return FileResponse(os.path.join(os.path.dirname(__file__), "index.html"))


@app.post("/shop")
async def shop(body: ShopRequest):
    run_id = body.run_id if body.run_id and re.fullmatch(r"[a-z0-9]{6,32}", body.run_id) else uuid.uuid4().hex[:12]
    log(run_id, "request_received", "user", {"request": body.request, "rules": body.rules})
    await track("request_received", {"run_id": run_id})

    board, learned = scoreboard(), lessons()
    log(run_id, "context_loaded", "boss", {"scoreboard": board, "lessons": learned})

    if ASK_WHEN_VAGUE:
        try:
            check = await clarify(body.request, body.rules)
        except Exception as exc:  # a failed pre-check must not block the run; record it and carry on
            check = {"clear": True}
            log(run_id, "clarify_failed", "boss", {"error": str(exc)})
        if check.get("clear") is False and check.get("question"):
            log(run_id, "clarification_needed", "boss", {"question": check["question"]})
            await track("clarification_needed", {"run_id": run_id})
            return {"run_id": run_id, "needs_clarification": check["question"], "bids": [], "decision": None,
                    "scoreboard": board, "lessons": learned}

    bids = await asyncio.gather(*(buying_agent(run_id, n, s, body.request, learned) for n, s in AGENTS.items()))

    log(run_id, "judging_started", "boss", {"bids": len(bids)})
    try:
        decision = await judge(body.request, body.rules, list(bids), board, learned)
    except Exception as exc:
        log(run_id, "judge_failed", "boss", {"error": str(exc)})
        raise HTTPException(502, f"Judge failed: {exc}")

    for v in decision.get("verdicts", []):
        step = {"win": "bid_won", "reject": "bid_rejected"}.get(v.get("decision"), "bid_runner_up")
        log(run_id, step, v.get("agent", "?"), v)
        await track(step, {"run_id": run_id, "agent": v.get("agent"), "score": v.get("score")})
    log(run_id, "decision_made", "boss", {"winner": decision.get("winner"), "summary": decision.get("summary")})

    return {"run_id": run_id, "bids": bids, "decision": decision, "scoreboard": scoreboard(), "lessons": lessons()}


@app.post("/approve/{run_id}")
async def approve(run_id: str, body: Approval | None = None):
    """Approve the current pick. Boss re-checks the live page first and stops if anything changed."""
    confirm = bool(body and body.confirm)
    trail = events(run_id=run_id)
    winner, bid = current_pick(trail)
    if not bid or not bid.get("url"):
        raise HTTPException(404, "No winning bid to approve for this run")

    if VERIFY_CHECKOUT and not confirm:
        try:
            check = await verify_bid(run_id, bid)
        except Exception as exc:
            check = {"available": None, "current_price_usd": None, "error": str(exc)}
        log(run_id, "checkout_check", "boss", {"agent": winner, "bid_price_usd": bid.get("price_usd"), **check})
        verdict = checkout_decision(bid, check)

        if verdict["action"] == "out_of_stock":
            log(run_id, "out_of_stock", winner, {"evidence": check.get("evidence")})
            backup = runner_up(events(run_id=run_id))
            if backup:
                message = f"Agent {winner}'s pick is out of stock. Boss fell back to runner-up Agent {backup}. Review it and approve again."
                log(run_id, "fallback_to_runner_up", "boss", {"from": winner, "to": backup})
            else:
                message = f"Agent {winner}'s pick is out of stock and there is no acceptable runner-up. Send the agents again."
            log(run_id, "decision_made", "boss", {"winner": backup, "summary": message, "fallback": True})
            await track("out_of_stock", {"run_id": run_id, "agent": winner, "fallback": backup})
            return {"needs_confirmation": True, "can_confirm": False, "message": message}

        if verdict["action"] == "price_changed":
            message = (f"The price rose from ${verdict['was']:.2f} to ${verdict['now']:.2f} since Agent {winner} bid. "
                       "Boss stopped. Approve at the new price?")
            log(run_id, "price_changed", "boss", {"agent": winner, "was": verdict["was"], "now": verdict["now"]})
            await track("price_changed", {"run_id": run_id, "agent": winner})
            return {"needs_confirmation": True, "can_confirm": True, "message": message}

        if verdict["action"] == "unverified":
            message = "Boss could not confirm the current price and stock on the product page. Open it anyway?"
            log(run_id, "checkout_unverified", "boss", {"agent": winner, "reason": check.get("error") or check.get("evidence")})
            return {"needs_confirmation": True, "can_confirm": True, "message": message}

    log(run_id, "order_approved", "user", {"agent": winner, "url": bid["url"], "price_usd": bid.get("price_usd"),
                                           "confirmed_after_warning": confirm})
    await track("order_approved", {"run_id": run_id, "agent": winner, "price_usd": bid.get("price_usd")})
    return {"url": bid["url"]}


@app.post("/feedback/{run_id}")
async def feedback(run_id: str, body: Feedback):
    """The shopper overrules Boss. The reason becomes a lesson used in every future run."""
    if body.agent not in AGENTS or not body.reason.strip():
        raise HTTPException(400, "Need a valid agent and a reason")
    trail = events(run_id=run_id)
    request = next((e["payload"].get("request") for e in trail if e["step"] == "request_received"), "")
    bid = next((e["payload"] for e in trail if e["step"] == "bid_submitted" and e["actor"] == body.agent), {})
    lesson = (f"For a request like \"{request}\", the shopper rejected \"{bid.get('title', 'a pick')}\" "
              f"from {bid.get('seller', 'a seller')}: {body.reason.strip()}")
    log(run_id, "user_rejected", body.agent, {"reason": body.reason.strip()})
    log(run_id, "lesson_learned", "boss", {"lesson": lesson})
    await track("user_rejected", {"run_id": run_id, "agent": body.agent})
    return {"lesson": lesson, "lessons": lessons(), "scoreboard": scoreboard()}


@app.get("/trail/{run_id}")
def get_trail(run_id: str):
    return events(run_id=run_id)


@app.get("/runs")
def get_runs():
    return [{"run_id": e["run_id"], "request": e["payload"].get("request"), "created_at": e["created_at"]}
            for e in events(step="request_received", limit=20, newest_first=True)]


@app.get("/analytics")
def get_analytics():
    return analytics()


@app.get("/state")
def get_state():
    return {"scoreboard": scoreboard(), "lessons": lessons(), "agents": AGENTS}
