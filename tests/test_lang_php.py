"""PHP: Laravel route files (verbs, match/any, resource/apiResource with only/except, prefix and group arrays, the /api
prefix of routes/api.php), Symfony attributes and annotations, Slim groups, `use`/PSR-4 and require imports, externals,
and controller call flows. Heron Bakery is invented."""
from __future__ import annotations

from pathlib import Path

import pytest

import code_scan
from kestrel_fixtures import write
from langs import php

H = "heron-api"

HERON = {
    f"{H}/composer.json": '{"require": {"laravel/framework": "^11.0", "aws/aws-sdk-php": "^3.0", "predis/predis": "^2.0"},\n'
                          ' "autoload": {"psr-4": {"App\\\\": "app/"}}}\n',
    f"{H}/config/database.php": "<?php\nreturn [\n    'default' => env('DB_CONNECTION', 'mysql'),\n];\n",
    f"{H}/config/queue.php": "<?php\n\nreturn [\n    'connections' => ['sqs' => ['driver' => 'sqs']],\n];\n",
    f"{H}/routes/api.php": '''<?php

use App\\Http\\Controllers\\LoafController;
use App\\Http\\Controllers\\OvenController;
use Illuminate\\Support\\Facades\\Route;

Route::get('/loaves', [LoafController::class, 'index']);
Route::post('loaves', [LoafController::class, 'store'])->middleware('auth');
Route::apiResource('ovens', OvenController::class)->only(['index', 'show']);
// Route::get('/commented', [LoafController::class, 'index']);
# Route::get('/hashed', [LoafController::class, 'index']);

Route::prefix('admin')->middleware('auth')->group(function () {
    Route::delete('/loaves/{loaf}', 'LoafController@destroy');
    Route::match(['put', 'patch'], 'loaves/{loaf}', [LoafController::class, 'update']);
});

Route::group(['prefix' => 'v2', 'middleware' => 'auth'], function () {
    Route::any('ping', fn () => 'pong');
});
''',
    f"{H}/routes/web.php": '''<?php

use Illuminate\\Support\\Facades\\Route;

Route::get('/', function () {
    return view('welcome');
});
Route::resource('crumbs', \\App\\Http\\Controllers\\CrumbController::class)->except(['create', 'edit', 'destroy']);
''',
    f"{H}/app/Http/Controllers/Controller.php": "<?php\n\nnamespace App\\Http\\Controllers;\n\nabstract class Controller\n{\n}\n",
    f"{H}/app/Http/Controllers/LoafController.php": '''<?php

namespace App\\Http\\Controllers;

use App\\Models\\Loaf;
use App\\Services\\OvenQueue;
use Illuminate\\Http\\Request;
use Illuminate\\Support\\Facades\\Cache;
use Illuminate\\Support\\Facades\\DB;
use Illuminate\\Support\\Facades\\Redis;

class LoafController extends Controller
{
    public function __construct(private OvenQueue $queue)
    {
    }

    public function index()
    {
        return Cache::remember('loaves', 60, fn () => Loaf::where('fresh', true)->get());
    }

    /**
     * Bake a loaf: save it, queue rye for the oven, log it.
     */
    public function store(Request $request)
    {
        $loaf = Loaf::create($request->all());
        if ($loaf->isRye()) {
            $this->queue->bake($loaf);
        }
        try {
            DB::table('bake_log')->insert(['loaf_id' => $loaf->id, 'note' => "it's done; }"]);
        } catch (\\Throwable $e) {
            Redis::set('bake:failed', $loaf->id);
        }
        return $loaf;
    }

    public function update(Request $request, Loaf $loaf)
    {
        $loaf->update($request->all());
        return $this->fresh($loaf);
    }

    public function destroy(Loaf $loaf)
    {
        $loaf->delete();
    }

    private function fresh(Loaf $loaf)
    {
        return Loaf::findOrFail($loaf->id);
    }
}
''',
    f"{H}/app/Http/Controllers/OvenController.php": '''<?php

namespace App\\Http\\Controllers;

use App\\Models\\Oven;

class OvenController extends Controller
{
    public function index()
    {
        return Oven::all();
    }

    public function show(Oven $oven)
    {
        return $oven;
    }
}
''',
    f"{H}/app/Http/Controllers/CrumbController.php": '''<?php

namespace App\\Http\\Controllers;

class CrumbController extends Controller
{
    public function index() { return []; }
    public function store() { return []; }
    public function show($id) { return []; }
    public function update($id) { return []; }
}
''',
    f"{H}/app/Models/Loaf.php": '''<?php

namespace App\\Models;

use Illuminate\\Database\\Eloquent\\Model;

class Loaf extends Model
{
    public function isRye(): bool
    {
        return $this->kind === 'rye';
    }
}
''',
    f"{H}/app/Models/Oven.php": "<?php\n\nnamespace App\\Models;\n\nuse Illuminate\\Database\\Eloquent\\Model;\n\nclass Oven extends Model\n{\n}\n",
    f"{H}/app/Services/OvenQueue.php": '''<?php

namespace App\\Services;

use Aws\\Sqs\\SqsClient;

class OvenQueue
{
    public function __construct(private SqsClient $sqs)
    {
    }

    public function bake($loaf): void
    {
        $this->sqs->sendMessage(['QueueUrl' => 'q', 'MessageBody' => (string) $loaf->id]);
    }
}
''',
    f"{H}/tests/Feature/LoafTest.php": "<?php\nRoute::get('/test-only', fn () => 1);\n",
    f"{H}/vendor/laravel/junk.php": "<?php\nRoute::get('/vendored', fn () => 1);\n",
    "crumb-admin/composer.json": '{"require": {"symfony/framework-bundle": "^7.0", "doctrine/orm": "^3.0"}, "autoload": {"psr-4": {"Crumb\\\\": "src/"}}}\n',
    "crumb-admin/src/Controller/TrayController.php": '''<?php

namespace Crumb\\Controller;

use Crumb\\Repository\\TrayRepository;
use Symfony\\Component\\Routing\\Attribute\\Route;

#[Route('/trays', name: 'trays_')]
class TrayController extends AbstractController
{
    public function __construct(private TrayRepository $trays)
    {
    }

    #[Route('', name: 'list', methods: ['GET'])]
    public function list(): JsonResponse
    {
        return $this->json($this->trays->findAll());
    }

    #[Route('/{id}', methods: ['GET', 'DELETE'])]
    public function one(int $id): JsonResponse
    {
        return $this->json($this->trays->find($id));
    }

    /**
     * @Route("/legacy/{id}", methods={"POST"})
     */
    public function legacy(int $id)
    {
    }
}
''',
    "crumb-admin/src/Repository/TrayRepository.php": '''<?php

namespace Crumb\\Repository;

use Doctrine\\Bundle\\DoctrineBundle\\Repository\\ServiceEntityRepository;

class TrayRepository extends ServiceEntityRepository
{
}
''',
    "oven-edge/composer.json": '{"require": {"slim/slim": "^4.0", "guzzlehttp/guzzle": "^7.0"}}\n',
    "oven-edge/src/helpers.php": "<?php\nfunction warmth() { return 1; }\n",
    "oven-edge/public/index.php": '''<?php
require __DIR__ . '/../vendor/autoload.php';
require_once __DIR__ . '/../src/helpers.php';

use Slim\\Factory\\AppFactory;

$app = AppFactory::create();
$app->get('/status', function ($request, $response) {
    return $response;
});
$app->group('/edge', function (RouteCollectorProxy $group) {
    $group->post('/warm', function ($req, $res) {
        $client = new \\GuzzleHttp\\Client();
        $client->post('http://heron-api/api/loaves');
        return $res;
    });
});
$app->run();
''',
}


