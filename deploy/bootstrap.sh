#!/usr/bin/env bash
# zoneXing Trading — one-command server setup + causal backtest.
#
#   curl -fsSL <raw-url>/deploy/bootstrap.sh | bash -s -- --exchange mexc --days 180
# or, after cloning:
#   bash deploy/bootstrap.sh --exchange mexc --days 180
#
# Installs into a venv, verifies the engine, then runs the backtest that tells
# you what the strategy is actually worth. It does NOT place any trade and does
# NOT need API keys — the backtest reads public candles only.
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/momenies/zoneXing-Trading.git}"
BRANCH="${BRANCH:-claude/bot-deployment-first-trade-52a7pw}"
DIR="${DIR:-$HOME/zonexing-trading}"
EXCHANGE="okx"
DAYS="180"
SYMBOLS="ETH-USDT,BNB-USDT,SOL-USDT,XRP-USDT,DOGE-USDT"
MARKET_TYPE="swap"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --exchange)    EXCHANGE="$2"; shift 2 ;;
    --days)        DAYS="$2"; shift 2 ;;
    --symbols)     SYMBOLS="$2"; shift 2 ;;
    --market-type) MARKET_TYPE="$2"; shift 2 ;;
    --dir)         DIR="$2"; shift 2 ;;
    --branch)      BRANCH="$2"; shift 2 ;;
    -h|--help)     sed -n '2,12p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }

say "checking prerequisites"
command -v git >/dev/null || { echo "git is required: sudo apt install -y git"; exit 1; }
PY=$(command -v python3 || true)
[[ -n "$PY" ]] || { echo "python3 is required: sudo apt install -y python3 python3-venv"; exit 1; }
"$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)' \
  || { echo "python 3.10+ required, found $($PY -V)"; exit 1; }
echo "git $(git --version | awk '{print $3}'), $($PY -V)"

say "fetching the code into $DIR"
if [[ -d "$DIR/.git" ]]; then
  git -C "$DIR" fetch origin "$BRANCH"
  git -C "$DIR" checkout "$BRANCH"
  git -C "$DIR" pull --ff-only origin "$BRANCH"
else
  git clone --branch "$BRANCH" "$REPO_URL" "$DIR"
fi
cd "$DIR"

say "installing dependencies"
[[ -d .venv ]] || "$PY" -m venv .venv
./.venv/bin/pip install --quiet --upgrade pip
./.venv/bin/pip install --quiet -r requirements.txt
VPY="$DIR/.venv/bin/python"

say "verifying the engine (offline)"
"$VPY" -m live.trader --selftest
"$VPY" -m tests.test_live

say "backtesting the LIVE rule on $DAYS days of real $EXCHANGE data"
echo "downloading candles — this can take a few minutes the first time"
REPORT="$DIR/backtest_${EXCHANGE}_${DAYS}d_$(date -u +%Y%m%d).txt"
"$VPY" -m tools.backtest \
  --exchange "$EXCHANGE" --market-type "$MARKET_TYPE" \
  --symbols "$SYMBOLS" --days "$DAYS" --compare --out "$REPORT"

say "done"
cat <<EOF
Report saved: $REPORT

Read the CAUSAL rows only — they are what the bot can trade. The archived
look-ahead row is there to show how much of the published edge was hindsight.

If the causal rows are not profitable after fees, do NOT go live: tune the
parameters and re-run this backtest first.

When you are ready to run the bot (still no real orders):
    cd $DIR
    cp .env.example .env && nano .env     # MODE=paper
    ./.venv/bin/python -m live.trader --once
    ./.venv/bin/python -m live.trader --health
EOF
