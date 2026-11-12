"""Frontend -> backend links: HTTP calls in JS/TS frontends, normalised and matched to backend routes (code.json clientCalls)."""
from __future__ import annotations

from pathlib import Path

import pytest

import code_scan
from kestrel_fixtures import LOYALTY, ROASTER, STOREFRONT, write
from langs import clientmatch, js


def _calls(text: str) -> list[tuple]:
    return [(m, p) for _, m, p in js.client_calls(Path("x.ts"), text)]


@pytest.mark.parametrize("src, expected", [
    ("fetch('/api/beans')", [("GET", "/api/beans")]),
    ("fetch(`/api/beans/${id}`, { method: 'PUT', body })", [("PUT", "/api/beans/{id}")]),
    ("const API_BASE = process.env.NEXT_PUBLIC_API;\nfetch(`${API_BASE}/api/cups?size=2`)", [("GET", "/api/cups")]),
    ("axios.post('/api/orders', body)", [("POST", "/api/orders")]),
    ("axios.get<Order[]>(\"/api/orders/\" + orderId)", [("GET", "/api/orders/{orderId}")]),
    ("const shop = axios.create({ baseURL: BASE });\nshop.delete(`/api/carts/${cart.id}`)", [("DELETE", "/api/carts/{id}")]),
    ("useSWR('/api/me', fetcher)", [("GET", "/api/me")]),
    ("fetch('https://payments.example.com/v1/charge')", []),
    ("fetch(url)", []),
    ("map.get('/not-a-call'); params.get('id')", []),
    ("fetch('/logo.svg')", []),
    ("// fetch('/api/commented')", []),
])
def test_client_call_extraction(src: str, expected: list) -> None:
    assert _calls(src) == expected


def test_angular_http_client_with_environment_base() -> None:
    src = ("import { environment } from '../environments/environment';\n@Injectable()\nexport class CupService {\n"
           "  private entityUrl = environment.REST_API_URL + 'cups';\n  constructor(private http: HttpClient) {}\n"
           "  getCups(): Observable<Cup[]> { return this.http.get<Cup[]>(this.entityUrl); }\n"
           "  getCup(cupId: string) { return this.http.get<Cup>((this.entityUrl + '/') + cupId); }\n"
           "  addCup(cup: Cup) { return this.http.post<Cup>(this.entityUrl, cup); }\n}\n")
    assert _calls(src) == [("GET", "/cups"), ("GET", "/cups/{cupId}"), ("POST", "/cups")]


@pytest.mark.parametrize("method, path, expected", [
    ("GET", "/api/roasts", 0), ("POST", "/api/roasts/{x}/start", 2), ("GET", "/roasts/{id}", 1), ("GET", "/api/v1/roasts", 0),
    ("DELETE", "/api/roasts", None), ("GET", "/api/members/{id}", 3), ("GET", "/api/roasts/find", 4), ("GET", "/nothing", None),
])
def test_matching_tolerates_prefixes_params_and_case(method: str, path: str, expected) -> None:
    routes = [{"m": "GET", "p": "/api/roasts"}, {"m": "GET", "p": "/api/roasts/{id}"}, {"m": "POST", "p": "/api/roasts/{id}/start"},
              {"m": "ANY", "p": "/api/Members/{id:int}"}, {"m": "GET", "p": "/api/roasts/find"}]
    assert clientmatch.match(method, path, routes) == expected


def test_storefront_calls_are_matched_to_backend_routes(tmp_path: Path) -> None:
    cm = code_scan.scan([write(tmp_path, ROASTER, LOYALTY, STOREFRONT)])
    files = [f["p"] for f in cm.files]
    got = {(files[f].split("/")[-1], line, m, p, (cm.routes[r]["m"] + " " + cm.routes[r]["p"]) if r is not None else None)
           for f, line, m, p, r in cm.client_calls}
    assert got == {
        ("api.ts", 7, "GET", "/api/roasts", "GET /api/roasts"),
        ("api.ts", 11, "POST", "/api/roasts/{id}/start", "POST /api/roasts/{id}/start"),
        ("Members.tsx", 4, "GET", "/api/members/{memberId}", "GET /api/Members/{id:int}"),
        ("Members.tsx", 5, "DELETE", "/api/members/{memberId}", "DELETE /api/Members/{id}"),
        ("Members.tsx", 6, "POST", "/api/rewards/{code}/redeem", "POST /api/rewards/{code}/redeem"),
        ("Members.tsx", 7, "GET", "/api/gift-cards", None),
    }


def test_backend_js_services_are_not_frontends(tmp_path: Path) -> None:
    write(tmp_path, {"api/package.json": "{}", "api/server.js": "app.get('/a', h);\nfetch('/api/elsewhere');\n"})
    assert code_scan.scan([tmp_path]).client_calls == []


def test_angular_inject_and_plain_class_fields() -> None:
    src = ("export class BeanService {\n  private http = inject(HttpClient);\n  entityUrl = environment.REST_API_URL + 'beans';\n"
           "  getBean(id: number) {\n    return this.http\n      .get<Bean>(this.entityUrl + '/' + id);\n  }\n}\n")
    assert _calls(src) == [("GET", "/beans/{id}")]


