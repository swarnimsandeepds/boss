"""Drills: prove Boss cannot be fooled before it touches real money.

Run:  python drills.py

These are tests, not part of the product path. Each drill hands Boss a crafted situation and
checks what it does. Drills 1 and 3 exercise the checkout rules directly and need no API key.
Drills 2 and 4 call the real judge and the real pre-check, so they need ANTHROPIC_API_KEY.
"""
import asyncio
import sys

import main

SHOE = {"agent": "A", "title": "Trail Runner 5", "price_usd": 89.0, "seller": "Example Outfitters",
        "url": "https://example.com/trail-runner-5", "variant": "Men's size 10", "delivery": "Wednesday"}


def drill_price_jump() -> str | None:
    """Price jumps $30 between bid and checkout. Boss must stop and ask."""
    verdict = main.checkout_decision(SHOE, {"available": True, "current_price_usd": 119.0})
    return None if verdict["action"] == "price_changed" else f"expected price_changed, got {verdict}"


def drill_out_of_stock() -> str | None:
    """The cheapest seller goes out of stock. Boss must fall back to the runner-up."""
    verdict = main.checkout_decision(SHOE, {"available": False, "current_price_usd": 89.0})
    if verdict["action"] != "out_of_stock":
        return f"expected out_of_stock, got {verdict}"
    trail = [
        {"step": "bid_submitted", "actor": "A", "payload": SHOE},
        {"step": "bid_submitted", "actor": "B", "payload": {**SHOE, "agent": "B", "price_usd": 99.0}},
        {"step": "bid_won", "actor": "A", "payload": {"score": 90}},
        {"step": "bid_runner_up", "actor": "B", "payload": {"score": 80}},
        {"step": "out_of_stock", "actor": "A", "payload": {}},
    ]
    backup = main.runner_up(trail)
    return None if backup == "B" else f"expected fallback to B, got {backup}"


async def drill_wrong_size() -> str | None:
    """An agent bids the wrong size. Boss must reject it."""
    bids = [
        {**SHOE, "agent": "A", "price_usd": 69.0, "variant": "Men's size 8"},   # cheapest, wrong size
        {**SHOE, "agent": "B", "price_usd": 95.0},
        {**SHOE, "agent": "C", "price_usd": 105.0},
    ]
    decision = await main.judge("Men's running shoes, size 10, under $120", "", bids, main.scoreboard(), [])
    wrong = next((v for v in decision.get("verdicts", []) if v.get("agent") == "A"), {})
    if wrong.get("decision") != "reject":
        return f"expected A rejected for wrong size, got {wrong}"
    return None if decision.get("winner") in ("B", "C") else f"expected B or C to win, got {decision.get('winner')}"


async def drill_vague_request() -> str | None:
    """Vague request. Boss must ask for quantity instead of guessing."""
    check = await main.clarify("get me wings", "")
    return None if check.get("clear") is False and check.get("question") else f"expected a question, got {check}"


async def run() -> int:
    drills = [drill_price_jump, drill_out_of_stock, drill_wrong_size, drill_vague_request]
    failed = 0
    for drill in drills:
        try:
            problem = drill()
            if asyncio.iscoroutine(problem):
                problem = await problem
        except Exception as exc:
            problem = f"could not run: {exc}"
        failed += bool(problem)
        print(f"{'FAIL' if problem else 'PASS'}  {drill.__doc__.strip()}" + (f"\n      {problem}" if problem else ""))
    print(f"\n{len(drills) - failed} of {len(drills)} drills passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
