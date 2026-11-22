"""PHP taint analysis with Psalm (gap-closing plan, task 9; decision bundled-security-rules-are-redistributable).

Psalm runs on uploaded source without the project's vendor/ and must never load project
code; framework taint sources and sinks come from the detected plugins' stubs.
"""

import base64
import hashlib
import io
import json
import zipfile
from pathlib import Path
from uuid import uuid4

import pytest

from code_quality import taint_analysis
from code_quality.framework_evidence import taint_stubs

needs_psalm = pytest.mark.skipif(not taint_analysis.available(), reason="Psalm is not installed here")


def _archive(files: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for path, content in files.items():
            archive.writestr(path, content)
    return buffer.getvalue()


def _run(files: dict[str, str], stubs: dict[str, str] | None = None) -> dict:
    return taint_analysis.run_taint_analysis(_archive(files), list(files), stubs)


def _kinds(result: dict) -> list[tuple[str, str, int]]:
    return sorted((f["rule"].split(" ")[0], f["file"], f["line"]) for f in result["findings"])


@needs_psalm
def test_project_autoloaders_never_run_and_builtin_taint_is_found(tmp_path):
    marker = tmp_path / "ran"
    planted = f"<?php file_put_contents({str(marker)!r}, 'x');\n"
    result = _run({
        "composer.json": json.dumps({"autoload": {"files": ["src/boot.php"]}}),
        "vendor/autoload.php": planted,
        "src/boot.php": planted,
        "src/Shell.php": "<?php\nfunction run(): void {\n    $name = $_GET['name'];\n    shell_exec('ls ' . $name);\n}\n",
    })
    assert not marker.exists()
    assert result["status"] == "complete" and result["version"] == taint_analysis.VERSION
    [finding] = [f for f in result["findings"] if f["file"] == "src/Shell.php"]
    assert finding["rule"] == "psalm-TaintedShell (CWE-78)" and finding["line"] == 4
    assert finding["severity"] == "error" and finding["category"] == "security"
    assert "$_GET" in finding["message"] and [step["line"] for step in finding["trace"]] == [3, 4]


LARAVEL_CONTROLLER = """<?php
namespace App\\Http\\Controllers;

use App\\Models\\Concerns\\Audited;
use Illuminate\\Database\\Eloquent\\SoftDeletes;
use Illuminate\\Http\\Request;
use Illuminate\\Support\\Facades\\DB;
use Illuminate\\Support\\Facades\\Process;

class ReportController extends Controller
{
    use SoftDeletes;

    public function search(Request $request)
    {
        return DB::select("select * from users where name = '" . $request->input('q') . "'");
    }

    public function sort(Request $request)
    {
        return DB::table('users')->orderByRaw($request->query('sort'))->get();
    }

    public function export(Request $request)
    {
        Process::run('convert ' . $request->input('file'));
    }

    public function safe(Request $request)
    {
        return DB::select('select * from users where id = ?', [$request->input('id')]);
    }
}
"""


@needs_psalm
def test_laravel_plugin_taint_model_finds_framework_flows_without_vendor():
    files = {"composer.json": json.dumps({"require": {"laravel/framework": "^12.0"}}),
             "app/Http/Controllers/ReportController.php": LARAVEL_CONTROLLER,
             "tests/Feature/ReportTest.php": "<?php\nshell_exec($_GET['x']);\n"}
    stubs, coverage = taint_stubs(_archive(files), list(files))
    assert list(stubs) == ["laravel.phpstub"] and coverage["gaps"] == []
    result = _run(files, stubs)
    assert result["status"] == "complete"                       # missing vendor trait SoftDeletes did not abort
    assert _kinds(result) == [("psalm-TaintedShell", "app/Http/Controllers/ReportController.php", 26),
                              ("psalm-TaintedSql", "app/Http/Controllers/ReportController.php", 16),
                              ("psalm-TaintedSql", "app/Http/Controllers/ReportController.php", 21)]
    assert "Illuminate\\Http\\Request::input" in result["findings"][0]["message"]


SYMFONY_CONTROLLER = """<?php
namespace App\\Controller;

use Doctrine\\DBAL\\Connection;
use Doctrine\\ORM\\EntityManagerInterface;
use Symfony\\Component\\HttpFoundation\\Request;
use Symfony\\Component\\Process\\Process;
use Symfony\\Contracts\\HttpClient\\HttpClientInterface;

class ProbeController extends AbstractController
{
    public function __construct(private Connection $db, private EntityManagerInterface $em, private HttpClientInterface $http) {}

    public function search(Request $request)
    {
        return $this->db->fetchAllAssociative('SELECT * FROM t WHERE n = ' . $request->query->get('n'));
    }

    public function dql(Request $request)
    {
        $qb = $this->em->createQueryBuilder();
        $qb->andWhere('u.name = ' . $request->request->getString('name'));
        $qb->andWhere('u.id = :id')->setParameter('id', $request->query->get('id'));
    }

    public function run(Request $request)
    {
        Process::fromShellCommandline('ls ' . $request->get('dir'))->run();
    }

    public function fetch(Request $request)
    {
        return $this->http->request('GET', $request->toArray()['url']);
    }

    public function page(Request $request)
    {
        return $this->db->fetchAllAssociative('SELECT * FROM t LIMIT ' . $request->query->getInt('limit'));
    }
}
"""


@needs_psalm
def test_symfony_plugin_taint_model_finds_doctrine_shell_and_ssrf_flows():
    files = {"composer.json": json.dumps({"require": {"symfony/framework-bundle": "6.4.*"}}),
             "src/Controller/ProbeController.php": SYMFONY_CONTROLLER}
    stubs, _coverage = taint_stubs(_archive(files), list(files))
    assert list(stubs) == ["symfony.phpstub"]
    result = _run(files, stubs)
    assert _kinds(result) == [("psalm-TaintedSSRF", "src/Controller/ProbeController.php", 33),
                              ("psalm-TaintedShell", "src/Controller/ProbeController.php", 28),
                              ("psalm-TaintedSql", "src/Controller/ProbeController.php", 16),
                              ("psalm-TaintedSql", "src/Controller/ProbeController.php", 22)]


def test_no_php_source_and_invalid_stubs():
    assert _run({"main.py": "print(1)\n"})["status"] == "complete"
    with pytest.raises(ValueError):
        taint_analysis.validate_stubs({"../evil.phpstub": "<?php"})
    with pytest.raises(ValueError):
        taint_analysis.validate_stubs({"big.phpstub": "<?php" + "x" * taint_analysis.MAX_STUB_BYTES})
    assert taint_analysis.validate_stubs({"laravel.phpstub": "<?php\n"}) == {"laravel.phpstub": "<?php\n"}
    assert not taint_analysis.analysable("tests/Unit/A.php") and not taint_analysis.analysable("vendor/x/A.php")
    assert taint_analysis.analysable("src/A.php")


def test_pinned_version_matches_the_lock_file():
    lock = Path(__file__).parents[2] / "php" / "psalm" / "composer.lock"
    if not lock.is_file():
        pytest.skip("php/psalm is not mounted here")
    packages = {p["name"]: p["version"] for p in json.loads(lock.read_text())["packages"]}
    assert packages["vimeo/psalm"] == taint_analysis.VERSION


def test_worker_accepts_taint_stubs_only_for_psalm(monkeypatch):
    app_path = str(Path(__file__).parents[3] / "analyzer-worker")
    if not Path(app_path, "worker.py").is_file():
        pytest.skip("analyzer-worker is not mounted here")
    monkeypatch.syspath_prepend(app_path)
    import worker
    from fastapi import HTTPException

    archive = _archive({"src/A.php": "<?php\n"})
    payload = {"schema_version": 1, "job_id": str(uuid4()), "revision_id": "rev_" + "a" * 32, "commit_sha": None,
               "archive_base64": base64.b64encode(archive).decode("ascii"),
               "bundle_sha256": hashlib.sha256(archive).hexdigest(), "files": ["src/A.php"],
               "file_line_counts": {"src/A.php": 2}, "languages": {"php": 1}, "analyzers": ["psalm"],
               "settings": {"sonarqube": {}, "joern": {}}, "sonarqube_token": None,
               "taint_stubs": {"laravel.phpstub": "<?php\n"}}
    worker._validate_job(payload)
    with pytest.raises(HTTPException, match="Taint stubs are invalid"):
        worker._validate_job({**payload, "taint_stubs": {"x.php": "<?php"}})
    with pytest.raises(HTTPException, match="unselected analyzer"):
        worker._validate_job({**payload, "analyzers": ["scip"]})
    seen = {}
    monkeypatch.setattr(worker, "run_taint_analysis", lambda _a, files, stubs: seen.update(files=files, stubs=stubs)
                        or {"name": "psalm", "status": "complete", "findings": []})
    assert worker._run_job(payload)["analyzers"]["psalm"]["status"] == "complete"
    assert seen == {"files": ["src/A.php"], "stubs": {"laravel.phpstub": "<?php\n"}}


def test_run_worker_requests_psalm_only_from_workers_that_offer_it(monkeypatch):
    from code_quality import remote_worker

    sent = []
    monkeypatch.setattr(remote_worker, "get_worker_capabilities", lambda _t: {"psalm": {"status": "unavailable"}})
    monkeypatch.setattr(remote_worker, "_request", lambda *a, **k: sent.append(a) or {})
    archive = _archive({"src/A.php": "<?php\n"})
    result = remote_worker.run_worker({"source_transfer_consent": True}, {}, archive, ["src/A.php"],
                                      {"php": 1}, "rev_" + "a" * 32, None, None, {"laravel.phpstub": "<?php\n"})
    assert sent == [] and result["psalm"]["status"] == "disabled"


def test_the_runner_never_names_a_framework():
    import inspect

    source = inspect.getsource(taint_analysis).lower()
    assert "laravel" not in source and "symfony" not in source


def test_a_broken_archive_is_a_failed_result_not_an_exception(monkeypatch):
    monkeypatch.setattr(taint_analysis, "available", lambda: True)
    result = taint_analysis.run_taint_analysis(b"not a zip", ["src/A.php"])
    assert result["status"] == "failed" and "BadZipFile" in result["reason"]