def test_express_backend_in_a_web_folder_keeps_its_routes(tmp_path: Path) -> None:
    write(tmp_path, {"web/package.json": '{"dependencies": {"express": "4"}}', "web/server.js": "api.get('/beans', list);\nfetch('/api/elsewhere');\n"})
    cm = code_scan.scan([tmp_path])
    assert [(r["m"], r["p"]) for r in cm.routes] == [("GET", "/beans")]
    assert [c[2:4] for c in cm.client_calls] == [["GET", "/api/elsewhere"]]


# ---- 0.6.0 review: query strings glued to the path, instance base URLs, suffix fallback, Express mounts ----

@pytest.mark.parametrize("src, expected", [
    ("fetch(`/api/v1/agents${qs}`)", [("GET", "/api/v1/agents")]),
    ("const base = API_URL;\nfetch(`${base}/orders/${id}/items${query}`)", [("GET", "/orders/{id}/items")]),
    ("fetch('/api/v1/agents' + qs)", [("GET", "/api/v1/agents")]),
    ("fetch(`/api/v1/agents?${params}`)", [("GET", "/api/v1/agents")]),
    ("fetch('/api/orders/' + orderId)", [("GET", "/api/orders/{orderId}")]),
])
def test_query_suffixes_are_dropped(src: str, expected: list) -> None:
    assert _calls(src) == expected


BARISTA = {
    "shop-api/package.json": '{"dependencies": {"express": "4"}}',
    "shop-api/src/app.js": ("const express = require('express');\nimport statsRouter from './routes/stats';\nconst app = express();\n"
                            "app.use('/api/v1/stats', statsRouter);\napp.use('/api/v1', require('./routes/agents'));\n"),
    "shop-api/src/routes/stats.js": "const router = express.Router();\nrouter.get('/total-templates', h);\nmodule.exports = router;\n",
    "shop-api/src/routes/agents.js": "const router = express.Router();\nrouter.get('/agents', h);\nrouter.post('/agents/:id/pause', p);\n",
    "shop-api/src/routes/beans.js": "router.get('/beans/house-blend/stock', s);\n",
    "barista-admin/package.json": '{"dependencies": {"react": "18"}}',
    "barista-admin/src/lib/http.ts": ("import axios from 'axios';\nexport const statsApi = axios.create({ baseURL: `${import.meta.env.VITE_API_URL}/api/v1/stats` });\n"
                                      "const shop = axios.create({ baseURL: 'https://shop.example.com/api/v1' });\nexport default shop;\n"
                                      "export function apiFetch(path: string, init?: RequestInit) {\n  return fetch(`${API_URL}/api/v1${path}`, init);\n}\n"),
    "barista-admin/src/pages/Dashboard.tsx": ("import shop, { statsApi, apiFetch } from '../lib/http';\n"
                                             "export function Dashboard() {\n  statsApi.get('/total-templates');\n  shop.get(`/agents${qs}`);\n"
                                             "  apiFetch(`/agents/${id}/pause`, { method: 'POST' });\n  fetch('/house-blend/stock');\n  fetch('/stock');\n}\n"),
}


def test_instance_bases_wrappers_suffix_fallback_and_express_mounts(tmp_path: Path) -> None:
    cm = code_scan.scan([write(tmp_path, BARISTA)])
    assert {(r["m"], r["p"]) for r in cm.routes} == {("GET", "/api/v1/stats/total-templates"), ("GET", "/api/v1/agents"),
                                                     ("POST", "/api/v1/agents/:id/pause"), ("GET", "/beans/house-blend/stock")}
    got = {(c[1], c[2], c[3], cm.routes[c[4]]["p"] if c[4] is not None else None, c[5] if len(c) > 5 else "") for c in cm.client_calls}
    assert got == {
        (3, "GET", "/api/v1/stats/total-templates", "/api/v1/stats/total-templates", ""),
        (4, "GET", "/api/v1/agents", "/api/v1/agents", ""),
        (5, "POST", "/api/v1/agents/{id}/pause", "/api/v1/agents/:id/pause", ""),
        (6, "GET", "/house-blend/stock", "/beans/house-blend/stock", "suffix"),
        (7, "GET", "/stock", None, ""),  # too short to be sure: one segment
    }


def test_suffix_fallback_needs_exactly_one_candidate() -> None:
    routes = [{"m": "GET", "p": "/ops/stats/total"}, {"m": "GET", "p": "/barista/stats/total"}, {"m": "GET", "p": "/api/v1/cups/{id}/beans"}]
    assert clientmatch.match_with_basis("GET", "/stats/total", routes) == (None, "")
    assert clientmatch.match_with_basis("GET", "/{x}/beans", routes) == (2, "suffix")
    assert clientmatch.match_with_basis("POST", "/{x}/beans", routes) == (None, "")


def test_empty_default_field_is_a_param() -> None:
    src = "class Cupping {\n  sessionId: string = '';\n  load() { this.http.get(`/api/session/${this.sessionId}/notes`); }\n}\n"
    assert _calls(src) == [("GET", "/api/session/{sessionId}/notes")]
