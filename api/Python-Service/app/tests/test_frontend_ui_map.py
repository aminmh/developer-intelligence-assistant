"""UI map of Angular, React, and Vue projects (front-end assistance plan, Phase 2)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("tree_sitter_language_pack")

from agent.core.registry import PluginRegistry
from agent.core.types import Project
from agent.core.workspace import Workspace
from agent.plugins import register_builtins
from projects.frontend.ui_map import build_ui_map, find_apps


def write(root: Path, files: dict[str, str]) -> Path:
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    return root


def package(**dependencies: str) -> str:
    return json.dumps({"name": "app", "dependencies": dependencies})


def by_name(items: list[dict], name: str) -> dict:
    return next(item for item in items if item["name"] == name)


def calls(ui_map: dict, ids: list[int]) -> list[str]:
    return sorted(f"{ui_map['http_calls'][i]['method']} {ui_map['http_calls'][i]['url']}" for i in ids)


VUE = {
    "package.json": package(vue="^3.4.0", pinia="^2.0.0"),
    "tsconfig.json": '{\n  // aliases\n  "compilerOptions": {"baseUrl": ".", "paths": {"@/*": ["src/*"]}},\n}',
    "src/router/index.ts": """
import { createRouter } from 'vue-router';
import HomeView from '@/views/HomeView.vue';
export const router = createRouter({ routes: [
  { path: '/', component: HomeView },
  { path: '/orders', component: () => import('../views/OrdersView.vue'), children: [
    { path: ':id', component: () => import('@/views/OrderDetail.vue') },
  ] },
  { path: '/old', redirect: '/' },
] });
""",
    "src/views/HomeView.vue": "<template><div><order-card /><RouterLink to='/orders' /></div></template>\n"
                              "<script setup lang=\"ts\">\nimport OrderCard from '@/components/OrderCard.vue';\n</script>\n",
    "src/views/OrdersView.vue": """<template><OrderCard v-for="o in store.orders" /></template>
<script setup lang="ts">
import OrderCard from '../components/OrderCard.vue';
import { useOrderStore } from '@/stores/orders';
const store = useOrderStore();
</script>
""",
    "src/views/OrderDetail.vue": """<template><p>{{ order }}</p></template>
<script>
import axios from 'axios';
export default {
  async mounted() { this.order = (await axios.get(`/api/orders/${this.$route.params.id}`)).data; },
};
</script>
""",
    "src/components/OrderCard.vue": "<template><article /></template>\n<script setup>\ndefineProps(['order']);\n</script>\n",
    "src/stores/orders.ts": """
import { defineStore } from 'pinia';
import { listOrders } from '@/api/orders';
export const useOrderStore = defineStore('orders', { actions: { async load() { this.orders = await listOrders(); } } });
""",
    "src/api/orders.ts": """
