"""Which code folder each Lambda function runs, so a serverless route traces through API Gateway and its function.

Names only, and strict: a folder is placed on a function when every word of the folder's name is in the function's name
and the function's name has no other words than those, environment words and the prefix most functions share (the
company or stack name): `kestrel-orders-api` runs `orders-api`, `shop-api-warmer` runs nothing. Folders already placed on
an ECS service or Kubernetes workload are left alone. hopscope.json `placement` entries named `lambda:<function>` set or
(with null) remove a link. Keys are function names as aws.json has them (with a scope prefix in a merged inventory)."""
from __future__ import annotations

from collections import Counter
from pathlib import Path

from scopes import plain

CONFIDENCE = 0.8


def lambda_links(code: dict, inv: dict, placement: dict[str, dict], overrides: dict | None = None) -> dict[str, dict]:
    from linker import GENERIC, resolve_folder, service_of, tokens  # imported here: linker imports this module

    services = list(code.get("services") or [])
    with_files = {service_of(str(f.get("p", "")), services) for f in code.get("files") or []}
    taken = {p.get("code") for p in placement.values()}
    folders = {s: tokens(Path(s).name) for s in services if s in with_files and s not in taken and s != "."}
    folders = {s: t for s, t in folders.items() if t and not t <= GENERIC}
    fns = [str(f.get("n")) for f in inv.get("lambda") or [] if isinstance(f, dict) and f.get("n")]
    fn_tokens = {n: tokens(plain(n)) for n in fns}
    common = Counter(t for ts in fn_tokens.values() for t in ts)
    prefix = {t for t, c in common.items() if c >= max(2, 0.4 * len(fns))} if len(fns) >= 3 else set()  # a shared prefix needs a few functions
    out: dict[str, dict] = {}
    for n, ft in fn_tokens.items():
        fits = [s for s, st in folders.items() if st <= ft and not (ft - st - prefix)]
        if len(fits) == 1:
            out[n] = {"code": fits[0], "confidence": CONFIDENCE, "evidence": [f"function name {plain(n)} names the folder {Path(fits[0]).name}"]}
    for key, folder in (overrides or {}).items():
        if not str(key).startswith("lambda:"):
            continue
        n = str(key)[len("lambda:"):]
        if folder is None:
            out.pop(n, None)
        elif (s := resolve_folder(str(folder), services)) and n in fn_tokens:
            out[n] = {"code": s, "confidence": 1.0, "evidence": ["set in hopscope.json"]}
    return out
