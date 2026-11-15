"""Static Laravel package-aware extractor (payment SDKs and notable deps).

Answers "which gateway is wired?", "is Stripe installed?" with **honest
absence**: when the project declares no payment dependency, that fact is
reported explicitly alongside the list of SDKs that were searched for — the
model can then answer "no payment gateway in this project" instead of
hallucinating one.

Sources: composer.json require + require-dev; resolved versions from
composer.lock (packages[].name => version).
"""

from __future__ import annotations

import json
import re
from typing import Any

# (composer package substring, friendly gateway name)
_PAYMENT_SDKS: list[tuple[str, str]] = [
    ("stripe/stripe-php", "Stripe"),
    ("mollie/mollie-api-php", "Mollie"),
    ("paypal/rest-api-sdk-php", "PayPal REST"),
    ("paypal/paypal-checkout-sdk", "PayPal Checkout"),
    ("braintree/braintree_php", "Braintree"),
    ("razorpay/razorpay", "Razorpay"),
    ("paystack/paystack", "Paystack"),
    ("flutterwave-v3/flutterwave-php-v3", "Flutterwave"),
    ("flutterwave/flutterwave-v3", "Flutterwave"),
    ("square/square", "Square"),
    ("adyen/adyen-php-api-library", "Adyen"),
    ("authorizenet/authorizenet", "Authorize.Net"),
    ("paddlehq/paddle", "Paddle"),
    ("recurly/recurly-client", "Recurly"),
]

_NOTABLE_DEP_PATTERN = re.compile(
    r"(laravel/(framework|horizon|nova|spark|cashier|sanctum|passport|scout|telescope)"
    r"|spatie/laravel-[\w-]+|barryvdh/laravel-[\w-]+|guzzlehttp/guzzle|"
    r"predis/predis|php-amqplib/php-amqplib|nunomaduro/collision|"
    r"laravel/pint|laravel/tinker|phpunit/phpunit|mockery/mockery|"
    r"fakerphp/faker|laravel/sail|laravel/pail|nikic/php-parser|"
    r"monolog/monolog|vlucas/phpdotenv|symfony/[\w-]+|league/flysystem-[\w-]+)"
)

_VERSION_RE = re.compile(r"\^?v?([\d.]+)")


def _require_sections(composer_json: str) -> dict[str, str]:
    """name => constraint from require + require-dev blocks of composer.json."""
    out: dict[str, str] = {}
    try:
        data = json.loads(composer_json)
    except (json.JSONDecodeError, TypeError):
        return out
    for section in ("require", "require-dev"):
        for name, constraint in (data.get(section) or {}).items():
            if isinstance(name, str) and isinstance(constraint, str):
                out[name] = constraint
    return out


def _locked_versions(composer_lock: str) -> dict[str, str]:
    """package name => resolved version from composer.lock (best effort)."""
    out: dict[str, str] = {}
    try:
        data = json.loads(composer_lock)
    except (json.JSONDecodeError, TypeError):
        return out
    for pkg in data.get("packages", []):
        name = pkg.get("name")
        version = pkg.get("version")
        if name and version:
            out[name] = version.lstrip("v")
    return out


def extract_packages(
    composer_json: str, composer_lock: str = ""
) -> dict[str, Any]:
    """Detect payment SDKs + notable deps with resolved versions."""
    req = _require_sections(composer_json or "{}")
    locked = _locked_versions(composer_lock or "{}") if composer_lock else {}

    present: list[dict[str, Any]] = []
    absent: list[str] = []
    for substring, gateway in _PAYMENT_SDKS:
        match = next((n for n in req if substring in n), None)
        if match:
            version = None
            vm = _VERSION_RE.search(req[match])
            if vm:
                version = vm.group(1)
            present.append(
                {
                    "gateway": gateway,
                    "package": match,
                    "required": req[match],
                    "version": locked.get(match) or version,
                }
            )
        else:
            absent.append(gateway)

    notable = sorted(
        ({"package": n, "required": c} for n, c in req.items() if _NOTABLE_DEP_PATTERN.match(n)),
        key=lambda d: d["package"],
    )

    return {
        "payment_gateways": present,
        "payment_absent": absent,  # searched but not found (honest absence)
        "notable_dependencies": notable,
    }
