"""Rust: axum routers (route, nest with a local router, an inline router, or a fn returning one in another file), actix-web
attribute macros, scopes and resources, Rocket mounts, `mod`/`use` imports, externals, and handler call flows. Plover
Post is invented."""
from __future__ import annotations

from pathlib import Path

import pytest

import code_scan
from kestrel_fixtures import write
from langs import rust

A = "plover-axum"
X = "plover-actix"

PLOVER = {
    f"{A}/Cargo.toml": ('[package]\nname = "plover-axum"\n\n[dependencies]\naxum = "0.7"\nsqlx = { version = "0.7", features = ["postgres", "runtime-tokio"] }\n'
                        'redis = "0.25"\naws-sdk-sqs = "1"\nreqwest = "0.12"\n'),
    f"{A}/target/debug/build/junk.rs": 'fn junk() { Router::new().route("/junk", get(j)); }\n',
    f"{A}/tests/api.rs": 'fn t() { Router::new().route("/test-only", get(t)); }\n',
    f"{A}/src/main.rs": '''mod db;
mod routes;

use axum::{routing::{get, post}, Router};

#[tokio::main]
async fn main() {
    let api = Router::new()
        .route("/parcels", get(routes::parcels::list).post(routes::parcels::create))
        .route("/parcels/:id", get(routes::parcels::show).delete(routes::parcels::remove));
    let app = Router::new()
        .route("/health", get(|| async { "ok" }))
        .nest("/api", api)
        .nest("/v2", Router::new().route("/labels", post(routes::parcels::create)))
        .nest("/admin", routes::admin::router());
    // .route("/commented", get(nope))
    axum::serve(listener, app).await.unwrap();
}
''',
    f"{A}/src/db.rs": '''use sqlx::PgPool;

pub struct Store;

impl Store {
    pub async fn insert_parcel(pool: &PgPool, p: &NewParcel) -> sqlx::Result<i64> {
        let rec = sqlx::query!("INSERT INTO parcel (label) VALUES ($1) RETURNING id", p.label)
            .fetch_one(pool)
            .await?;
        Ok(rec.id)
    }
}
''',
    f"{A}/src/routes/mod.rs": "pub mod admin;\npub mod parcels;\n",
    f"{A}/src/routes/admin.rs": '''use axum::{routing::post, Router};

pub fn router() -> Router<AppState> {
    Router::new().route("/purge", post(purge))
}

async fn purge() {}
''',
    f"{A}/src/routes/parcels.rs": '''use crate::db::Store;
use aws_sdk_sqs::Client as SqsClient;
use axum::extract::{Path, State};

pub struct AppState {
    pub pool: PgPool,
    pub sqs: SqsClient,
    pub http: reqwest::Client,
    pub redis: redis::Client,
}

pub async fn list(State(pool): State<PgPool>) -> Json<Vec<Parcel>> {
    let rows = sqlx::query_as!(Parcel, r#"SELECT id, label FROM parcel WHERE label <> 'x"y'"#)
        .fetch_all(&pool)
        .await
        .unwrap();
    Json(rows)
}

/// Accept a parcel: store it, queue express ones, tell the ledger.
pub async fn create(State(state): State<AppState>, Json(body): Json<NewParcel>) -> StatusCode {
    let id = Store::insert_parcel(&state.pool, &body).await.unwrap();
    if body.express_delivery {
        state.sqs.send_message().queue_url("q").message_body(id.to_string()).send().await.unwrap();
    }
    match notify(&state.http, id).await {
        Ok(_) => {}
        Err(e) => {
            let mut con = state.redis.get_connection().unwrap();
            redis::cmd("SET").arg("notify:failed").query::<()>(&mut con).unwrap();
        }
    }
    StatusCode::CREATED
}

async fn notify<'a>(http: &'a reqwest::Client, id: i64) -> Result<(), reqwest::Error> {
    let c = '{';
    http.post("http://ledger/api/entries").send().await?;
    Ok(())
}

pub async fn show(Path(id): Path<i64>) -> String {
    format!("{id}")
}

pub async fn remove(Path(id): Path<i64>) -> StatusCode {
    StatusCode::NO_CONTENT
}
''',
    f"{X}/Cargo.toml": ('[package]\nname = "plover-actix"\n\n[dependencies]\nactix-web = "4"\ndiesel = { version = "2", features = ["mysql"] }\n'
                        'rdkafka = "0.36"\n'),
    f"{X}/src/main.rs": '''use actix_web::{web, App, HttpServer};
mod handlers;

#[actix_web::main]
async fn main() -> std::io::Result<()> {
    HttpServer::new(|| {
        App::new()
            .service(handlers::ping)
            .service(
                web::scope("/v1")
                    .service(handlers::track)
                    .service(web::resource("/routes/{id}").route(web::get().to(handlers::route_info)).route(web::put().to_async(handlers::reroute))),
            )
            .route("/status", web::get().to(handlers::status))
    })
    .bind(("0.0.0.0", 8080))?
    .run()
    .await
}
''',
    f"{X}/src/handlers.rs": '''use actix_web::{get, post, web, HttpResponse};
use diesel::prelude::*;
use rdkafka::producer::{FutureProducer, FutureRecord};

#[get("/ping")]
async fn ping() -> HttpResponse {
    HttpResponse::Ok().finish()
}

#[post("/track/{id}")]
async fn track(path: web::Path<i64>, producer: web::Data<FutureProducer>) -> HttpResponse {
    let rows = routes::table.filter(routes::id.eq(*path)).load::<Route>(&mut conn);
    producer.send(FutureRecord::to("tracking").payload("x").key("k"), Duration::from_secs(0)).await;
    HttpResponse::Ok().finish()
}

pub async fn route_info() -> HttpResponse {
    HttpResponse::Ok().finish()
}

pub async fn reroute() -> HttpResponse {
    HttpResponse::Ok().finish()
}

pub async fn status() -> HttpResponse {
    HttpResponse::Ok().finish()
}
''',
    "plover-rocket/Cargo.toml": '[package]\nname = "plover-rocket"\n\n[dependencies]\nrocket = "0.5"\n',
    "plover-rocket/src/main.rs": '''#[macro_use]
extern crate rocket;

#[get("/<id>")]
fn stamp(id: u32) -> String {
    id.to_string()
}

#[post("/", data = "<form>")]
fn issue(form: Form<Stamp>) -> &'static str {
    "ok"
}

#[launch]
fn rocket() -> _ {
    rocket::build().mount("/stamps", routes![stamp, issue])
}
''',
}