@pytest.fixture()
def cm(tmp_path: Path):
    return code_scan.scan([write(tmp_path, HERON)])


def _routes(cm) -> set[tuple[str, str]]:
    return {(r["m"], r["p"]) for r in cm.routes}


def _files(cm) -> list[str]:
    return [f["p"] for f in cm.files]


def test_laravel_routes_with_api_prefix_groups_and_resources(cm) -> None:
    assert {("GET", "/api/loaves"), ("POST", "/api/loaves"), ("GET", "/api/ovens"), ("GET", "/api/ovens/{oven}"),
            ("DELETE", "/api/admin/loaves/{loaf}"), ("PUT", "/api/admin/loaves/{loaf}"), ("PATCH", "/api/admin/loaves/{loaf}"),
            ("ANY", "/api/v2/ping"), ("GET", "/"), ("GET", "/crumbs"), ("POST", "/crumbs"), ("GET", "/crumbs/{crumb}"),
            ("PUT", "/crumbs/{crumb}"), ("PATCH", "/crumbs/{crumb}")} <= _routes(cm)


def test_symfony_attributes_annotations_and_slim_groups(cm) -> None:
    assert {("GET", "/trays"), ("GET", "/trays/{id}"), ("DELETE", "/trays/{id}"), ("POST", "/trays/legacy/{id}"),
            ("GET", "/status"), ("POST", "/edge/warm")} <= _routes(cm)


def test_no_routes_from_comments_tests_or_vendor(cm) -> None:
    paths = {p for _, p in _routes(cm)}
    assert not paths & {"/api/commented", "/api/hashed", "/test-only", "/vendored"}
    assert len(cm.routes) == 20


