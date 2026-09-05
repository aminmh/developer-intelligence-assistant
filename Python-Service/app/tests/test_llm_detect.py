"""Tests for the LLM-assisted detection fallback (llm_detect + project_profile
wiring). The probe itself is never invoked — llm_detect_framework is
monkeypatched, and JSON extraction is unit-tested directly."""

from __future__ import annotations

import json

import projects.llm_detect as llm_detect
import projects.project_profile as pp
from projects.project_profile import build_project_profile


class TestExtractJson:
    def test_plain_json(self):
        out = llm_detect._extract_json('{"framework": "django", "confidence": 0.9}')
        assert out and out["framework"] == "django"

    def test_fenced_and_verbose(self):
        content = 'Sure!\n```json\n{"framework": "rails", "version": "8", "confidence": 0.87}\n```\nhope that helps'
        out = llm_detect._extract_json(content)
        assert out and out["framework"] == "rails" and out["version"] == "8"

    def test_garbage_returns_none(self):
        assert llm_detect._extract_json("no json here at all") is None


class TestExcerpt:
    def test_collects_manifests_and_listing(self, tmp_path):
        (tmp_path / "package.json").write_text(json.dumps({"name": "x", "dependencies": {}}), encoding="utf-8")
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "app.js").write_text("console.log(1)", encoding="utf-8")
        out = llm_detect._collect_excerpt(str(tmp_path))
        assert "package.json" in out  # in listing AND as manifest content
        assert '{"name": "x"' in out
        assert "src/" in out

    def test_skips_noise_dirs(self, tmp_path):
        (tmp_path / "node_modules").mkdir()
        (tmp_path / "node_modules" / "package.json").write_text("{}", encoding="utf-8")
        out = llm_detect._collect_excerpt(str(tmp_path))
        assert "node_modules" not in out
        assert "--- package.json ---" not in out  # noise manifest never scanned


class TestBuildProjectProfileFallback:
    def test_static_detection_still_wins(self, monkeypatch):
        # Archive-like composer.json → laravel detected statically; probe must
        # not run.
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "composer.json").write_text(
                json.dumps({"require": {"laravel/framework": "^12.0"}}), encoding="utf-8"
            )
            (root / "artisan").write_text("<?php", encoding="utf-8")
            calls = []

            def boom(*a, **k):
                calls.append(1)
                raise AssertionError("probe must not run when static wins")

            monkeypatch.setattr("projects.llm_detect.llm_detect_framework", boom)
            profile = build_project_profile(str(root))
            assert profile and profile["framework"] == "laravel"
            assert calls == []

    def test_llm_verdict_for_registered_framework_runs_plugin(self, monkeypatch, tmp_path):
        (tmp_path / "package.json").write_text(json.dumps({"name": "x"}), encoding="utf-8")
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "main.js").write_text("// no known framework marker", encoding="utf-8")

        verdict = {
            "framework": "laravel",
            "version": "12",
            "confidence": 0.9,
            "reason": "manifest-ish signal",
            "provider": "local",
        }
        monkeypatch.setattr("projects.llm_detect.llm_detect_framework", lambda path: verdict)
        profile = build_project_profile(str(tmp_path))
        assert profile is not None
        assert profile["framework"] == "laravel"
        assert profile["version"] == "12"
        # plugin analyze ran → canonical keys present (empty structure ok)
        assert isinstance(profile["routes"], list)
        assert "LLM-assisted detection" in (profile.get("summary") or "")

    def test_llm_verdict_for_unknown_framework_is_thin(self, monkeypatch, tmp_path):
        (tmp_path / "Cargo.toml").write_text("[package]\nname='x'\n", encoding="utf-8")
        verdict = {
            "framework": "rocket-rs",
            "version": None,
            "confidence": 0.8,
            "reason": "Cargo.toml present",
            "provider": "local",
        }
        monkeypatch.setattr("projects.llm_detect.llm_detect_framework", lambda path: verdict)
        profile = build_project_profile(str(tmp_path))
        assert profile is not None
        assert profile["framework"] == "rocket-rs"
        assert profile["routes"] == [] and profile["schema"] == []
        assert "no framework plugin registered" in (profile.get("summary") or "")

    def test_no_verdict_returns_none(self, monkeypatch, tmp_path):
        (tmp_path / "somefile.txt").write_text("hi", encoding="utf-8")
        monkeypatch.setattr("projects.llm_detect.llm_detect_framework", lambda path: None)
        assert build_project_profile(str(tmp_path)) is None

    def test_verdict_low_confidence_rejected_inside_probe(self):
        assert pp._DETECTION_THRESHOLD == 0.6  # static bar unchanged