@pytest.fixture()
def cm(tmp_path: Path):
    return code_scan.scan([write(tmp_path, PLOVER)])


def _routes(cm) -> set[tuple[str, str]]:
    return {(r["m"], r["p"]) for r in cm.routes}


def _files(cm) -> list[str]:
    return [f["p"] for f in cm.files]


def test_axum_routes_with_nested_routers(cm) -> None:
    assert {("GET", "/api/parcels"), ("POST", "/api/parcels"), ("GET", "/api/parcels/:id"), ("DELETE", "/api/parcels/:id"),
            ("GET", "/health"), ("POST", "/v2/labels"), ("POST", "/admin/purge")} <= _routes(cm)


def test_actix_macros_scopes_and_resources(cm) -> None:
    assert {("GET", "/ping"), ("POST", "/v1/track/{id}"), ("GET", "/v1/routes/{id}"), ("PUT", "/v1/routes/{id}"), ("GET", "/status")} <= _routes(cm)


def test_rocket_mounts(cm) -> None:
    assert {("GET", "/stamps/<id>"), ("POST", "/stamps")} <= _routes(cm)


def test_no_routes_from_comments_tests_or_target(cm) -> None:
    paths = {p for _, p in _routes(cm)}
    assert not paths & {"/commented", "/test-only", "/junk", "/api/commented"}
    assert len(cm.routes) == 14