def test_laravel_route_points_at_the_controller_method(cm) -> None:
    files = _files(cm)
    store = next(r for r in cm.routes if r["m"] == "POST" and r["p"] == "/api/loaves")
    assert files[store["f"]] == f"{H}/app/Http/Controllers/LoafController.php" and store["l"] == 26
    assert store["t"] == "Store loaf"
    destroy = next(r for r in cm.routes if r["m"] == "DELETE" and r["p"] == "/api/admin/loaves/{loaf}")
    assert files[destroy["f"]].endswith("LoafController.php")
    ping = next(r for r in cm.routes if r["p"] == "/api/v2/ping")
    assert files[ping["f"]] == f"{H}/routes/api.php" and ping["l"] == 19
    tray = next(r for r in cm.routes if r["p"] == "/trays")
    assert files[tray["f"]].endswith("TrayController.php") and tray["l"] == 16


def test_services_are_composer_folders(cm) -> None:
    assert cm.services == ["crumb-admin", H, "oven-edge"]


def test_use_and_require_imports(cm) -> None:
    idx = {f: i for i, f in enumerate(_files(cm))}
    links = {(a, b) for a, b, _, _ in cm.links}
    loaf = idx[f"{H}/app/Http/Controllers/LoafController.php"]
    assert (loaf, idx[f"{H}/app/Models/Loaf.php"]) in links
    assert (loaf, idx[f"{H}/app/Services/OvenQueue.php"]) in links
    assert (loaf, idx[f"{H}/app/Http/Controllers/Controller.php"]) in links  # same namespace
    assert (idx[f"{H}/routes/api.php"], loaf) in links
    assert (idx["crumb-admin/src/Controller/TrayController.php"], idx["crumb-admin/src/Repository/TrayRepository.php"]) in links
    assert (idx["oven-edge/public/index.php"], idx["oven-edge/src/helpers.php"]) in links


def test_externals(cm) -> None:
    x = {f["p"]: set(f["x"]) for f in cm.files}
    assert {"cache", "mysql"} <= x[f"{H}/app/Http/Controllers/LoafController.php"]
    assert "mysql" in x[f"{H}/app/Models/Loaf.php"]
    assert "sqs" in x[f"{H}/app/Services/OvenQueue.php"]
    assert "postgres" in x["crumb-admin/src/Repository/TrayRepository.php"]
    assert "http" in x["oven-edge/public/index.php"]
    assert x[f"{H}/config/queue.php"] == set()  # config arrays name every driver, used or not


def _flow(data: dict, method: str, path: str) -> list[tuple]:
    i = next(i for i, r in enumerate(data["routes"]) if r["m"] == method and r["p"] == path)
    steps = php.flows(data, Path(data["base"]), i)
    assert steps is not None
    return [(s[0], s[1], s[2], s[5], s[6]) for s in steps]


def test_controller_flows(tmp_path: Path) -> None:
    cm = code_scan.scan([write(tmp_path, HERON)])
    data = {"base": str(tmp_path), "files": cm.files, "routes": cm.routes, "services": cm.services}
    assert _flow(data, "POST", "/api/loaves") == [
        (0, "sql", "MySQL: INSERT Loaf", 0, ""),
        (0, "call", "bake()", 1, "loaf is rye"),
        (1, "aws", "SQS: SendMessage", 1, "loaf is rye"),
        (0, "sql", "MySQL: INSERT bake_log", 0, ""),
        (0, "cache", "Cache: SET", 2, "it fails"),
    ]
    assert _flow(data, "GET", "/api/loaves") == [(0, "cache", "Cache: GET", 0, ""), (0, "sql", "MySQL: SELECT Loaf", 0, "")]
    assert _flow(data, "PUT", "/api/admin/loaves/{loaf}") == [(0, "sql", "MySQL: UPDATE Loaf", 0, ""), (0, "call", "fresh()", 0, ""),
                                                             (1, "sql", "MySQL: SELECT by id Loaf", 0, "")]
    assert _flow(data, "GET", "/trays") == [(0, "sql", "Postgres: SELECT Tray", 0, "")]
    assert _flow(data, "POST", "/edge/warm") == [(0, "http", "HTTP POST http://heron-api/api/loaves", 0, "")]


def test_workflow_stream_and_kafka_clients() -> None:
    src = "<?php\nuse Aws\\Sfn\\SfnClient;\nuse Aws\\Kinesis\\KinesisClient;\n$c = new \\RdKafka\\Producer();\n"
    assert {"sfn", "kinesis", "kafka"} <= php.externals(src)