import { http } from './http';
const BASE = '/api/orders';
export const listOrders = () => http.get(BASE);
export const createOrder = (body: unknown) => http.post(BASE + '/', body);
export function cancel(id: number) { return fetch(`${BASE}/${id}/cancel?force=1`, { method: 'DELETE' }); }
const cache = new Map(); cache.get('x'); new URLSearchParams().delete('y');
""",
    "src/api/http.ts": "import axios from 'axios';\nexport const http = axios.create({ baseURL: '/' });\n",
    "src/api/orders.spec.ts": "fetch('/should/not/appear');\n",
}


def test_vue_map_links_routes_components_stores_and_requests(tmp_path):
    ui_map = build_ui_map(write(tmp_path, VUE))
    assert ui_map["framework"] == "vue" and ui_map["notes"] == []

    routes = {route["path"]: route for route in ui_map["routes"]}
    assert sorted(routes) == ["/", "/old", "/orders", "/orders/:id"]
    assert routes["/"]["component"] == "HomeView" and routes["/"]["file"] == "src/views/HomeView.vue"
    assert routes["/orders/:id"]["component"] == "OrderDetail" and routes["/orders/:id"]["lazy"] is True
    assert routes["/old"]["redirect"] == "/"

    home = by_name(ui_map["components"], "HomeView")
    assert home["uses"] == ["src/components/OrderCard.vue#OrderCard"]  # kebab-case tag; RouterLink is not a project component
    orders = by_name(ui_map["components"], "OrdersView")
    assert orders["stores"] == ["src/stores/orders.ts#useOrderStore"]
    assert orders["http_calls"] == []
    # Requests reached through the store and API module it imports, with constants and templates resolved.
    assert calls(ui_map, orders["via_imports"]) == ["DELETE /api/orders/{id}/cancel", "GET /api/orders", "POST /api/orders/"]
    detail = by_name(ui_map["components"], "OrderDetail")
    assert calls(ui_map, detail["http_calls"]) == ["GET /api/orders/{id}"]
    request = ui_map["http_calls"][detail["http_calls"][0]]
    assert request["line"] == 5 and request["symbol"] == "mounted"  # line in the .vue file, not in the script block

    assert [store["kind"] for store in ui_map["stores"]] == ["pinia"]
    assert all("should/not" not in (call["url"] or "") for call in ui_map["http_calls"])  # test files are skipped
    assert len(ui_map["http_calls"]) == 4  # Map.get and URLSearchParams.delete are not requests


REACT = {
    "package.json": package(react="^18.2.0", **{"react-router-dom": "^6.0.0", "zustand": "^4.0.0"}),
    "src/App.tsx": """
import { Routes, Route } from 'react-router-dom';
import { Dashboard } from './pages/Dashboard';
import UserPage from './pages/UserPage';
export default function App() {
  return (
    <Routes>
      <Route path="/" element={<Dashboard />} />
      <Route path="users">
        <Route path=":id" element={<UserPage />} />
      </Route>
    </Routes>
  );
}
""",
    "src/pages/Dashboard.tsx": """
import { useCart } from '../state/cart';
import { Badge } from '../ui/Badge';
export const Dashboard = () => {
  const items = useCart((s) => s.items);
  return <section><Badge count={items.length} /><Badge.Dot /></section>;
};
function helper() { return 1; }
""",
    "src/pages/UserPage.tsx": """
import { useEffect, useState } from 'react';
const API = process.env.API_URL;
export default function UserPage({ id }: { id: string }) {
  const [user, setUser] = useState(null);
  useEffect(() => {
    fetch(`${API}/users/${id}`).then((r) => r.json()).then(setUser);
    fetch(buildUrl(id), { method: 'POST' });
  }, [id]);
  return <div>{user}</div>;
}
""",
    "src/ui/Badge.tsx": "export function Badge({ count }: { count: number }) { return <span>{count}</span>; }\n",
    "src/state/cart.ts": "import { create } from 'zustand';\nexport const useCart = create((set) => ({ items: [] }));\n",
    "src/routes.tsx": """
import { createBrowserRouter } from 'react-router-dom';
import Settings from './pages/Settings';
export const router = createBrowserRouter([{ path: '/settings', element: <Settings /> }]);
""",
    "src/pages/Settings.jsx": "export default function Settings() { return <form />; }\n",
}


def test_react_map_reads_jsx_routes_object_routes_and_hooks(tmp_path):
    ui_map = build_ui_map(write(tmp_path, REACT))
    assert ui_map["framework"] == "react"
    routes = {route["path"]: route["component"] for route in ui_map["routes"]}
    assert routes == {"/": "Dashboard", "/users": None, "/users/:id": "UserPage", "/settings": "Settings"}

    names = sorted(component["name"] for component in ui_map["components"])
    assert names == ["App", "Badge", "Dashboard", "Settings", "UserPage"]  # helper() returns no JSX
    dashboard = by_name(ui_map["components"], "Dashboard")
    assert dashboard["uses"] == ["src/ui/Badge.tsx#Badge"]
    assert dashboard["stores"] == ["src/state/cart.ts#useCart"]
    user = by_name(ui_map["components"], "UserPage")
    requests = [ui_map["http_calls"][i] for i in user["http_calls"]]
    assert [(r["method"], r["url"]) for r in requests] == [("GET", "{API}/users/{id}"), ("POST", None)]
    assert requests[1]["raw"] == "buildUrl(id)" and requests[0]["symbol"] == "UserPage"
    assert any("run time" in note for note in ui_map["notes"])


ANGULAR = {
    "package.json": package(**{"@angular/core": "^17.0.0"}),
    "src/app/app.routes.ts": """
