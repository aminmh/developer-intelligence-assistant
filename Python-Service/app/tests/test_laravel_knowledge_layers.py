"""Tests for the Laravel wiring/behaviors/frontend/errors/packages extractors."""

from __future__ import annotations

from agent.plugins.frameworks.laravel.wiring import (
    extract_wiring,
    parse_bindings,
    parse_middleware,
    parse_providers,
)

APP_SERVICE_PROVIDER = """<?php
namespace App\\Providers;
use Illuminate\\Support\\ServiceProvider;
class AppServiceProvider extends ServiceProvider
{
    public function register(): void
    {
        $this->app->bind(PaymentService::class, function ($app) {
            return new StripePaymentService();
        });
        $this->app->singleton('payments.gateway', StripePaymentService::class);
    }
    public function boot(): void
    {
        //
    }
}
"""

EMPTY_PROVIDER = """<?php
namespace App\\Providers;
use Illuminate\\Support\\ServiceProvider;
class MetricsServiceProvider extends ServiceProvider
{
    public function register(): void
    {
        //
    }
    public function boot(): void
    {
        //
    }
}
"""

BOOTSTRAP_PROVIDERS = """<?php
use App\\Providers\\AppServiceProvider;
use App\\Providers\\MetricsServiceProvider;
return [
    AppServiceProvider::class,
    MetricsServiceProvider::class,
];
"""

BOOTSTRAP_APP = """<?php
use Illuminate\\Foundation\\Application;
use Illuminate\\Foundation\\Configuration\\Exceptions;
use Illuminate\\Foundation\\Configuration\\Middleware;
return Application::configure(basePath: dirname(__DIR__))
    ->withRouting(
        web: __DIR__.'/../routes/web.php',
        commands: __DIR__.'/../routes/console.php',
        health: '/up',
    )
    ->withMiddleware(function (Middleware $middleware) {
        $middleware->alias([
            'auth' => \\App\\Http\\Middleware\\Authenticate::class,
            'admin' => \\App\\Http\\Middleware\\AdminOnly::class,
        ]);
        $middleware->group('web', [
            \\Illuminate\\Cookie\\Middleware\\EncryptCookies::class,
        ]);
        $middleware->append(\\App\\Http\\Middleware\\ForceHttps::class);
    })
    ->withExceptions(function (Exceptions $exceptions) {
        $exceptions->dontReport(\\Illuminate\\Auth\\AuthenticationException::class);
    })
    ->create();
"""

LEGACY_KERNEL = """<?php
namespace App\\Http;
class Kernel extends \\Illuminate\\Foundation\\Http\\Kernel
{
    protected $middlewareAliases = [
        'auth' => \\App\\Http\\Middleware\\Authenticate::class,
    ];
    protected $middlewareGroups = [
        'web' => [\\App\\Http\\Middleware\\EncryptCookies::class],
        'api' => ['throttle:api', \\Illuminate\\Routing\\Middleware\\SubstituteBindings::class],
    ];
}
"""


class TestParseProviders:
    def test_bootstrap_registration_marks_provider(self):
        res = parse_providers(
            [("AppServiceProvider.php", APP_SERVICE_PROVIDER)],
            bootstrap_src=BOOTSTRAP_PROVIDERS,
        )
        assert res["provider_count"] == 1
        assert res["registration_site"] == "bootstrap/providers.php"
        assert res["providers"][0]["registered"] is True

    def test_unregistered_provider_honest(self):
        res = parse_providers(
            [("AppServiceProvider.php", APP_SERVICE_PROVIDER)],
            bootstrap_src="return [ OtherProvider::class ];",
        )
        assert res["providers"][0]["registered"] is False

    def test_register_body_activity_detected(self):
        res = parse_providers(
            [("AppServiceProvider.php", APP_SERVICE_PROVIDER)],
            bootstrap_src=BOOTSTRAP_PROVIDERS,
        )
        p = res["providers"][0]
        assert p["register_has_code"] is True
        assert p["boot_has_code"] is False  # empty boot w/ comment only

    def test_legacy_config_app_providers(self):
        legacy = """return [
    'providers' => [
        App\\Providers\\AppServiceProvider::class,
    ],
];"""
        res = parse_providers(
            [("AppServiceProvider.php", APP_SERVICE_PROVIDER)],
            app_config_src=legacy,
        )
        assert res["registration_site"] == "config/app.php"
        assert res["providers"][0]["registered"] is True


class TestParseBindings:
    def test_bind_and_singleton_found(self):
        out = parse_bindings([("app/Providers/AppServiceProvider.php", APP_SERVICE_PROVIDER)])
        types = {(b["type"], b["abstract"]) for b in out}
        assert ("bind", "PaymentService") in types
        assert ("singleton", "payments.gateway") in types
        assert out[0]["concrete"] == "StripePaymentService"

    def test_no_bindings_empty(self):
        assert parse_bindings([]) == []


