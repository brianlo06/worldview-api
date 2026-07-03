"""Game spine + SCAN module (jarvisworlds.com/game).

Layout:
  logic.py    — pure functions (rolls, pity, streaks, allowances); no I/O
  identity.py — anonymous player tokens + FastAPI auth dependency
  rates.py    — rate-table config loader (live-tunable rows in game_rate_tables)
  wallet.py   — daily grant / streak persistence helpers
  mint.py     — daily card-pool minting job
"""
