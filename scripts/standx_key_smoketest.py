#!/usr/bin/env python3
from __future__ import annotations

import json

from standx.config.runtime import RuntimeConfig
from standx.integrations.client import StandXClient, StandXConfig
from standx.integrations.signing import parse_ed25519_private_key, sign_request


def main() -> None:
    cfg = RuntimeConfig.from_env()
    print("base_url", cfg.standx_base_url)
    print("symbol", cfg.strategy.symbol)

    # local checks
    jwt_parts = [p for p in (cfg.standx_jwt or "").split(".") if p]
    print("jwt_parts", len(jwt_parts))

    seed = parse_ed25519_private_key(cfg.standx_ed25519_privkey)
    print("ed25519_seed_len", len(seed))

    # safe API checks
    client = StandXClient(StandXConfig(base_url=cfg.standx_base_url, jwt=cfg.standx_jwt or None))
    t = client.server_time()
    print("server_time", t)

    px = client.symbol_price(cfg.strategy.symbol)
    print("symbol_price", json.dumps(px, ensure_ascii=False))

    # signed query checks (if endpoints exist)
    sign_headers = sign_request('{"ping":1}', seed)
    for name, fn in [
        ("query_positions", lambda: client.query_positions(sign_headers=sign_headers)),
        ("query_orders", lambda: client.query_orders(sign_headers=sign_headers)),
    ]:
        try:
            res = fn()
            print(name, "ok", json.dumps(res, ensure_ascii=False)[:1000])
        except Exception as exc:
            print(name, "failed", type(exc).__name__, str(exc)[:200])


if __name__ == "__main__":
    main()