import { Routes } from '@angular/router';
import { HomeComponent } from './home/home.component';
export const routes: Routes = [
  { path: '', component: HomeComponent },
  { path: 'invoices', loadComponent: () => import('./invoices/invoice-list.component').then((m) => m.InvoiceListComponent) },
  { path: 'admin', loadChildren: () => import('./admin/admin.routes').then((m) => m.ADMIN_ROUTES) },
  { path: '**', redirectTo: '' },
];
""",
    "src/app/admin/admin.routes.ts": """
import { Routes } from '@angular/router';
import { UsersComponent } from './users.component';
export const ADMIN_ROUTES: Routes = [{ path: 'users', component: UsersComponent }];
""",
    "src/app/admin/users.component.ts": """
import { Component } from '@angular/core';
@Component({ selector: 'app-users', standalone: true, template: '<p>users</p>' })
export class UsersComponent {}
""",
    "src/app/home/home.component.ts": """
import { Component } from '@angular/core';
@Component({ selector: 'app-home', templateUrl: './home.component.html' })
export class HomeComponent {}
""",
    "src/app/home/home.component.html": "<main><app-invoice-list></app-invoice-list><router-outlet /></main>\n",
    "src/app/invoices/invoice-list.component.ts": """
import { Component, inject } from '@angular/core';
import { InvoiceService } from './invoice.service';
@Component({ selector: 'app-invoice-list', template: '<ul></ul>' })
export class InvoiceListComponent {
  private readonly invoices = inject(InvoiceService);
}
""",
    "src/app/invoices/invoice.service.ts": """
