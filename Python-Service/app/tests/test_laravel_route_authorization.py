"""Laravel routes and guards from PHP facts (gap-closing plan, task 8).

The Laravel plugin reads the route DSL, middleware aliases and groups, controller
middleware and authorization calls from PHP facts; the framework-neutral
route-authorization check (the same one Symfony feeds) decides each route.
"""

import shutil
from pathlib import Path

import pytest

from agent import default_registry
from agent.core.capability import Capability
from agent.core.revision import RevisionContext
from code_quality import route_authorization


def _php_cli_available() -> bool:
    from infra.settings import settings

    return shutil.which("php") is not None and Path(settings.php_cli_root, "bin", "project-inspect").is_file()


pytestmark = pytest.mark.skipif(not _php_cli_available(), reason="PHP CLI tools are not installed here")

COMPOSER = '{"require": {"php": "^8.2", "laravel/framework": "^12.0"}}'

BOOTSTRAP = """<?php
use App\\Http\\Middleware\\RoleMiddleware;
use App\\Http\\Middleware\\TraceMiddleware;
use Illuminate\\Foundation\\Application;
use Illuminate\\Foundation\\Configuration\\Middleware;
use Illuminate\\Support\\Facades\\Route;

return Application::configure(basePath: dirname(__DIR__))
    ->withRouting(
        web: __DIR__.'/../routes/web.php',
        api: __DIR__.'/../routes/api.php',
        commands: __DIR__.'/../routes/console.php',
        health: '/up',
        then: function () {
            Route::middleware('api')->prefix('hooks')->group(base_path('routes/hooks.php'));
        },
    )
    ->withMiddleware(function (Middleware $middleware): void {
        $middleware->alias(['role' => RoleMiddleware::class, 'trace' => TraceMiddleware::class]);
        $middleware->group('staff', ['auth', 'role:staff']);
    })
    ->create();
"""

WEB = """<?php
use App\\Http\\Controllers\\HomeController;
use Illuminate\\Support\\Facades\\Route;

Route::get('/', [HomeController::class, 'index'])->name('home');
Route::middleware('auth')->group(function () {
    Route::get('/orders', [HomeController::class, 'orders']);
    require __DIR__.'/account.php';
});
Route::middleware('guest')->group(function () {
    Route::get('/login', [HomeController::class, 'login']);
});
Route::get('/traced', [HomeController::class, 'traced'])->middleware('trace');
"""

ACCOUNT = """<?php
use App\\Http\\Controllers\\HomeController;
use Illuminate\\Support\\Facades\\Route;

Route::get('/account', [HomeController::class, 'account']);
"""

API = """<?php
use App\\Http\\Controllers\\Api\\PostController;
use App\\Http\\Controllers\\Api\\ReportController;
use App\\Http\\Controllers\\Api\\StatsController;
use App\\Http\\Controllers\\Api\\TagController;
use Illuminate\\Support\\Facades\\Route;

Route::prefix('v1')->group(function () {
    Route::get('/posts', [PostController::class, 'index']);
    Route::post('/posts', [PostController::class, 'store'])->middleware('auth:sanctum');
    Route::match(['put', 'patch'], '/posts/{post}', [PostController::class, 'update'])->can('update', 'post');
    Route::get('/posts/{post}', [PostController::class, 'show']);
    Route::get('/stats', StatsController::class);
    Route::controller(ReportController::class)->group(function () {
        Route::get('/reports', 'index');
        Route::post('/reports', 'store');
        Route::delete('/reports/{report}', 'destroy');
    });
    Route::apiResource('tags', TagController::class)->only(['index', 'show']);
});

Route::prefix('v1/admin')->middleware(['auth:sanctum', 'role:admin'])->group(function () {
    Route::apiResource('posts.comments', PostController::class);
    Route::get('/open', [PostController::class, 'open'])->withoutMiddleware(['auth:sanctum', 'role:admin']);
});

Route::middleware('staff')->group(function () {
    Route::get('/desk', [PostController::class, 'desk']);
});
"""

HOOKS = """<?php
use App\\Http\\Controllers\\Api\\HookController;
use Illuminate\\Support\\Facades\\Route;

Route::post('/stripe', [HookController::class, 'stripe'])->middleware('signed');
"""

UNLOADED = """<?php
use Illuminate\\Support\\Facades\\Route;

Route::get('/forgotten', fn () => 'x');
"""

ROLE_MIDDLEWARE = """<?php
namespace App\\Http\\Middleware;

use Closure;
use Illuminate\\Http\\Request;

class RoleMiddleware
{
    public function handle(Request $request, Closure $next, string $role)
    {
        if ($request->user()?->role !== $role) {
            abort(403);
        }
        return $next($request);
    }
}
"""

TRACE_MIDDLEWARE = """<?php
namespace App\\Http\\Middleware;

use Closure;
use Illuminate\\Http\\Request;

class TraceMiddleware
{
    public function handle(Request $request, Closure $next)
    {
        logger()->info('trace');
        return $next($request);
    }
}
"""

