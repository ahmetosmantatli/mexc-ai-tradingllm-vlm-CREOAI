# Cloudflare / OpenRouter free provider pool

This adds opt-in fallback routes to the current provider pool. It does not change
the trading strategy, runner, broker, risk rules, live gates, or Claude setting.
More providers add availability, not evidence of profitable trading.

## What is admitted

OpenRouter: authenticated account catalog, explicit `:free` variant, every listed
price component zero, text decision output, at least 32K context, known completion
limit, and JSON output or function calling. Image inputs are required for VLM.
Every candidate is checked; paid/unknown-price, embedding/audio-only, small-context,
and incompatible models are excluded with a reason in the discovery report.
The free catalog currently includes `nvidia/nemotron-3.5-super-vl:free` (vision)
and text models such as `nvidia/nemotron-3-ultra-550b-a55b:free`. Availability is
discovered from your account; those names are examples, not hardcoded routes.

Cloudflare: the full catalog is scanned, but only these verified free-plan models
are currently admitted: `@cf/zai-org/glm-4.7-flash` (text),
`@cf/google/gemma-4-26b-a4b-it` (text/vision), and
`@cf/nvidia/nemotron-3-120b-a12b` (text). Other models are reported as unverified,
not silently assumed free. The token needs Workers AI Read and Edit permissions
for the selected account; do not grant full account/DNS access.

Catalog eligibility is not a completed inference test, proof of output reliability,
or a trading quality assessment. `probe` below tests one small text response.
Actual chart/decision requests still pass the same application validator.

## Quotas and fallback

- OpenRouter: all free models share the account's free daily allowance. Baseline
  is 20 RPM and 50 RPD; a larger daily allowance is used only when the authenticated
  key endpoint reports it at discovery. Key quota is refreshed before generation
  with a 60-second cache, pricing/capabilities with a five-minute cache. Failed
  generations count conservatively. Additional models/keys do not multiply it.
- Cloudflare: all models share 10,000 neurons per UTC day on Workers Free. The
  default local budget is 9,000 neurons. It reserves a conservative input/output
  estimate before HTTP, reconciles known usage, and retains unknown reservations
  across failures/restarts. Images reserve the full input context bound. Other
  applications' usage is not observed. Four RPM is our operational guard, not a
  claim about the provider's rate allowance. Free-plan confirmation is required;
  the setup does not upgrade your account or enable paid usage.
- One in-flight operation per external account bucket prevents parallel models
  overspending the same allowance. A timeout/429 does not create extra quota.
  Retry-After is binding, wait/retry remains bounded by the current analysis
  deadline, and existing Groq/Gemini/optional Claude fallback still applies.
- Valid HOLD is final, not a provider failure. All routes receive the same system
  context, transaction card, data, and (for VLM) images. Requests too large for a
  route are skipped instead of cutting context. Tool calls only return a decision;
  they do not execute exchange actions. The risk/execution layers remain mandatory.
- Authentication errors cool the account; unsupported requests cool the model.
  Redirects are not followed. Journals contain model, provider, status, tokens,
  latency and safe validation fields, never prompts, keys or raw response bodies.

## Server setup

Use the project directory. Keep `AI_CLAUDE_FALLBACK_ENABLED=false` if you do not
want paid fallback. This tool preserves its existing value. Cloudflare should
actually be on **Workers Free** before using the confirmation flag below.

```sh
cd /opt/mexc-ai-trading
git fetch origin
git switch codex/live-pipeline-integration
git pull --ff-only origin codex/live-pipeline-integration
.venv/bin/python -m trading.control.external_models setup --cloudflare-free-plan
```

Paste OpenRouter key, Cloudflare token, then the 32-character Cloudflare Account
ID into the hidden prompts. Empty input preserves an existing value. Secrets
never go into shell arguments/history. The `.env` update is atomic; `.env` and
the backup have mode 0600. Existing MEXC/AI/risk configuration is preserved.
If you only use OpenRouter, leave Cloudflare fields empty and omit the flag.
Do not copy `.env.example` over your configured `.env`.

