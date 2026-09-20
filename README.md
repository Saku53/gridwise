# GridWise — LLM-Assisted Campus Energy Optimizer

BUP CSE Fest 2026 Hackathon (Online Preliminary). An HTTP API that interprets
1–3 natural-language operator notes with an LLM, validates the interpretation
deterministically, and produces a cost-minimal 24-hour battery/grid schedule.

## Architecture

```
[Energy Data + Operator Notes]
        |
        v
[LLM Interpreter]  app/llm.py            <- Gemini, JSON-mode, temperature 0
        |             notes -> raw directive JSON only, no scenario numbers
        v
[Guardrail Validator]  app/guardrails.py <- deterministic repair/rejection
        |             raw JSON -> exactly N clean entries + directive list
        v
[Math Optimizer]  app/optimizer.py       <- PuLP LP, then netting + self-check
        |             directives + numbers -> valid 24-hour plan
        v
[API Response]  app/main.py
```

The LLM never sees demand/solar/tariff/battery values, and the optimizer never
sees a natural-language string — the two are decoupled by the guardrail layer.

- **Model / provider:** Google Gemini via `google-genai`, model name from
  `GEMINI_MODEL` (default `gemini-3.6-flash`).
- **LLM's role:** convert `operator_notes` into structured directives only.
  It performs no scheduling and no arithmetic on the scenario.
- **Guardrails:** `app/guardrails.py` — enforces the allowed directive-type
  enum, repairs malformed hours/factors, forces `no_op` semantics, strips
  unknown keys, fills missing/duplicate note mappings.
- **Optimizer/solver:** PuLP with the bundled CBC solver, solving a linear
  program per request, followed by a deterministic self-check
  (`app/optimizer.py::self_check`) that re-verifies every energy/battery rule
  against the exact numbers being returned.
- **Outage fallback:** `app/fallback_interpreter.py` — a regex-based
  interpreter used ONLY if Gemini is unreachable after retries, so the
  service never 500s. This is not the primary interpretation path.

## Local quickstart (clean environment)

```bash
git clone <https://github.com/Saku53/gridwise>
cd gridwise
python -m venv venv && source venv/bin/activate      # Windows: venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env        # then fill in GEMINI_API_KEY
export $(grep -v '^#' .env | xargs)   # or use a .env loader / your shell's export

uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Required environment variables:

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `GEMINI_API_KEY` | yes | — | Google AI Studio API key |
| `GEMINI_MODEL` | no | `gemini-3.6-flash` | Gemini model name |
| `PORT` | no | `8000` | Port the server binds to |

## Live deployment

Base URL: `https://gridwise-anye.onrender.com`

```bash
curl https://gridwise-anye.onrender.com/health
```

Deployed as a Docker web service on Render, reading `GEMINI_API_KEY` from
the platform's environment variables (never committed to the repository).

## API check

```bash
curl http://localhost:8000/health
# {"status":"ok"}

curl -X POST http://localhost:8000/optimize-energy \
  -H "Content-Type: application/json" \
  -d @sample_request.json
```

`sample_request.json` should contain one full 24-hour scenario matching the
Problem Statement schema (`scenario_id`, `operator_notes`, `hours[24]`,
`battery`).

## Run the public sample pack

```bash
python scripts/run_samples.py path/to/public_samples.json http://localhost:8000
```

This posts every case, checks `directive_interpretation` shape/order, and
replays `hourly_plan` against the energy-balance, battery, and neutrality
rules — printing PASS/FAIL per case rather than requiring byte-exact match.

## Offline unit tests (no API key needed)

```bash
pytest tests/test_local.py -q
```

These exercise `guardrails.py` and `optimizer.py` directly (repair of
malformed model output, all directive types together, infeasible-combo
fallback) without calling Gemini.

## Docker fallback

```bash
docker build -t <dockerhub-user>/gridwise:v1 .
docker run --rm -p 8000:8000 -e GEMINI_API_KEY=xxxx <dockerhub-user>/gridwise:v1
curl http://localhost:8000/health

docker push <dockerhub-user>/gridwise:v1
```

The image exposes port 8000, binds to `0.0.0.0`, runs as a non-root user, and
contains no baked-in credentials — pass `GEMINI_API_KEY` at `docker run` time.

## Dependencies & credits

FastAPI, Pydantic, PuLP (CBC solver), `google-genai`, `httpx`, `pytest`. No
other external services are used. Synthetic challenge data only — no live
campus, utility, billing, or personal data.

## Known limitations

- Gemini API quota/rate limits on the free tier apply; a single call handles
  all notes in one request to stay within them.
- If the Gemini provider is unreachable, the regex fallback interpreter is
  weaker on paraphrased time expressions than the LLM path — this only
  activates on provider outage, never as the primary path.
- The optimizer sheds constraints in a fixed order (`max_grid_window` →
  `minimum_battery_reserve` → charge/discharge windows) only if the LP is
  infeasible; per the Problem Statement, valid scoring scenarios should never
  reach this path.

## Secret handling

No API keys or secrets are committed to this repository. `.env` is
git-ignored; only `.env.example` (names, no values) is tracked. Error
responses return a generic error type, never raw exception messages or
stack traces.