def test_route_points_at_the_handler_fn(cm) -> None:
    files = _files(cm)
    create = next(r for r in cm.routes if r["m"] == "POST" and r["p"] == "/api/parcels")
    assert files[create["f"]] == f"{A}/src/routes/parcels.rs" and create["l"] == 21
    assert create["t"] == "Create" and create["d"] == "Accept a parcel: store it, queue express ones, tell the ledger."
    health = next(r for r in cm.routes if r["p"] == "/health")
    assert files[health["f"]] == f"{A}/src/main.rs" and health["l"] == 12
    status = next(r for r in cm.routes if r["p"] == "/status")
    assert files[status["f"]] == f"{X}/src/handlers.rs"


def test_services_are_cargo_folders(cm) -> None:
    assert cm.services == [X, A, "plover-rocket"]


def test_mod_and_use_imports(cm) -> None:
    idx = {f: i for i, f in enumerate(_files(cm))}
    links = {(a, b) for a, b, _, _ in cm.links}
    main = idx[f"{A}/src/main.rs"]
    assert (main, idx[f"{A}/src/db.rs"]) in links
    assert (main, idx[f"{A}/src/routes/mod.rs"]) in links
    assert (main, idx[f"{A}/src/routes/parcels.rs"]) in links
    assert (idx[f"{A}/src/routes/mod.rs"], idx[f"{A}/src/routes/admin.rs"]) in links
    assert (idx[f"{A}/src/routes/parcels.rs"], idx[f"{A}/src/db.rs"]) in links
    assert (idx[f"{X}/src/main.rs"], idx[f"{X}/src/handlers.rs"]) in links


def test_externals(cm) -> None:
    x = {f["p"]: set(f["x"]) for f in cm.files}
    assert {"postgres", "sqs", "cache", "http"} <= x[f"{A}/src/routes/parcels.rs"]
    assert "postgres" in x[f"{A}/src/db.rs"]
    assert {"mysql", "kafka"} <= x[f"{X}/src/handlers.rs"]


def _flow(data: dict, method: str, path: str) -> list[tuple]:
    i = next(i for i, r in enumerate(data["routes"]) if r["m"] == method and r["p"] == path)
    steps = rust.flows(data, Path(data["base"]), i)
    assert steps is not None
    return [(s[0], s[1], s[2], s[5], s[6]) for s in steps]


def test_handler_flows(tmp_path: Path) -> None:
    cm = code_scan.scan([write(tmp_path, PLOVER)])
    data = {"base": str(tmp_path), "files": cm.files, "routes": cm.routes, "services": cm.services}
    assert _flow(data, "POST", "/api/parcels") == [
        (0, "call", "insert_parcel()", 0, ""),
        (1, "sql", "Postgres: INSERT parcel", 0, ""),
        (0, "aws", "SQS: SendMessage", 1, "express delivery is set"),
        (0, "call", "notify()", 0, ""),
        (1, "http", "HTTP POST http://ledger/api/entries", 0, ""),
        (0, "cache", "Cache: SET", 2, "it fails"),
    ]
    assert _flow(data, "GET", "/api/parcels") == [(0, "sql", "Postgres: SELECT parcel", 0, "")]
    assert _flow(data, "POST", "/v1/track/{id}") == [(0, "sql", "MySQL: SELECT routes", 0, ""), (0, "kafka", "Kafka: send to tracking", 0, "")]


def test_workflow_stream_and_kafka_clients() -> None:
    src = "use aws_sdk_sfn::Client as Sfn;\nuse aws_sdk_kinesis::Client;\nuse rdkafka::producer::FutureProducer;\n"
    assert {"sfn", "kinesis", "kafka"} <= rust.externals(src)
