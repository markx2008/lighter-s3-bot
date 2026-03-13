from __future__ import annotations

import logging
import os
import time

# Timezone is configured at container level via Dockerfile (TZ=Asia/Taipei).

from standx.apps.monitor import build_dependencies as build_monitor_dependencies
from standx.apps.monitor import run_once as run_monitor_once
from standx.apps.trader import build_dependencies as build_trader_dependencies
from standx.apps.trader import run_once as run_trader_once
from standx.config.runtime import RuntimeConfig
from standx.integrations.client import StandXClient, StandXConfig
from standx.integrations.signing import parse_ed25519_private_key, sign_request
from standx.integrations.telegram import TelegramNotifier


def _startup_diagnostics(config: RuntimeConfig) -> list[str]:
    """Run lightweight diagnostics for startup notifications.

    - Never places orders
    - Avoids leaking secrets
    """
    lines: list[str] = []

    # --- runtime persistence check ---
    try:
        state_dir = os.path.dirname(config.state_path) or "."
        st = os.stat(state_dir)
        lines.append(f"✅ runtime 目錄可存取：{state_dir}")
        # Try create+delete a small probe file to verify write permissions.
        probe_path = os.path.join(state_dir, ".standx_runtime_probe")
        with open(probe_path, "w", encoding="utf-8") as f:
            f.write("ok")
        os.remove(probe_path)
        lines.append("✅ runtime 可寫入：OK")
    except Exception as exc:
        lines.append(f"❌ runtime 寫入檢查失敗：{type(exc).__name__}")

    # --- StandX key sanity checks (local only) ---
    if config.live:
        # JWT shape check (header.payload.signature)
        jwt_parts = [p for p in (config.standx_jwt or "").split(".") if p]
        if len(jwt_parts) == 3:
            lines.append("✅ STANDX_JWT 格式：正常")
        else:
            lines.append("⚠️ STANDX_JWT 格式：異常")

        # ED25519 key parse check
        try:
            _ = parse_ed25519_private_key(config.standx_ed25519_privkey)
            # signing smoke test (no network)
            _ = sign_request('{"ping":1}', parse_ed25519_private_key(config.standx_ed25519_privkey))
            lines.append("✅ STANDX_ED25519_PRIVKEY：載入正常")
        except Exception as exc:
            lines.append(f"❌ STANDX_ED25519_PRIVKEY：無效（{type(exc).__name__}）")

    # --- StandX API reachability (safe GETs) ---
    try:
        client = StandXClient(StandXConfig(base_url=config.standx_base_url, jwt=config.standx_jwt or None))
        t = client.server_time()
        lines.append(f"✅ StandX API 連線正常：server_time={t}")
        try:
            px = client.symbol_price(config.strategy.symbol)
            if isinstance(px, dict):
                mp = px.get("mark_price") or px.get("last_price") or px.get("mid_price")
                if mp is not None:
                    lines.append(f"✅ {config.strategy.symbol} 價格取得正常：{mp}")
        except Exception:
            # Price endpoint may vary; don't fail startup.
            lines.append(f"⚠️ {config.strategy.symbol} 價格檢查失敗")
    except Exception as exc:
        lines.append(f"❌ StandX API 檢查失敗：{type(exc).__name__}")

    return lines


def _safe_startup_notify(config: RuntimeConfig) -> None:
    """Send a one-time startup message (useful for Docker/CapRover restarts)."""
    try:
        msg_lines = [
            "🟢 StandX S3 Bot 已啟動",
            config.startup_summary(),
            f"STATE_PATH={config.state_path}",
        ]
        msg_lines.extend(_startup_diagnostics(config))
        TelegramNotifier(config).send("\n".join(msg_lines))
    except Exception:
        logging.getLogger(__name__).exception("coordinator.startup_notify_failed")


def main() -> None:
    # Docker already prefixes logs with its own timestamp; avoid double timestamps.
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    config = RuntimeConfig.from_env()
    config.validate()
    logging.info("coordinator.start %s", config.startup_summary())
    _safe_startup_notify(config)
    trader_deps = build_trader_dependencies(config)

    def _next_aligned_run(now_s: float, interval_s: int, offset_s: int = 1) -> float:
        """Return next run time aligned to interval boundaries + offset.

        For 15m candles, we want 00/15/30/45 + offset_s.
        """
        interval_s = max(60, int(interval_s))
        next_boundary = ((int(now_s) // interval_s) + 1) * interval_s
        return float(next_boundary + offset_s)

    next_trader_run = _next_aligned_run(time.time(), config.trader_every_sec, offset_s=1)
    next_heartbeat_run = time.time() + 3600

    while True:
        now = time.time()
        if now >= next_trader_run:
            try:
                logging.info("coordinator.tick job=trader symbol=%s", config.strategy.symbol)
                run_trader_once(trader_deps)
            except Exception:
                logging.exception("coordinator.tick_failed job=trader symbol=%s", config.strategy.symbol)
            next_trader_run = _next_aligned_run(time.time(), config.trader_every_sec, offset_s=1)

        # Heartbeat: log only.
        if now >= next_heartbeat_run:
            try:
                px = StandXClient(StandXConfig(base_url=config.standx_base_url, jwt=config.standx_jwt or None)).symbol_price(config.strategy.symbol)
                mp = px.get("mark_price") if isinstance(px, dict) else None
                logging.info("heartbeat alive symbol=%s mark=%s", config.strategy.symbol, mp)
            except Exception:
                logging.getLogger(__name__).exception("coordinator.heartbeat_failed")
            next_heartbeat_run = time.time() + 3600

        time.sleep(0.2)


if __name__ == "__main__":
    main()
