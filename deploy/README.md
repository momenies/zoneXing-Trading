# Running the backtest on Google Cloud

The sandbox this harness was written in cannot reach exchange APIs, so the
figures in the top-level README come from synthetic candles. Cloud Run has open
egress — running the job there is what produces **real** numbers.

## Why a Cloud Run Job

A backtest is a batch task: it starts, runs for a few minutes, writes results,
and exits. A Cloud Run *Job* bills only for those minutes; a Cloud Run *service*
or a VM would sit idle between runs and still cost money. The image is built by
Cloud Build, so **Docker is not needed on your machine**.

```
deploy.sh ──> Cloud Build ──> Artifact Registry ──> Cloud Run Job
                                                         │
                                    exchange API ────────┤
                                                         ▼
                                              GCS bucket (results + candles)
```

## Deploy

```bash
git clone <this repo> && cd zoneXing-Trading
./deploy/deploy.sh YOUR_PROJECT_ID            # region defaults to us-central1
```

The script is idempotent — re-run it to redeploy after a code change. It creates
an Artifact Registry repo, a results bucket, a service account scoped to that
bucket only, and the job itself.

## Run and read results

```bash
gcloud run jobs execute zonexing-backtest --region us-central1 --wait

gcloud storage cat gs://YOUR_PROJECT_ID-zonexing-results/runs/latest/report.txt
```

Each execution writes to `runs/<timestamp>/`, and mirrors the newest into
`runs/latest/`:

| Object | Contents |
|---|---|
| `report.txt` | human-readable report for every pivot mode |
| `results.json` | full metrics, constraint audit, causality check |
| `trades_<mode>.csv` | every round trip |
| `equity_<mode>.csv` | the equity curve |
| `candles/5m/<CODE>.csv` | fetched candles, cached for offline re-runs |

Pull the cached candles back down and the whole thing runs locally with no
network at all:

```bash
gcloud storage cp -r gs://YOUR_PROJECT_ID-zonexing-results/candles/5m ./data
python3 run_backtest.py --source csv --csv-dir ./data --initial-cash 1200 --compare
```

## Configuration

Every knob is an environment variable, so changing the window needs no rebuild:

```bash
gcloud run jobs update zonexing-backtest --region us-central1 \
  --update-env-vars BARS=26496,INITIAL_CASH=5000
```

| Variable | Default | Meaning |
|---|---|---|
| `BARS` | `52992` | 5m bars per symbol (52992 ≈ 6 months, 26496 ≈ 3) |
| `INITIAL_CASH` | `1200` | starting equity |
| `INVEST_FRAC` | `0.2` | weight per position |
| `LEVERAGE` | `1` | |
| `TAKER` / `SLIPPAGE` | `0.0005` | cost per side |
| `PIVOT_MODES` | all three | `fractal shift donchian` |
| `CODES` | the six from the README | space-separated |
| `DATA_SOURCE` | `exchange` | `synthetic` for a smoke test |
| `GCS_BUCKET` | set by deploy.sh | omit to write locally only |
| `CACHE_CANDLES` | `1` | also upload the fetched candles |

## Running it on a schedule

```bash
gcloud scheduler jobs create http zonexing-weekly \
  --location us-central1 --schedule "0 6 * * MON" \
  --uri "https://us-central1-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/YOUR_PROJECT_ID/jobs/zonexing-backtest:run" \
  --http-method POST \
  --oauth-service-account-email zonexing-backtest@YOUR_PROJECT_ID.iam.gserviceaccount.com
```

The service account also needs `roles/run.invoker` for this.

## Cost

A run is 1 vCPU / 2 GiB for roughly 3–8 minutes, most of it waiting on the
exchange's rate limiter. That is a few US cents per run, and Cloud Run's monthly
free tier normally absorbs occasional runs entirely. Storage is a few MB.
`./deploy/destroy.sh PROJECT_ID` removes everything.

## API keys

Not needed. Candle endpoints are public on both venues. If you want the higher
Binance rate limit, add the key as a Cloud Run secret rather than baking it into
the image:

```bash
echo -n "YOUR_KEY" | gcloud secrets create binance-api-key --data-file=-
gcloud run jobs update zonexing-backtest --region us-central1 \
  --set-secrets BINANCE_API_KEY=binance-api-key:latest
```

Use a **read-only** key: this job reads prices and places no orders.

## Troubleshooting

**`PERMISSION_DENIED` during the build.** On new projects the Cloud Build
service account may lack log-writing rights:

```bash
PROJECT_NUMBER=$(gcloud projects describe YOUR_PROJECT_ID --format='value(projectNumber)')
gcloud projects add-iam-policy-binding YOUR_PROJECT_ID \
  --member "serviceAccount:${PROJECT_NUMBER}-compute@developer.gserviceaccount.com" \
  --role roles/logging.logWriter
```

**The job fails with `no exchange reachable`.** Check whether a VPC connector or
egress policy is restricting outbound traffic; by default Cloud Run egress is
open.

**The run exceeds the 30m task timeout.** Lower `BARS`, or raise the timeout
with `--task-timeout 60m`.
