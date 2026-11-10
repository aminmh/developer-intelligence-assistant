"""Before/after evaluation of agent code tools on armaiti-api-develop.

Run inside the python-api container (it uses the live settings and model):

    docker cp scripts/agent_eval.py python_api:/tmp/agent_eval.py
    docker exec -w /app -e PYTHONPATH=/app python_api python /tmp/agent_eval.py /tmp/agent_eval.jsonl

EVAL_PROJECT=pizza|armaiti picks the question set; EVAL_ONLY=2,5 limits the questions;
EVAL_CONDITIONS=tools runs one condition. EVAL_EMBEDDINGS=openrouter embeds queries with
OpenRouter's baai/bge-m3 (key in /tmp/openrouter.key) instead of Cloudflare.
ROOT must point at an extracted copy of the project the questions are about.

Runs each question through the real AnswerService twice — tools on, tools off —
with the same retrieval and model, and scores the answers deterministically:
required facts present (checked against the source by hand), citations that
point at real lines, time, tokens, and tool steps. Results are appended to a
JSON-lines file after every answer so a quota failure keeps partial results.
"""
import json
import os
import re
import sys
import time

sys.path.insert(0, "/app")
PROJECT = os.environ.get("EVAL_PROJECT", "armaiti")
ROOT = {
    "armaiti": "/app/workspaces/ad49b929-3547-4a83-804d-0e6b9457bc19/armaiti-api-develop",
    # Revision cache extracted by agent_tools.source for project "pizza" at this commit.
    "pizza": "/tmp/devassistant-code-tools/96721091694a1c8ba52748da5697a9f2495d1f77d6c1b173f0cd41998bb364cc",
}[PROJECT]
# Request fields per project. pizza is an indexed revision: tools read its
# archive (project_id + commit_sha) and retrieval uses its pinned collection,
# as the app's job path does; armaiti is a legacy upload with no vector index.
REQUEST = {
    "armaiti": {"filename": "armaiti-api-develop.zip"},
    "pizza": {"project_id": "pizza", "commit_sha": "1670d9ecc83f95d2729e4857b355b78966c40429",
              "collection": "snapshot_5d8deaed8a649f4e450221503ee692547f48e6b6a488d9b6d525f19e2e2e342c"},
}[PROJECT]
OUT = sys.argv[1] if len(sys.argv) > 1 else "/tmp/agent_eval.jsonl"

# Each fact is a group of acceptable spellings; a group counts if any spelling appears.
ARMAITI_QUESTIONS = [
    ("Where are the login routes defined, and which middleware protects the admin routes?",
     [["auth.php"], ["landlord.php"], ["tenant.resolve"], ["admin.status"], ["role:admin"]]),
    ("How does the API decide which tenant a request belongs to?",
     [["TenantResolver"], ["subdomain", "sub-domain", "sub domain", "getSubDomain"], ["host"],
      ["500", "HTTP_INTERNAL_SERVER_ERROR", "internal server error"]]),
    ("Which scheduled jobs does the application run, and when do they run?",
     [["weekly-inactive"], ["weekly-no-appreciation"], ["monday"], ["thursday"], ["10:00"]]),
    ("How does the API force mobile clients to update the app?",
     [["VersionChecker"], ["x-app-version"], ["must_update"], ["version_compare", "older", "lower", "less than"],
      ["AccessDenied", "403"]]),
    ("How is a user's wallet balance changed? Which events and listeners are involved?",
     [["WalletCredited"], ["WalletDebited"], ["IncreaseWalletBalance"], ["DecreaseWalletBalance"],
      ["EventServiceProvider"]]),
    ("Who is allowed to create, update, or delete appreciations according to the authorization policy?",
     [["AppreciationPolicy"], ["admin"]]),
    ("Which Eloquent model observers exist and where are they registered?",
     [["AppreciationObserver"], ["UserObserver"], ["WalletTransactionObserver"], ["static::observe", "boot"]]),
    ("How are SMS verification codes sent, and when do they expire?",
     [["SmsSenderListener"], ["SmsProviderInterface"], ["expires_at"], ["verification.expires"],
      ["ShouldQueue", "queue"]]),
    ("Which listeners or jobs run on the queue instead of synchronously?",
     [["MarkAllAppreciationsAsSeen"], ["MarketPurchaseListener"], ["ReceiveAppreciationListener"],
      ["SmsSenderListener"], ["MinerCollectionListener"]]),
    ("What happens when a user opens the app each day - how is daily-visit XP awarded?",
     [["UserVisitedApp"], ["DailyVisitXpListener"]]),
]

