"""Go routes: gin/echo/chi-style calls, net/http HandleFunc, and gorilla/mux HandleFunc(...).Methods(...) with a path prefix
variable. Invented Kestrel fixtures."""
from __future__ import annotations

from pathlib import Path

from langs import go

MUX = '''package main

const apiPrefix = "/api/v1"

var baseUrl = ""

func init() { baseUrl = os.Getenv("BASE_URL") }

func routes(r *mux.Router) {
	r.HandleFunc(baseUrl+"/", svc.home).Methods(http.MethodGet, http.MethodHead)
	r.HandleFunc(baseUrl+"/beans/{id}", svc.bean).Methods("GET")
	r.HandleFunc(apiPrefix + "/cart", svc.addToCart).Methods(http.MethodPost)
	r.HandleFunc("/healthz", svc.health)
	r.HandleFunc("/roasts", svc.roasts).Methods("GET", "POST")
	r.PathPrefix("/static/").Handler(fs)
}
'''


def _routes(text: str) -> list[tuple[str, str]]:
    return [(r["m"], r["p"]) for r in go.routes(Path("main.go"), text)]


def test_gorilla_mux_methods_and_prefix_variables() -> None:
    assert _routes(MUX) == [("GET", "{?}/"), ("GET", "{?}/beans/{id}"), ("POST", "/api/v1/cart"), ("ANY", "/healthz"), ("GET", "/roasts"), ("POST", "/roasts")]


def test_gin_routes_unchanged() -> None:
    assert _routes('r.GET("/menu", h)\nr.POST("/orders", h)\n') == [("GET", "/menu"), ("POST", "/orders")]
