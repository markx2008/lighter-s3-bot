#!/usr/bin/env python3
"""standx_jwt_tool.py

Helper CLI to obtain and store StandX JWT via wallet signature (BSC/EVM).

Workflow (recommended):
1) Prepare sign-in message:
   python3 standx_jwt_tool.py prepare --address 0xYourAddress
   -> prints the EXACT message to sign + stores signedData JWT in STATE_PATH

2) Sign the message with your wallet (Binance Web3 Wallet / MetaMask / etc)
   using personal_sign / Sign Message.
   -> get a signature string (usually 0x...)

3) Complete login and store access token:
   python3 standx_jwt_tool.py login --signature 0x...

4) Check status:
   python3 standx_jwt_tool.py status

Notes:
- No private key is required on this machine.
- The wallet does the signing; you just paste the signature.

Env:
  STANDX_CHAIN=bsc
  STANDX_API_BASE=https://api.standx.com
  STATE_PATH=/home/mark/.openclaw/workspace/state_standx_jwt.json

"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from datetime import datetime, timezone

from standx_auth import StandXAuthConfig, jwt_payload, login, prepare_signin

STATE_PATH = os.getenv("STATE_PATH", "/home/mark/.openclaw/workspace/state_standx_jwt.json")


def load_state() -> dict:
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(st: dict) -> None:
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_PATH)


def cmd_prepare(args) -> int:
    cfg = StandXAuthConfig(api_base=args.api_base, chain=args.chain)
    req_id = args.request_id or str(uuid.uuid4())
    res = prepare_signin(args.address, request_id=req_id, cfg=cfg)
    if not res.get("success"):
        print("prepare-signin failed:", res)
        return 1
    sd = res["signedData"]
    pl = jwt_payload(sd)
    msg = pl.get("message")
    if not msg:
        print("No message in signedData payload")
        return 1

    st = load_state()
    st["pending"] = {
        "address": args.address,
        "chain": args.chain,
        "request_id": req_id,
        "signedData": sd,
        "issuedAt": pl.get("issuedAt"),
        "chainId": pl.get("chainId"),
    }
    save_state(st)

    print("=== StandX Sign-in Message (SIGN THIS EXACT TEXT) ===\n")
    print(msg)
    print("\n=== End Message ===")
    print("\nAfter signing, run:\n  python3 standx_jwt_tool.py login --signature 0x...\n")
    return 0


def cmd_login(args) -> int:
    st = load_state()
    pend = st.get("pending")
    if not pend or not pend.get("signedData"):
        print("No pending signedData. Run prepare first.")
        return 1

    cfg = StandXAuthConfig(api_base=args.api_base, chain=pend.get("chain") or args.chain)
    res = login(args.signature, pend["signedData"], cfg=cfg)

    # We don't assume response shape; store full response
    st["jwt"] = res
    st["jwt_saved_at"] = datetime.now(timezone.utc).isoformat()
    st["pending"] = None
    save_state(st)

    print("Login response saved to", STATE_PATH)
    print(json.dumps(res, ensure_ascii=False, indent=2)[:2000])
    return 0


def cmd_status(_args) -> int:
    st = load_state()
    print("STATE_PATH:", STATE_PATH)
    if st.get("pending"):
        p = st["pending"]
        print("pending: yes (address=%s request_id=%s)" % (p.get("address"), p.get("request_id")))
    else:
        print("pending: no")

    jwt = st.get("jwt")
    if not jwt:
        print("jwt: none")
        return 0

    # try locate token-like fields
    token = jwt.get("accessToken") or jwt.get("access_token") or jwt.get("token") or jwt.get("jwt")
    if token:
        print("jwt: present (token field found)")
    else:
        print("jwt: present (unknown shape; open the json)")
    print("saved_at:", st.get("jwt_saved_at"))
    return 0


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    p1 = sub.add_parser("prepare")
    p1.add_argument("--address", required=True)
    p1.add_argument("--chain", default=os.getenv("STANDX_CHAIN", "bsc"))
    p1.add_argument("--api-base", default=os.getenv("STANDX_API_BASE", "https://api.standx.com"))
    p1.add_argument("--request-id")
    p1.set_defaults(fn=cmd_prepare)

    p2 = sub.add_parser("login")
    p2.add_argument("--signature", required=True)
    p2.add_argument("--chain", default=os.getenv("STANDX_CHAIN", "bsc"))
    p2.add_argument("--api-base", default=os.getenv("STANDX_API_BASE", "https://api.standx.com"))
    p2.set_defaults(fn=cmd_login)

    p3 = sub.add_parser("status")
    p3.set_defaults(fn=cmd_status)

    args = ap.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
