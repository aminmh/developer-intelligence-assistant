"""Front-end static checks (front-end assistance plan, Phase 6)."""

from __future__ import annotations

import io
import zipfile

import pytest

pytest.importorskip("tree_sitter_language_pack")

from code_quality.frontend_checks import check_file, check_sources, run_on_archive, run_on_directory, template_elements


def rules(path: str, source: str) -> list[tuple[str, int]]:
    return [(finding["rule"], finding["line"]) for finding in check_file(path, source.encode())]


VUE = """<template>
  <div>
    <img src="a.png">
    <img src="b.png" alt="">
    <img :src="c" :alt="name">
    <div @click="open(a > b)">Open</div>
    <div role="button" tabindex="0" @click="open">Open</div>
    <button @click="close"><svg /></button>
    <button aria-label="Close" @click="close"><svg /></button>
    <button>Save</button>
    <input v-model="email">
    <label>Email <input v-model="email"></label>
    <label for="phone">Phone</label><input id="phone" v-model="phone">
    <input type="hidden" name="t">
    <a @click="go">Go</a>
    <a href="/x"><img src="i.png" alt="Home"></a>
    <li v-for="item in items">{{ item }}</li>
    <li v-for="(item, index) in items" :key="index">{{ item }}</li>
    <li v-for="item in items" :key="item.id" tabindex="3">{{ item }}</li>
    <p v-html="description"></p>
    <!-- <img src="commented.png"> -->
  </div>
</template>

<script setup lang="ts">
// eval('not code');
localStorage.setItem('access_token', token);
localStorage.setItem('theme', 'dark');
const timer = setInterval(tick, 1000);
window.addEventListener('resize', onResize);
</script>
"""


def test_vue_template_and_script_rules_report_the_right_lines():
    assert rules("src/Page.vue", VUE) == sorted([
        ("a11y-img-alt", 3), ("a11y-click-on-noninteractive", 6), ("a11y-control-no-name", 8),
        ("a11y-input-no-label", 11), ("a11y-link-no-href", 15), ("frontend-list-without-key", 17),
        ("frontend-index-as-key", 18), ("a11y-positive-tabindex", 19), ("frontend-raw-html", 20),
        ("frontend-token-in-web-storage", 27), ("frontend-timer-not-cleared", 29), ("frontend-listener-not-removed", 30),
    ])


def test_cleanup_and_safe_patterns_are_not_reported():
    clean = """<template><ul><li v-for="row in rows" :key="row.id">{{ row.name }}</li></ul></template>
<script setup>
const timer = setInterval(tick, 1000);
onUnmounted(() => { clearInterval(timer); window.removeEventListener('resize', onResize); });
window.addEventListener('resize', onResize);
sessionStorage.setItem('lastTab', tab);
</script>
"""
    assert rules("src/List.vue", clean) == []


def test_jsx_rules_use_the_syntax_tree():
    source = """export function Card({ items, onPick, ...rest }) {
  return (
    <section>
      <img src="a.png" />
      <img src="b.png" alt="Logo" />
      <img {...rest} />
      <div onClick={() => onPick(items.length > 2)}>Pick</div>
      <button onClick={onPick}><Icon name="x" /></button>
      <button onClick={onPick} aria-label="Remove"><Icon name="x" /></button>
      <button>{label}</button>
      {items.map((item, index) => <li key={index}>{item}</li>)}
      <span dangerouslySetInnerHTML={{ __html: html }} />
      <input value={v} onChange={f} />
      <label>Name <input value={v} onChange={f} /></label>
    </section>
  );
}
"""
    assert rules("src/Card.tsx", source) == sorted([
        ("a11y-img-alt", 4), ("a11y-click-on-noninteractive", 7), ("a11y-control-no-name", 8),
        ("frontend-index-as-key", 11), ("frontend-raw-html", 12), ("a11y-input-no-label", 13),
    ])


