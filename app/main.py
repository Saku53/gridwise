"""API surface. The whole pipeline is visible in one function below."""
from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("gridwise")
app = FastAPI(title="GridWise", docs_url="/docs")


class Hour(BaseModel):
    hour: int = Field(ge=0, le=23)
    demand_kwh: float = Field(ge=0)
    solar_kwh: float = Field(ge=0)
    tariff_bdt_per_kwh: float = Field(ge=0)


class Battery(BaseModel):
    capacity_kwh: float = Field(gt=0)
    initial_energy_kwh: float = Field(ge=0)
    minimum_energy_kwh: float = Field(ge=0)
    max_charge_kwh_per_hour: float = Field(ge=0)
    max_discharge_kwh_per_hour: float = Field(ge=0)


class Scenario(BaseModel):
    scenario_id: str
    operator_notes: list[str] = Field(min_length=1, max_length=3)
    hours: list[Hour] = Field(min_length=24, max_length=24)
    battery: Battery

    @field_validator("hours")
    @classmethod
    def _unique_hours(cls, v):
        if sorted(h.hour for h in v) != list(range(24)):
            raise ValueError("hours must contain each hour 0-23 exactly once")
        return sorted(v, key=lambda h: h.hour)

    @field_validator("operator_notes")
    @classmethod
    def _non_empty(cls, v):
        if any(not str(n).strip() for n in v):
            raise ValueError("operator_notes must be non-empty strings")
        return [str(n).strip() for n in v]


@app.get("/health")
def health():
    return {"status": "ok"}          # never touches the LLM, always instant


@app.post("/optimize-energy")
def optimize_energy(req: Scenario):
    from app.guardrails import validate
    from app.llm import interpret_notes
    from app.optimizer import optimize
    from app.summary import summarize

    raw, source = interpret_notes(req.operator_notes)          # 1. language -> JSON
    entries, directives = validate(raw, len(req.operator_notes), req.battery)  # 2. trust boundary
    plan, totals, applied = optimize(req.hours, req.battery, directives)       # 3. math

    log.info("%s notes=%d source=%s directives=%d cost=%.2f",
             req.scenario_id, len(req.operator_notes), source, len(applied),
             totals["total_cost_bdt"])

    return {
        "scenario_id": req.scenario_id,
        "directive_interpretation": entries,
        "hourly_plan": plan,
        **totals,
        "plan_summary": summarize(plan, entries, totals),
    }


@app.exception_handler(RequestValidationError)
async def _bad_request(_: Request, exc: RequestValidationError):
    return JSONResponse(status_code=400,
                        content={"error": "invalid_request", "detail": str(exc)[:500]})


@app.exception_handler(Exception)
async def _internal(_: Request, exc: Exception):
    log.exception("unhandled")
    return JSONResponse(status_code=500,
                        content={"error": "internal_error",
                                 "detail": type(exc).__name__})  # never leak the message