HOME_CONTROLLER = """<?php
namespace App\\Http\\Controllers;

class HomeController
{
    public function index() {}
    public function orders() {}
    public function account() {}
    public function login() {}
    public function traced() {}
}
"""

POST_CONTROLLER = """<?php
namespace App\\Http\\Controllers\\Api;

class PostController
{
    public function index() {}
    public function store() {}
    public function update() {}
    public function show($post)
    {
        abort_if($post === null, 404);
        $this->authorize('view', $post);
    }
    public function open() {}
    public function desk() {}
}
"""

REPORT_CONTROLLER = """<?php
namespace App\\Http\\Controllers\\Api;

use Illuminate\\Routing\\Controllers\\HasMiddleware;
use Illuminate\\Routing\\Controllers\\Middleware;

class ReportController implements HasMiddleware
{
    public static function middleware(): array
    {
        return [new Middleware('auth', only: ['store'])];
    }

    public function index() {}
    public function store() {}
    public function destroy($report)
    {
        abort_unless(auth()->user()?->isAdmin(), 403);
    }
}
"""

STATS_CONTROLLER = """<?php
namespace App\\Http\\Controllers\\Api;

use Illuminate\\Support\\Facades\\Gate;

class StatsController
{
    public function __invoke()
    {
        if (Gate::denies('view-stats')) {
            abort(403);
        }
    }
}
"""

TAG_CONTROLLER = """<?php
namespace App\\Http\\Controllers\\Api;

class TagController
{
    public function __construct()
    {
        $this->middleware('auth')->except(['index']);
    }

    public function index() {}
    public function show() {}
}
"""

HOOK_CONTROLLER = """<?php
namespace App\\Http\\Controllers\\Api;

class HookController
{
    public function stripe() {}
}
"""


def _laravel_app(**extra: str) -> dict[str, str]:
    return {
        "composer.json": COMPOSER,
        "bootstrap/app.php": BOOTSTRAP,
        "routes/web.php": WEB,
        "routes/account.php": ACCOUNT,
        "routes/api.php": API,
        "routes/hooks.php": HOOKS,
        "routes/unloaded.php": UNLOADED,
        "app/Http/Middleware/RoleMiddleware.php": ROLE_MIDDLEWARE,
        "app/Http/Middleware/TraceMiddleware.php": TRACE_MIDDLEWARE,
        "app/Http/Controllers/HomeController.php": HOME_CONTROLLER,
        "app/Http/Controllers/Api/PostController.php": POST_CONTROLLER,
        "app/Http/Controllers/Api/ReportController.php": REPORT_CONTROLLER,
        "app/Http/Controllers/Api/StatsController.php": STATS_CONTROLLER,
        "app/Http/Controllers/Api/TagController.php": TAG_CONTROLLER,
        "app/Http/Controllers/Api/HookController.php": HOOK_CONTROLLER,
        **extra,
    }


def _write(tmp_path, files: dict[str, str]) -> Path:
    root = tmp_path / "rev"
    for path, content in files.items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text(content)
    return root


def _collections(tmp_path, files: dict[str, str] | None = None):
    context = RevisionContext.from_directory(_write(tmp_path, files or _laravel_app()))
    registry = default_registry()
    return {c: registry.collect(c, context) for c in (Capability.ROUTE_ANALYSIS, Capability.ROUTE_AUTHORIZATION)}


def _rows(tmp_path, files: dict[str, str] | None = None) -> list[dict]:
    return route_authorization.evaluate(_collections(tmp_path, files))


def _route(rows: list[dict], method: str, path: str) -> dict:
    matches = [row for row in rows if row["method"] == method and row["path"] == path]
    assert len(matches) == 1, (method, path, sorted((r["method"], r["path"]) for r in rows))
    return matches[0]


def test_route_files_load_the_way_laravel_loads_them(tmp_path):
    rows = _rows(tmp_path)
    paths = {(row["method"], row["path"]) for row in rows}
    assert ("GET", "/") in paths and ("GET", "/api/v1/posts") in paths   # web and api (prefix /api)
    assert ("GET", "/account") in paths                                  # required inside a group
    assert ("POST", "/hooks/stripe") in paths                            # then: closure, base_path()
    assert not any(path == "/forgotten" for _m, path in paths)           # never loaded
    assert not any(path == "/up" for _m, path in paths)


def test_group_middleware_protects_and_reaches_required_files(tmp_path):
    rows = _rows(tmp_path)
    assert _route(rows, "GET", "/orders")["status"] == "protected"
    account = _route(rows, "GET", "/account")
    assert account["status"] == "protected" and account["mechanisms"] == ["middleware"]
    assert _route(rows, "GET", "/")["status"] == "unprotected"


def test_guest_is_public_and_middleware_without_a_denial_is_not_a_guard(tmp_path):
    rows = _rows(tmp_path)
    login = _route(rows, "GET", "/login")
    assert login["status"] == "public" and "guest" in login["public_reason"]
    assert _route(rows, "GET", "/traced")["status"] == "unprotected"