def test_angular_templates_external_and_inline():
    html = """<ul>
  <li *ngFor="let row of rows">{{ row.name }}</li>
  <li *ngFor="let row of rows; trackBy: byId">{{ row.name }}</li>
</ul>
<div (click)="open()">Open</div>
<span [innerHTML]="body"></span>
<a routerLink="/home">Home</a>
<input [(ngModel)]="q" aria-label="Search">
"""
    assert rules("src/app/list.component.html", html) == sorted([
        ("frontend-ngfor-without-trackby", 2), ("a11y-click-on-noninteractive", 5), ("frontend-raw-html", 6)])
    component = """import { Component } from '@angular/core';
@Component({
  selector: 'app-x',
  template: `
    <img src="a.png">
    <button (click)="save()">Save</button>
  `,
})
export class XComponent {
  constructor(private s: DomSanitizer) { this.html = s.bypassSecurityTrustHtml(raw); }
  run(code: string) { return new Function(code)(); }
}
"""
    assert rules("src/app/x.component.ts", component) == sorted([
        ("a11y-img-alt", 5), ("frontend-raw-html", 10), ("frontend-eval", 11)])


def test_large_components_and_scanner_details():
    big = "<template><div /></template>\n<script setup>\n" + "const a = 1;\n" * 620 + "</script>\n"
    finding = check_file("src/Big.vue", big.encode())[0]
    assert finding["rule"] == "frontend-large-component" and finding["area"] == "maintainability" and "623 lines" in finding["message"]
    elements = list(template_elements('<a href="/x?a=1&b=2" title="1 > 0">Go <b>now</b></a>\n<br/><input disabled>'))
    assert [(e.tag, e.line, e.has_text) for e in elements] == [("a", 1, True), ("b", 1, True), ("br", 2, False), ("input", 2, False)]
    assert elements[0].attributes == {"href": "/x?a=1&b=2", "title": "1 > 0"} and elements[3].attributes == {"disabled": None}
    assert elements[1].inside == ("a",)


def test_directories_and_archives_skip_tests_vendor_and_plain_html(tmp_path):
    files = {
        "src/A.vue": "<template><img src='a.png'></template>\n",
        "src/A.spec.ts": "eval('x');\n",
        "node_modules/x/B.vue": "<template><img src='a.png'></template>\n",
        "public/index.html": "<img src='a.png'>\n",
        "src/api.ts": "export const run = (c: string) => eval(c);\n",
    }
    for name, content in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    result = run_on_directory(tmp_path)
    assert [(f["file"], f["rule"]) for f in result["findings"]] == [("src/A.vue", "a11y-img-alt"), ("src/api.ts", "frontend-eval")]
    assert result["files_checked"] == 2 and result["by_area"]["accessibility"] == 1 and result["by_area"]["security"] == 1

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    archived = run_on_archive(buffer.getvalue(), ["src/A.vue", "src/api.ts", "src/A.spec.ts"])
    assert [f["rule"] for f in archived["findings"]] == ["a11y-img-alt", "frontend-eval"]
    assert run_on_archive(buffer.getvalue(), ["src/api.ts"]) is None      # no component source: not a front-end project


def test_frequent_rules_are_capped():
    many = "<template>" + "<img src='a.png'>\n" * 200 + "</template>\n"
    result = check_sources([("A.vue", many.encode())])
    assert len(result["findings"]) == 150 and result["rule_counts"]["a11y-img-alt"] == 200 and result["notes"]


def test_revision_checks_are_cached_by_archive(tmp_path):
    from types import SimpleNamespace

    from projects.frontend.service import UiMapUnavailable, checks_for_revision

    root = tmp_path / "source"
    (root / "src").mkdir(parents=True)
    (root / "src/A.vue").write_text("<template><img src='a.png'></template>\n")
    revision = {"revision_id": "rev_" + "a" * 32, "state": "ready", "source_archive_sha256": "c" * 64}
    registry = SimpleNamespace(latest_ready=lambda _project: revision, get_by_revision_id=lambda _project, _id: revision)
    options = {"registry": registry, "source_resolver": lambda _context: SimpleNamespace(path=root, archive_sha256="c" * 64),
               "cache_dir": tmp_path}
    first = checks_for_revision("web", **options)
    assert first["revision_id"] == revision["revision_id"] and [f["rule"] for f in first["findings"]] == ["a11y-img-alt"]
    (root / "src/A.vue").unlink()
    assert checks_for_revision("web", **options)["findings"] == first["findings"]
    empty = SimpleNamespace(latest_ready=lambda _project: None, get_by_revision_id=lambda _project, _id: None)
    with pytest.raises(UiMapUnavailable):
        checks_for_revision("web", **{**options, "registry": empty})
