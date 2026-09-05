"""Laravel static route parser + project-profile tests."""

import json

from agent.plugins.frameworks.laravel import LaravelFrameworkPlugin
from agent.plugins.frameworks.laravel_routes import (
    parse_routes_directory,
    parse_routes_from_source,
)

_AUTH_FILE = """<?php
use App\\Http\\Controllers\\Auth\\AuthenticationController;
use Illuminate\\Support\\Facades\\Route;

Route::middleware('guest')->group(function () {
    Route::get('/register', [AuthenticationController::class, 'showRegisterForm'])->name('register');
    Route::post('/register', [AuthenticationController::class, 'register']);
});

Route::post('/logout', [AuthenticationController::class, 'logout'])->middleware('auth')->name('logout');
Route::get('/dashboard', [AuthenticationController::class, 'dashboard'])->middleware('auth')->name('dashboard');
"""

_COMPLEX_FILE = """<?php
use Illuminate\\Support\\Facades\\Route;

Route::prefix('api')->middleware('auth:sanctum')->group(function () {
    Route::post('/payments', [PaymentController::class, 'charge'])->name('payments.charge');
    Route::prefix('v1')->group(function () {
        Route::get('/users/{id}', [UserController::class, 'show']);
    });
});

Route::resource('users', UserController::class);
Route::apiResource('posts', 'App\\Http\\Controllers\\PostController');
Route::view('/about', 'pages.about')->name('about');
Route::redirect('/old', '/new');
Route::match(['get', 'post'], '/multi', [XController::class, 'handle']);
Route::get('/closure', function () { return view('x'); });
Route::delete('/items/{id}', [ItemController::class, 'destroy'])->middleware(['auth', 'admin']);
"""


class TestParseAuthFile:
    def setup_method(self):
        self.routes = parse_routes_from_source(_AUTH_FILE)

    def test_counts_all_routes(self):
        assert len(self.routes) == 4

    def test_uri_and_action(self):
        by_uri = {}
        for r in self.routes:
            by_uri.setdefault(r["uri"], []).append(r)
        assert by_uri["/register"][0]["method"] == "GET"
        assert "AuthenticationController@showRegisterForm" in by_uri["/register"][0]["action"]
        assert by_uri["/register"][1]["method"] == "POST"

    def test_names_parsed(self):
        register = [r for r in self.routes if r["uri"] == "/register" and r["method"] == "GET"]
        assert register and register[0]["name"] == "register"
        logout = [r for r in self.routes if r["uri"] == "/logout"]
        assert logout and logout[0]["name"] == "logout"

    def test_middleware_parsed(self):
        mw = {r["uri"]: r["middleware"] for r in self.routes}
        assert mw["/logout"] == ["auth"]
        assert mw["/register"] == []  # group-level middleware not attached


class TestParseComplexFile:
    def setup_method(self):
        self.routes = parse_routes_from_source(_COMPLEX_FILE)

    def test_prefix_group_applied(self):
        by_uri = {r["uri"]: r for r in self.routes}
        assert "/api/payments" in by_uri
        assert by_uri["/api/payments"]["method"] == "POST"
        assert "PaymentController@charge" in by_uri["/api/payments"]["action"]
        # nested v1 prefix
        assert "/api/v1/users/{id}" in by_uri

    def test_resource_expansion(self):
        uris = {r["uri"] for r in self.routes}
        assert "/users" in uris
        assert "/users/{id}" in uris
        assert "/posts" in uris

    def test_view_redirect_match(self):
        actions = {r["action"] for r in self.routes}
        assert "view:pages.about" in actions
        assert "redirect:/new" in actions

    def test_match_expands_methods(self):
        multi = [r for r in self.routes if r["uri"] == "/multi"]
        assert {r["method"] for r in multi} == {"GET", "POST"}

    def test_closure(self):
        closure = [r for r in self.routes if r["uri"] == "/closure"]
        assert closure and closure[0]["action"] == "closure"

    def test_no_duplicate_routes(self):
        assert len(self.routes) == len({(r["method"], r["uri"]) for r in self.routes})


class TestLaravelPluginAnalyze:
    def test_analyze_routes_data(self, tmp_path):
        (tmp_path / "routes").mkdir(parents=True)
        (tmp_path / "routes" / "web.php").write_text(_AUTH_FILE, encoding="utf-8")
        (tmp_path / "composer.json").write_text(
            json.dumps({"require": {"laravel/framework": "^12.0"}}), encoding="utf-8"
        )
        plugin = LaravelFrameworkPlugin()
        from agent.core.workspace import Workspace

        result = plugin.analyze(Workspace(tmp_path))
        assert result.data["routes"]
        assert len(result.data["routes"]) == 4
        assert result.to_dict()["data"]["routes"]

    def test_detect_nested_root(self, tmp_path):
        inner = tmp_path / "Archive"
        inner.mkdir()
        (inner / "composer.json").write_text(
            json.dumps({"require": {"laravel/framework": "^11.0"}}), encoding="utf-8"
        )
        (inner / "artisan").write_text("<?php\n", encoding="utf-8")
        plugin = LaravelFrameworkPlugin()
        from agent.core.types import Project

        det = plugin.detect(Project.from_path(tmp_path))
        assert det.name == "laravel"
        assert det.version == "11"
        assert det.confidence == 0.99


class TestParseDirectory:
    def test_files_tagged(self, tmp_path):
        r = parse_routes_directory([("web.php", _AUTH_FILE)])
        assert all(x["file"] == "web.php" for x in r)