import { Injectable } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { environment } from '../../environments/environment';
@Injectable({ providedIn: 'root' })
export class InvoiceService {
  constructor(private http: HttpClient) {}
  list() { return this.http.get<Invoice[]>(`${environment.apiUrl}/invoices`); }
  pay(id: number) { return this.http.post<void>('/api/invoices/' + id + '/pay', {}); }
  raw() { return this.http.request('PUT', '/api/invoices/bulk'); }
}
""",
}


def test_angular_map_reads_decorators_templates_lazy_routes_and_services(tmp_path):
    ui_map = build_ui_map(write(tmp_path, ANGULAR))
    assert ui_map["framework"] == "angular"
    routes = {route["path"]: route for route in ui_map["routes"]}
    assert routes["/"]["component"] == "HomeComponent"
    assert routes["/invoices"]["component"] == "InvoiceListComponent" and routes["/invoices"]["lazy"]
    assert routes["/admin/users"]["component"] == "UsersComponent"  # prefixed by the lazy parent route
    assert routes["/**"]["redirect"] == ""

    home = by_name(ui_map["components"], "HomeComponent")
    assert home["uses"] == ["src/app/invoices/invoice-list.component.ts#InvoiceListComponent"]  # from the HTML template
    listing = by_name(ui_map["components"], "InvoiceListComponent")
    assert listing["stores"] == ["src/app/invoices/invoice.service.ts#InvoiceService"]
    assert calls(ui_map, listing["via_imports"]) == [
        "GET {apiUrl}/invoices", "POST /api/invoices/{id}/pay", "PUT /api/invoices/bulk"]
    assert ui_map["http_calls"][0]["symbol"] == "InvoiceService.list"
    assert by_name(ui_map["stores"], "InvoiceService")["kind"] == "service"


def test_file_based_routes_for_next_and_nuxt(tmp_path):
    next_app = write(tmp_path / "next", {
        "package.json": package(next="14.0.0", react="18.2.0"),
        "app/page.tsx": "export default function Home() { return <main />; }\n",
        "app/(shop)/products/[id]/page.tsx": "export default function Product() { return <div />; }\n",
        "app/products/layout.tsx": "export default function Layout() { return <div />; }\n",
        "pages/blog/[...slug].tsx": "export default function Post() { return <div />; }\n",
        "pages/api/hello.ts": "export default function handler() {}\n",
    })
    paths = {route["path"]: route["component"] for route in build_ui_map(next_app)["routes"]}
    assert paths == {"/": "Home", "/products/:id": "Product", "/blog/*": "Post"}

    nuxt_app = write(tmp_path / "nuxt", {
        "package.json": package(nuxt="^3.0.0"),
        "pages/index.vue": "<template><div /></template>\n",
        "pages/users/[id].vue": "<template><div /></template>\n<script setup>\nconst { data } = await useFetch('/api/users/1');\n</script>\n",
    })
    ui_map = build_ui_map(nuxt_app)
    assert ui_map["meta_framework"] == "nuxt"
    assert sorted(route["path"] for route in ui_map["routes"]) == ["/", "/users/:id"]
    assert calls(ui_map, by_name(ui_map["components"], "[id]")["http_calls"]) == ["GET /api/users/1"]


def test_apps_are_found_in_a_monorepo_and_absent_projects_are_reported(tmp_path):
    write(tmp_path, {"apps/web/package.json": package(vue="3"), "apps/admin/package.json": package(react="18"),
                     "apps/api/package.json": package(express="4"), "node_modules/x/package.json": package(react="18")})
    assert [(app["root"], app["framework"]) for app in find_apps(tmp_path)] == [("apps/admin", "react"), ("apps/web", "vue")]
    empty = build_ui_map(write(tmp_path / "empty", {"index.php": "<?php"}))
    assert empty["framework"] is None and empty["routes"] == [] and empty["notes"]


def test_plugins_detect_each_framework_without_displacing_php(tmp_path):
    registry = register_builtins(PluginRegistry())
    for files, expected in ((VUE, "vue"), (REACT, "react"), (ANGULAR, "angular")):
        root = write(tmp_path / expected, files)
        detected = registry.detect(Project.from_path(root))
        assert [framework.name for framework in detected.frameworks] == [expected]
        result = registry.framework(expected).analyze(Workspace(root), runtime_routes=False)
        assert result.data["ui_map"]["framework"] == expected
        assert result.data["routes"] and all(route["method"] == "PAGE" for route in result.data["routes"])
        assert result.data["frontend"]["components"]

    laravel = write(tmp_path / "laravel", {
        "composer.json": json.dumps({"require": {"laravel/framework": "^11.0"}}), "artisan": "#!/usr/bin/env php",
        "package.json": package(vue="^3.4.0"), "resources/js/App.vue": "<template><div /></template>\n"})
    names = [framework.name for framework in registry.detect(Project.from_path(laravel)).frameworks]
    assert names[0] == "laravel" and "vue" in names


# ── serving a revision's map ─────────────────────────────────────────────────

class FakeRegistry:
    def __init__(self, revision):
        self.revision = revision

    def latest_ready(self, _project_id):
        return self.revision

    def get_by_revision_id(self, _project_id, revision_id):
        return self.revision if self.revision and self.revision["revision_id"] == revision_id else None


def test_revision_map_is_cached_and_reports_missing_source(tmp_path):
    from types import SimpleNamespace

    from projects.frontend.service import UiMapUnavailable, ui_map_for_revision

    root = write(tmp_path / "source", VUE)
    revision = {"revision_id": "rev_" + "a" * 32, "state": "ready", "source_archive_sha256": "b" * 64, "source_kind": "git"}
    resolved = []

    def resolver(context):
        resolved.append(context.revision_id)
        return SimpleNamespace(path=root, archive_sha256="b" * 64)

    options = {"registry": FakeRegistry(revision), "source_resolver": resolver, "cache_dir": tmp_path / "cache"}
    (tmp_path / "cache").mkdir()
    first = ui_map_for_revision("web", **options)
    assert first["framework"] == "vue" and first["revision_id"] == revision["revision_id"]
    (root / "src/views/HomeView.vue").unlink()  # a cached map does not re-read the source
    second = ui_map_for_revision("web", revision["revision_id"], **options)
    assert second["components"] == first["components"] and len(list((tmp_path / "cache").iterdir())) == 1

    with pytest.raises(ValueError):
        ui_map_for_revision("web", app_root="../etc", **options)
    with pytest.raises(ValueError):
        ui_map_for_revision("web", app_root="apps/none", **options)
    with pytest.raises(UiMapUnavailable):
        ui_map_for_revision("web", **{**options, "registry": FakeRegistry(None)})


def test_chat_tool_summarises_routes_and_component_dependencies(tmp_path, monkeypatch):
    from types import SimpleNamespace

    import projects.frontend.service as service
    from agent_tools import frontend
    from agent_tools.types import ToolContext

    ui_map = {"project_id": "web", "revision_id": "rev_1", **build_ui_map(write(tmp_path, VUE))}
    monkeypatch.setattr(service, "ui_map_for_revision", lambda *_args, **_kwargs: ui_map)
    context = ToolContext(workflow="chat", project_id="web")

    overview = frontend._ui_map({}, context)
    assert "4 routes, 4 components, 1 stores, 4 HTTP calls" in overview
    assert "/orders/:id -> OrderDetail (lazy)" in overview and "/old -> redirect to /" in overview
    detail = frontend._ui_map({"search": "OrdersView"}, context)
    assert "stores: useOrderStore" in detail and "GET /api/orders" in detail and "uses: OrderCard" in detail
    assert SimpleNamespace  # keep the import used


def test_inertia_apps_explain_missing_routes_and_refs_name_their_variable(tmp_path):
    ui_map = build_ui_map(write(tmp_path, {
        "package.json": package(vue="^3.4.0", **{"@inertiajs/vue3": "^2.0.0"}),
        "resources/js/pages/Orders.vue": "<template><div /></template>\n<script setup>\nimport { ref } from 'vue';\n"
                                         "const orderId = ref(1);\nfetch(`/api/orders/${orderId.value}`);\n</script>\n",
    }))
    assert ui_map["routes"] == [] and any("Inertia" in note for note in ui_map["notes"])
    assert ui_map["http_calls"][0]["url"] == "/api/orders/{orderId}"


def test_requests_record_query_body_and_models_for_contract_checks(tmp_path):
    ui_map = build_ui_map(write(tmp_path, {
        "package.json": package(react="^18.0.0"),
        "src/api.ts": """
