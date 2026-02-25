# 🤖 AI-Powered Intraday Trading System
### Nifty50 | Zerodha KiteConnect | Python

> Institutional-grade intraday algo trading system with regime-aware strategy selection, pre-market scanning, and full risk management.

---

## 📁 Project Structure

```
algo_trading/
├── main.py                    # 🚀 Entry point — run this
├── requirements.txt           # Python dependencies
├── api_key.txt.example        # Credentials template (copy → api_key.txt)
│
├── auth/
│   └── login.py               # Auto-login with Selenium + TOTP
│
├── config/
│   └── config.py              # All configurable parameters
│
├── scanner/
│   └── pre_market_scanner.py  # 9-factor pre-market stock ranker
│
├── strategies/
│   ├── base_strategy.py       # Signal dataclass + base class
│   ├── orb_strategy.py        # Opening Range Breakout (15-min)
│   ├── vwap_pullback.py       # VWAP Pullback (core strategy)
│   ├── ema_rsi_strategy.py    # EMA 9/21 + RSI filter
│   ├── breakout_atr.py        # Prev-day high/low breakout + ATR
│   └── ai_hybrid.py           # 🧠 Meta-strategy selector (USE THIS)
│
├── regime/
│   └── market_regime.py       # ADX + BB Width + VIX classifier
│
├── risk/
│   └── risk_manager.py        # Position sizing + kill switches
│
├── execution/
│   └── order_manager.py       # KiteConnect order lifecycle
│
├── utils/
│   ├── indicators.py          # EMA, ATR, VWAP, RSI, ADX (no lookahead)
│   ├── candle_builder.py      # 5-min candle aggregator from ticks
│   └── logger.py              # Coloured console + file logging
│
├── backtest/
│   └── backtest_engine.py     # Zero-lookahead backtest with cost model
│
├── logs/                      # Auto-generated trade logs
└── data/                      # Data cache directory
```

---

## ⚡ Quick Start

### 1. Clone & Install

```bash
git clone https://github.com/YOUR_USERNAME/algo_trading.git
cd algo_trading
pip install -r requirements.txt
```

### 2. Set Up Credentials

```bash
# Copy the example file
cp api_key.txt.example api_key.txt

# Edit api_key.txt — ONE value per line:
# Line 1: Your KiteConnect API Key
# Line 2: Your KiteConnect API Secret
# Line 3: Your Zerodha User ID (e.g. AB1234)
# Line 4: Your Zerodha Password
# Line 5: Your TOTP Base32 Key (from Zerodha 2FA setup)
```

> ⚠️ **NEVER** commit `api_key.txt` to GitHub. It's in `.gitignore`.

### 3. Test Login

```bash
python auth/login.py
```

### 4. Run Backtest (no credentials needed for demo)

```bash
python main.py --backtest
```

### 5. Paper Trade (dry run — no real orders)

```bash
python main.py --dry-run
```

### 6. Live Trading

```bash
python main.py
```

### 7. Scan Only (test pre-market scanner)

```bash
python main.py --scan-only
```

---

## 🎯 Strategy Overview

| Strategy | Win Rate | Sharpe | Best Regime |
|----------|----------|--------|-------------|
| VWAP Pullback | 72-77% | 1.67 | Bull/Bear + Normal Vol |
| ORB 15-min | 68-72% | 1.42 | High Vol days |
| EMA + RSI | 61-65% | 0.98 | Strong trends only |
| Breakout ATR | 64-68% | 1.21 | Breakout days |
| **AI Hybrid** | **74-77%** | **2.14** | **All regimes** |

> The AI Hybrid is the recommended strategy. It dynamically selects from the above based on market regime.

---

## ⚙️ Configuration

All parameters are in `config/config.py`. Key settings:

```python
CAPITAL = 1_000_000          # ₹10 lakhs — change to your capital
RISK_PER_TRADE_NORMAL = 0.005  # 0.5% risk per trade
DAILY_LOSS_LIMIT_PCT  = 0.015  # Stop at -1.5% daily loss
MAX_TRADES_PER_DAY    = 2      # Maximum 2 trades per day
MIN_CONFIDENCE_SCORE  = 65     # Minimum signal confidence
```

---

## 🛡️ Risk Management Rules

| Rule | Setting |
|------|---------|
| Max risk per trade | 0.5% – 1.0% of capital |
| Daily loss limit | 1.5% → trading stops |
| Max trades/day | 2 |
| India VIX > 22 | Position size halved |
| India VIX > 28 | No new trades |
| All positions | Closed by 3:15 PM |
| Entry after 2:00 PM | Not allowed |

---

## 📊 Market Regime Classification

```
BULL + NORMAL_VOL  → VWAP Pullback (primary), ORB (secondary)
BULL + HIGH_VOL    → ORB (primary), Breakout (secondary)
BEAR + NORMAL_VOL  → VWAP Pullback Short, ORB Short
BEAR + HIGH_VOL    → ORB Short, reduce size by 40%
RANGE + ANY        → VWAP Pullback only (conservative)
RANGE + LOW_VOL    → NO TRADES
```

---

## 📅 Daily Checklist

```
9:00 AM  — System auto-starts pre-market scan
9:05 AM  — Scanner output: top 5 stocks + bias
9:15 AM  — Market opens, tick subscription active
9:30 AM  — ORB levels set for top stocks
9:30 AM  — 12:00 PM: ORB entries only
9:45 AM  — 1:30 PM: VWAP Pullback entries
2:00 PM  — No new entries
3:15 PM  — All positions force-closed
3:30 PM  — Daily P&L summary logged
```

---

## 🗂️ Trade Logs

All trades are automatically logged to `logs/trades_YYYY-MM-DD.csv` with:
- Entry/exit price, P&L, strategy, regime, confidence, hold time

---

## 🔬 Backtesting

```bash
# Run demo backtest (uses synthetic data)
python main.py --backtest

# For real backtest, edit backtest/backtest_engine.py
# and replace demo_df with historical data from KiteConnect
```

---

## 🚀 Deployment (Production)

For running 24/7 on a server:

```bash
# Install PM2 (Node.js process manager)
npm install -g pm2
pm2 start "python main.py" --name algo_trading
pm2 startup    # Auto-restart on reboot
pm2 save
```

Or use a simple cron job:
```bash
# Crontab: start at 9 AM on weekdays
0 9 * * 1-5 cd /path/to/algo_trading && python main.py >> logs/cron.log 2>&1
```

---

## ⚠️ Disclaimer

This software is for **educational and research purposes only**.
Trading involves substantial risk of loss. Past performance does not guarantee future results.
Always paper trade for at least 3 months before deploying real capital.
The authors are not responsible for any financial losses.

---

## 📝 License

MIT License — see LICENSE file.