PIZZA_QUESTIONS = [
    ("How is the price of a pizza calculated?",
     [["CalculatePizzaPriceAction"], ["quantity"], ["labor_fee", "labor fee"], ["getPriceForSize", "size"]]),
    ("Which order status transitions are allowed?",
     [["pending"], ["paid"], ["preparing"], ["completed"], ["cancelled"], ["canTransitionTo"]]),
    ("How is OTP request rate limiting enforced, and where do its limits come from?",
     [["CheckOtpRateLimitAction"], ["RateLimiter"], ["OtpRequestLimit"], ["OtpRequestWindowMinutes"], ["setting"]]),
    ("When is a pizza locked against editing?",
     [["CheckEditLockAction"], ["CartPizzaEditLockDays", "lock days", "lockDays"], ["cart"], ["owner"], ["active"]]),
    ("What happens when a payment is processed, and which events are dispatched?",
     [["ProcessPaymentAction"], ["PaymentSucceeded"], ["OrderPaid"], ["PaymentFailed"], ["pending"]]),
    ("According to the authorization policy, who can update, delete, and publish a pizza?",
     [["PizzaPolicy"], ["owner"], ["isEditable", "editable"], ["admin"]]),
    ("How does a guest cart become a signed-in user's cart?",
     [["ClaimGuestCartAction"], ["guest_token", "guest token"], ["merge"], ["99"]]),
    ("Which API routes require authentication, and what rate limits apply to them?",
     [["api.php"], ["throttle:60,1", "60 requests", "60 per minute"], ["throttle:120,1", "120 requests", "120 per minute"],
      ["role:admin"], ["request-otp"]]),
    ("Which actions write audit log entries?",
     [["AuditAction"], ["AuditLog"], ["PublishPizzaAction"], ["ChangeOrderStatusAction"], ["ProcessPaymentAction"]]),
    ("Can a failed payment be retried, and under what conditions?",
     [["RetryPaymentAction"], ["pending"], ["latest"]]),
]
QUESTIONS = PIZZA_QUESTIONS if PROJECT == "pizza" else ARMAITI_QUESTIONS

CITE = re.compile(r"((?:[\w@.-]+/)*[\w@.-]+\.[A-Za-z]{1,10}):(\d{1,6})")


def citations(answer):
    valid = invalid = 0
    for path, line in set(CITE.findall(answer)):
        candidates = [os.path.join(ROOT, path), os.path.join(os.path.dirname(ROOT), path)]
        found = next((c for c in candidates if os.path.isfile(c)), None)
        if found:
            with open(found, errors="replace") as fh:
                total = sum(1 for _ in fh)
            valid += 1 if 1 <= int(line) <= total else 0
            invalid += 0 if 1 <= int(line) <= total else 1
        else:
            invalid += 1
    return valid, invalid


def facts(answer, groups):
    text = answer.lower()
    hits = [any(s.lower() in text for s in group) for group in groups]
    return sum(hits), len(groups), [g[0] for g, h in zip(groups, hits) if not h]


def _embed_via_openrouter(texts, progress=None):
    """Query embeddings from OpenRouter's baai/bge-m3 (same model as Cloudflare's @cf/baai/bge-m3).

    For evaluation runs when the Cloudflare quota is used up; the key is read
    from /tmp/openrouter.key inside the container and never logged.
    """
    import urllib.request

    key = open("/tmp/openrouter.key").read().strip()
    vectors = []
    for offset in range(0, len(texts), 32):
        req = urllib.request.Request(
            "https://openrouter.ai/api/v1/embeddings",
            data=json.dumps({"model": "baai/bge-m3", "input": texts[offset:offset + 32]}).encode(),
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json", "User-Agent": "devassistant-eval"},
            method="POST")
        rows = json.loads(urllib.request.urlopen(req, timeout=60).read())["data"]
        vectors.extend(r["embedding"] for r in sorted(rows, key=lambda r: r["index"]))
    return vectors


def main():
    import agent_tools.runtime as runtime
    from main import _answer_service

    if os.environ.get("EVAL_EMBEDDINGS") == "openrouter":
        import index.embeddings as embeddings
        embeddings._embed_cloudflare = _embed_via_openrouter

    real_status = runtime.tool_status
    service = _answer_service()
    only = {int(x) for x in os.environ.get("EVAL_ONLY", "").split(",") if x.strip()}
    conditions = tuple(os.environ.get("EVAL_CONDITIONS", "tools,no_tools").split(","))
    for index, (question, groups) in enumerate(QUESTIONS, 1):
        if only and index not in only:
            continue
        for condition in conditions:
            if condition == "no_tools":
                runtime.tool_status = lambda route, use_remote: {"available": False, "code": "eval_off", "reason": "evaluation"}
            else:
                runtime.tool_status = real_status
            body = {"query": question, "route": "code_explain", "use_remote": True, **REQUEST}
            started = time.monotonic()
            try:
                result = service.answer(body)
                error = None
            except Exception as exc:  # noqa: BLE001 — record and continue
                result, error = {}, str(exc)[:300]
            answer = result.get("answer") or ""
            hit, total, missing = facts(answer, groups)
            valid, invalid = citations(answer)
            usage = result.get("usage") or {}
            row = {"q": index, "project": PROJECT, "condition": condition, "retrieved": len(result.get("matches") or []), "facts": hit, "fact_total": total, "missing": missing,
                   "citations_valid": valid, "citations_invalid": invalid,
                   "seconds": round(time.monotonic() - started, 1), "tokens": usage.get("total_tokens"),
                   "tokens_estimated": bool(usage.get("estimated")), "tool_calls": len(result.get("tool_calls") or []),
                   "provider": result.get("provider"), "error": error or (None if answer else "empty answer"),
                   "answer": answer}
            with open(OUT, "a") as fh:
                fh.write(json.dumps(row) + "\n")
            print(f"Q{index:>2} {condition:8} ctx {row['retrieved']} facts {hit}/{total} cites {valid}+/{invalid}- "
                  f"{row['seconds']:>5}s tok {row['tokens']} tools {row['tool_calls']} {row['error'] or ''}", flush=True)
    runtime.tool_status = real_status


if __name__ == "__main__":
    main()