class TestParseMiddleware:
    def test_bootstrap_aliases_groups_global(self):
        mw = parse_middleware(BOOTSTRAP_APP)
        assert mw["source"] == "bootstrap/app.php"
        assert mw["aliases"]["auth"] == "App\\Http\\Middleware\\Authenticate"
        assert "web" in mw["groups"]
        assert "ForceHttps" in " ".join(mw["global"])

    def test_legacy_kernel_aliases(self):
        mw = parse_middleware("", kernel_src=LEGACY_KERNEL)
        assert mw["source"] == "app/Http/Kernel.php"
        assert mw["aliases"]["auth"].endswith("Authenticate")
        assert set(mw["groups"]) == {"web", "api"}

    def test_no_middleware(self):
        mw = parse_middleware("", kernel_src="")
        assert mw["source"] is None
        assert not mw["aliases"]


class TestExtractWiring:
    def test_full_aggregation(self):
        files = [
            ("app/Providers/AppServiceProvider.php", APP_SERVICE_PROVIDER),
            ("app/Http/Kernel.php", LEGACY_KERNEL),
            ("app/Services/PaymentService.php", "<?php class PaymentService {}"),
        ]
        res = extract_wiring(files, bootstrap_providers_src=BOOTSTRAP_PROVIDERS)
        assert res["provider_count"] == 1
        assert res["bindings"]
        assert res["middleware"]["aliases"]


# --- behaviors ---------------------------------------------------------------

from agent.plugins.frameworks.laravel.behaviors import extract_behaviors

JOB = """<?php
namespace App\\Jobs;
use Illuminate\\Contracts\\Queue\\ShouldQueue;
class ProcessPayment implements ShouldQueue
{
    public function handle(): void {}
}
"""

PLAIN_JOB = """<?php
namespace App\\Jobs;
class SyncReport
{
    public function handle(): void {}
}
"""

EVENT = """<?php
namespace App\\Events;
class PaymentSucceeded {}
"""

LISTENER = """<?php
namespace App\\Listeners;
class SendReceipt
{
    public function handle(PaymentSucceeded $event): void {}
}
"""

CONSOLE = """<?php
use Illuminate\\Support\\Facades\\Schedule;
Schedule::command('inspire')->hourly();
Schedule::call(function () { report('ping'); })->everyMinute();
"""

DISPATCHER = """<?php
namespace App\\Services;
class BookingService
{
    public function book(): void {
        dispatch(new ProcessPayment($order));
        event(new PaymentSucceeded($order));
    }
}
"""


class TestBehaviors:
    def test_jobs_queueable_flag(self):
        res = extract_behaviors(
            [("app/Jobs/ProcessPayment.php", JOB), ("app/Jobs/SyncReport.php", PLAIN_JOB)]
        )
        by = {j["name"]: j for j in res["jobs"]}
        assert by["ProcessPayment"]["queueable"] is True
        assert by["SyncReport"]["queueable"] is False

    def test_listeners_record_handled_event(self):
        res = extract_behaviors(
            [("app/Listeners/SendReceipt.php", LISTENER)],
            console_routes_src="",
        )
        assert res["listeners"][0]["handles"] == "PaymentSucceeded"

    def test_schedule_parsed(self):
        res = extract_behaviors([], console_routes_src=CONSOLE)
        kinds = {(s["task"], s["frequency"]) for s in res["schedule"]}
        assert ("inspire", "hourly") in kinds
        assert len(res["schedule"]) == 2

    def test_dispatch_and_event_sites(self):
        res = extract_behaviors([("app/Services/BookingService.php", DISPATCHER)])
        assert res["dispatch_sites"].get("ProcessPayment") == 1
        assert res["event_sites"].get("PaymentSucceeded") == 1

    def test_absent_dirs_are_empty_not_error(self):
        res = extract_behaviors([])
        assert res["jobs"] == []
        assert res["events"] == []
        assert res["listeners"] == []
        assert res["schedule"] == []


# --- frontend ----------------------------------------------------------------

from agent.plugins.frameworks.laravel.frontend import extract_frontend

CONTROLLER_AUTH = """<?php
namespace App\\Http\\Controllers;
class AuthenticationController
{
    public function showLoginForm() { return view('auth.login'); }
    public function register() { return view('auth.register'); }
}
"""

WEB_ROUTES = """<?php
Route::view('/welcome-lite', 'welcome');
Route::get('/dashboard', [DashboardController::class, 'home']);
Route::get('/', function () { return view('welcome'); });
"""


