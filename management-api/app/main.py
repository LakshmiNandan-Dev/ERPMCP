from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import load_settings
from app.routers import audit_log, auth, branding, ebs_lookup, entra_config, identity_mappings

app = FastAPI(title="EBSMCP Management API")

# Dev-only convenience: the admin-gui's Vite dev server runs on a different
# origin/port than this API. A real deployment would front both through
# the same origin (or an explicit allowlist), not "*" — narrowing this is
# part of the same auth work admin_subject already flags as pending.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(identity_mappings.router)
app.include_router(audit_log.router)
app.include_router(entra_config.router)
app.include_router(ebs_lookup.router)
app.include_router(auth.router)
app.include_router(branding.router)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/deployment")
def deployment() -> dict[str, str]:
    """What this deployment is, for the admin console to read.

    Deliberately separate from /health rather than folded into it: health is
    scraped by probes and orchestrators, and changing its shape to carry
    configuration risks breaking whatever is already watching it.

    Ungated, like GET /branding — the console needs this before sign-in to
    know which environment it is administering, and a deploy-stage name is
    not sensitive. It is which stage this process serves, NOT which EBS
    database it reaches; see the README on those being different axes.
    """
    return {"environment": load_settings().ebsmcp_environment}
