from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.routers import audit_log, auth, ebs_lookup, entra_config, identity_mappings

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


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