Recreate the worker so new environment values reach the container, then discover
inside it. Discovery uses GETs only; it never sends a MEXC request/order.
Existing broker reconciliation and native protection behavior are unchanged.

```sh
docker compose --env-file .env -f infrastructure/docker-compose.yml -p mexc-ai up -d --build --no-deps worker
docker compose --env-file .env -f infrastructure/docker-compose.yml -p mexc-ai exec -T worker python -m trading.control.external_models discover
docker compose --env-file .env -f infrastructure/docker-compose.yml -p mexc-ai exec -T worker python -m trading.control.external_models status
```

The report lists admitted models, excluded reasons, and verified/estimated quota
scope. If discovery fails, other providers remain available; no paid substitute
is enabled by this tool. `discovery_failed` means credentials/permissions/catalog
need checking, not that the catalog was empty. Do not send your keys in chat.
The manifest is stored on the existing `/app/data` Docker volume. Running
discovery on the host writes a different `data` directory, so use the container.
Updated routes load without restarting the worker; manifests expire after 24h.

Status and diagnostics now include counts of plans waiting for entry, triggered
plans waiting for review, plans waiting for fresh data after HOLD, and queued
analysis candidates. These are plans, not pending exchange orders. They also show
the last completed analysis/execution outcome and whether paid fallback is on.
LIVE/PAPER share the activity message formatter. Enabled accounts receive a
summary every 15 minutes (`TELEGRAM_ACTIVITY_INTERVAL_SECONDS=900`); category
changes can notify after at least two minutes. This creates no AI request.
LIVE tasks taking longer than max(180s, three scan intervals, two provider
timeouts + 30s) are flagged as delayed, not conclusively deadlocked. Trading
controls are not changed by the warning. If the entire process/server or Telegram
connection is down, self-notification cannot be guaranteed; that needs an
independent watchdog. No alert here automatically re-arms or retries an order.

Optional small inference check (one text request, no market/broker calls):

```sh
docker compose --env-file .env -f infrastructure/docker-compose.yml -p mexc-ai exec -T worker python -m trading.control.external_models probe --model '@cf/zai-org/glm-4.7-flash'
```

Use an exact admitted ID from discovery. A successful probe does not certify
vision quality, trading decisions or all models. It shares the worker's quota
ledger and never invokes Claude, even when Claude is the configured primary.

Install hourly GET-only discovery to keep the catalog current automatically:

```sh
cp infrastructure/mexc-ai-catalog.service infrastructure/mexc-ai-catalog.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now mexc-ai-catalog.timer
systemctl list-timers mexc-ai-catalog.timer
```

`journalctl -u mexc-ai-catalog -n 30 --no-pager` shows discovery results. Telegram
`/quota`, diagnostics and trade attribution show the new providers. Cloudflare
neuron totals are **local estimates**, never a claimed provider balance. Disable
with `EXTERNAL_MODELS_ENABLED=false` and recreate the worker; disable the timer
with `systemctl disable --now mexc-ai-catalog.timer` if desired.

## Primary sources checked 2026-11-08

- [OpenRouter account limits and authenticated key quota](https://openrouter.ai/docs/api/reference/limits)
- [OpenRouter account-filtered models](https://openrouter.ai/docs/api/api-reference/models/list-models-filtered-by-user-provider-preferences-privacy-settings-and-guardrails)
- [OpenRouter model properties](https://openrouter.ai/docs/api/api-reference/models/list-all-models-and-their-properties)
- [Cloudflare free versus paid models](https://developers.cloudflare.com/changelog/post/2026-07-28-models-require-workers-paid/)
- [Cloudflare neurons/pricing](https://developers.cloudflare.com/workers-ai/platform/pricing/)
- [Cloudflare OpenAI compatibility](https://developers.cloudflare.com/workers-ai/configuration/open-ai-compatibility/)
