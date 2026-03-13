# Lighter S3 Bot

Strategy3 (Supertrend flip) trading bot skeleton adapted from an internal StandX S3 bot.

This repository is intended to be open-sourced. It contains:
- strategy / risk / scheduling scaffolding
- Telegram notifications
- placeholders for a Lighter exchange integration

## Status

- ✅ Project skeleton created
- 🚧 Lighter integration (auth / market data / order placement) is not implemented yet

## Quick start

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m standx.apps.coordinator
```

## Configuration

Copy `.env.example` and fill in your own values.

## License

Apache-2.0