def test_guards_are_per_method_on_the_same_path(tmp_path):
    rows = _rows(tmp_path)
    assert _route(rows, "GET", "/api/v1/posts")["status"] == "unprotected"
    assert _route(rows, "POST", "/api/v1/posts")["status"] == "protected"
    assert _route(rows, "PUT", "/api/v1/posts/{post}")["status"] == "protected"     # ->can()
    assert _route(rows, "PATCH", "/api/v1/posts/{post}")["status"] == "protected"


def test_project_middleware_alias_and_custom_group_resolve_to_a_denying_class(tmp_path):
    rows = _rows(tmp_path)
    nested = _route(rows, "DELETE", "/api/v1/admin/posts/{post}/comments/{comment}")
    assert nested["status"] == "protected"
    assert _route(rows, "GET", "/api/desk")["status"] == "protected"               # staff = auth + role:staff
    assert _route(rows, "GET", "/api/v1/admin/open")["status"] == "unprotected"    # withoutMiddleware


def test_calls_in_actions_and_controller_middleware_protect_handlers(tmp_path):
    rows = _rows(tmp_path)
    show = _route(rows, "GET", "/api/v1/posts/{post}")
    assert show["status"] == "protected" and show["mechanisms"] == ["call"]          # authorize(), not abort_if 404
    assert _route(rows, "GET", "/api/v1/stats")["status"] == "protected"           # Gate::denies + abort(403)
    assert _route(rows, "GET", "/api/v1/reports")["status"] == "unprotected"
    assert _route(rows, "POST", "/api/v1/reports")["status"] == "protected"        # new Middleware(only:)
    assert _route(rows, "DELETE", "/api/v1/reports/{report}")["status"] == "protected"  # abort_unless 403
    assert _route(rows, "GET", "/api/v1/tags")["status"] == "unprotected"          # ->except(['index'])
    assert _route(rows, "GET", "/api/v1/tags/{tag}")["status"] == "protected"
    assert _route(rows, "POST", "/hooks/stripe")["status"] == "protected"          # signed


def test_unprotected_route_is_a_finding_at_its_definition(tmp_path):
    findings, summary = route_authorization.run(_collections(tmp_path))
    home = next(f for f in findings if "GET / (HomeController::index)" in f["message"])
    assert home["file"] == "routes/web.php" and home["line"] == 5 and home["severity"] == "warning"
    store = next(f for f in findings if "DELETE" not in f["message"] and "/api/v1/admin/open" in f["message"])
    assert store["severity"] == "warning"
    assert summary["frameworks"] == ["laravel"] and summary["unprotected"] == len(findings)


def test_laravel_10_service_provider_and_kernel_aliases(tmp_path):
    files = _laravel_app()
    del files["bootstrap/app.php"]
    files["app/Providers/RouteServiceProvider.php"] = """<?php
namespace App\\Providers;

use Illuminate\\Support\\Facades\\Route;

class RouteServiceProvider
{
    public function boot(): void
    {
        $this->routes(function () {
            Route::middleware('api')->prefix('api')->group(base_path('routes/api.php'));
            Route::middleware('web')->group(base_path('routes/web.php'));
        });
    }
}
"""
    files["app/Http/Kernel.php"] = """<?php
namespace App\\Http;

class Kernel
{
    protected $middlewareAliases = [
        'role' => \\App\\Http\\Middleware\\RoleMiddleware::class,
    ];
    protected $middlewareGroups = [
        'staff' => ['auth', 'role:staff'],
    ];
}
"""
    rows = _rows(tmp_path, files)
    assert _route(rows, "GET", "/api/v1/admin/posts/{post}/comments")["status"] == "protected"
    assert _route(rows, "GET", "/api/desk")["status"] == "protected"
    assert not any(row["path"].startswith("/hooks") for row in rows)


def test_default_route_files_without_any_loader(tmp_path):
    files = {"composer.json": COMPOSER, "routes/api.php": API, "routes/web.php": WEB, "routes/account.php": ACCOUNT}
    paths = {(row["method"], row["path"]) for row in _rows(tmp_path, files)}
    assert ("GET", "/api/v1/posts") in paths and ("GET", "/") in paths


def test_plugin_analysis_reports_the_facts_route_table(tmp_path):
    from agent.core.workspace import Workspace
    from agent.plugins.frameworks.laravel.plugin import LaravelFrameworkPlugin

    result = LaravelFrameworkPlugin().analyze(Workspace(_write(tmp_path, _laravel_app())))
    rows = result.data["routes"]
    orders = next(r for r in rows if r["uri"] == "/orders")
    assert orders["source"] == "facts" and orders["middleware"] == ["web", "auth"]
    assert orders["action"] == "App\\Http\\Controllers\\HomeController@orders"
    stats = next(r for r in rows if r["uri"] == "/api/v1/stats")
    assert stats["action"] == "App\\Http\\Controllers\\Api\\StatsController"