class TestFrontend:
    def test_view_inventory_dotted(self):
        files = [
            ("resources/views/auth/login.blade.php", "<form>login</form>"),
            ("resources/views/dashboard/home.blade.php", "<h1>Home</h1>"),
        ]
        res = extract_frontend(files)
        names = {v["name"] for v in res["views"]}
        assert names == {"auth.login", "dashboard.home"}

    def test_components_blade_and_class(self):
        files = [
            ("resources/views/components/alert.blade.php", "<div></div>"),
            ("app/View/Components/FlashMessage.php", "<?php class FlashMessage {}"),
        ]
        res = extract_frontend(files)
        kinds = {(c["name"], c["kind"]) for c in res["components"]}
        assert kinds == {("alert", "blade"), ("FlashMessage", "class")}

    def test_controller_and_route_renderers(self):
        files = [
            ("app/Http/Controllers/AuthenticationController.php", CONTROLLER_AUTH),
        ]
        res = extract_frontend(files, routes_files=[("web.php", WEB_ROUTES)])
        assert res["controller_view_links"][0]["views"] == ["auth.login", "auth.register"]
        uris = {l["uri"] for l in res["route_view_links"]}
        assert "/welcome-lite" in uris  # Route::view
        assert "/" in uris  # closure view()
        assert "welcome" in {l["view"] for l in res["route_view_links"]}

    def test_no_views_honest(self):
        res = extract_frontend([("app/Models/User.php", "<?php class User {}")])
        assert res["views"] == []
        assert res["components"] == []


# --- errors ------------------------------------------------------------------

from agent.plugins.frameworks.laravel.errors import extract_errors

CUSTOM_EXC = """<?php
namespace App\\Exceptions;
class PaymentFailedException extends \\Exception
{
    public function render($request) { return response('nope', 402); }
}
"""

THROWY = """<?php
namespace App\\Services;
class PaymentService
{
    public function charge() {
        try {
            throw new PaymentFailedException('declined');
        } catch (\\Exception $e) {
            throw new \\RuntimeException('wrap', 0, $e);
        }
    }
}
"""


class TestErrors:
    def test_custom_exception_captured(self):
        res = extract_errors([("app/Exceptions/PaymentFailedException.php", CUSTOM_EXC)])
        assert res["custom_exceptions"][0]["name"] == "PaymentFailedException"
        assert res["custom_exceptions"][0]["renders"] is True

    def test_throw_and_catch_hotspots(self):
        res = extract_errors(
            [
                ("app/Exceptions/PaymentFailedException.php", CUSTOM_EXC),
                ("app/Services/PaymentService.php", THROWY),
            ]
        )
        hotspot = next(h for h in res["throw_hotspots"] if "PaymentService" in h["file"])
        assert hotspot["throws"] == 2
        catches = next(h for h in res["catch_hotspots"] if "PaymentService" in h["file"])
        assert catches["catches"] == 1

    def test_handler_configured_via_bootstrap(self):
        res = extract_errors([], bootstrap_app_src=BOOTSTRAP_APP)
        assert res["handler_configured"] is True

    def test_no_throws_honest(self):
        res = extract_errors([("app/Models/User.php", "<?php class User {}")])
        assert res["custom_exceptions"] == []
        assert res["throw_hotspots"] == []


# --- packages ----------------------------------------------------------------

from agent.plugins.frameworks.laravel.packages import extract_packages

COMPOSER_NO_PAY = """{
    "require": {
        "php": "^8.2",
        "laravel/framework": "^12.0",
        "guzzlehttp/guzzle": "^7.9"
    },
    "require-dev": { "phpunit/phpunit": "^11.5" }
}
"""

COMPOSER_STRIPE = """{
    "require": {
        "laravel/framework": "^11.0",
        "stripe/stripe-php": "^13.0",
        "mollie/mollie-api-php": "^2.65"
    }
}
"""

LOCK_STRIPE = """{
    "packages": [
        {"name": "stripe/stripe-php", "version": "v13.9.0"},
        {"name": "mollie/mollie-api-php", "version": "v2.70.0"}
    ]
}
"""


class TestPackages:
    def test_honest_absence(self):
        res = extract_packages(COMPOSER_NO_PAY)
        assert res["payment_gateways"] == []
        assert "Stripe" in res["payment_absent"]
        assert "Mollie" in res["payment_absent"]

    def test_gateways_detected_with_resolved_versions(self):
        res = extract_packages(COMPOSER_STRIPE, LOCK_STRIPE)
        gateways = {g["gateway"]: g for g in res["payment_gateways"]}
        assert set(gateways) == {"Stripe", "Mollie"}
        assert gateways["Stripe"]["version"] == "13.9.0"
        assert gateways["Mollie"]["version"] == "2.70.0"

    def test_gateway_version_falls_back_to_constraint(self):
        res = extract_packages(COMPOSER_STRIPE, "")
        gateways = {g["gateway"]: g for g in res["payment_gateways"]}
        assert gateways["Stripe"]["version"] == "13.0"

    def test_notable_dependencies(self):
        res = extract_packages(COMPOSER_NO_PAY)
        pkgs = {d["package"] for d in res["notable_dependencies"]}
        assert "laravel/framework" in pkgs
        assert "phpunit/phpunit" in pkgs

    def test_bad_json_degrades(self):
        res = extract_packages("not json at all")
        assert res["payment_gateways"] == []
        assert "Stripe" in res["payment_absent"]