import axios from 'axios';
export interface Order { id: number; note?: string; 'ship-to': string }
export type Filter = { status: string };
type Alias = string;
export const list = (page: number) => axios.get(`/api/orders?page=${page}&sort=asc`, { params: { status: 'open' } });
export const create = (note: string) => axios.post('/api/orders', { note, total: 1 });
export const patch = (id: number, body: object) => axios.patch('/api/orders/' + id, { ...body, note: 'x' });
export const send = (body: object) => fetch('/api/orders', { method: 'PUT', body: JSON.stringify({ id: 1 }) });
export const search = (qs: string, options: object) => axios.get(`/api/orders?${qs}`, options);
export const remove = (id: number) => axios.delete(`/api/orders/${id}`);
""",
    }))
    by_symbol = {call["symbol"]: call for call in ui_map["http_calls"]}
    assert by_symbol["list"]["url"] == "/api/orders"
    assert by_symbol["list"]["query"] == ["page", "sort", "status"] and by_symbol["list"]["query_complete"] is True
    assert by_symbol["create"]["body_keys"] == ["note", "total"] and by_symbol["create"]["body_complete"] is True
    assert by_symbol["patch"]["body_keys"] == ["note"] and by_symbol["patch"]["body_complete"] is False  # spread hides keys
    assert by_symbol["send"]["method"] == "PUT" and by_symbol["send"]["body_keys"] == ["id"]
    assert by_symbol["search"]["query_complete"] is False  # the query string and options are variables
    assert by_symbol["remove"]["has_body"] is False and by_symbol["remove"]["query_complete"] is True
    models = {model["name"]: model["properties"] for model in ui_map["models"]}
    assert models == {"Order": [{"name": "id", "optional": False}, {"name": "note", "optional": True},
                                {"name": "ship-to", "optional": False}],
                      "Filter": [{"name": "status", "optional": False}]}
