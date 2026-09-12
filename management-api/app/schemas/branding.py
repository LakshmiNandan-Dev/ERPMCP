from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field, field_validator

# ~256 KB of base64, roughly a 190 KB image. No reverse proxy sits in front of
# this API (admin-gui's nginx serves static files only; the browser calls
# VITE_API_BASE_URL directly), so nothing upstream would reject an oversized
# body first — this cap is the only one, and it has to reject with a message an
# admin can act on rather than a truncated write.
MAX_LOGO_CHARS = 256_000

_ALLOWED_PREFIXES = ("data:image/png;base64,", "data:image/jpeg;base64,",
                     "data:image/svg+xml;base64,", "data:image/webp;base64,")


class BrandingInput(BaseModel):
    company_name: str | None = Field(default=None, max_length=120)
    logo_data_uri: str | None = Field(default=None, max_length=MAX_LOGO_CHARS)

    @field_validator("company_name")
    @classmethod
    def _blank_to_none(cls, v: str | None) -> str | None:
        return (v or "").strip() or None

    @field_validator("logo_data_uri")
    @classmethod
    def _must_be_an_inline_image(cls, v: str | None) -> str | None:
        """Reject anything that isn't an inline image.

        An http(s) URL would render in most browsers, which is exactly why it
        has to be refused explicitly: it would work on the developer's machine
        and silently show a broken image in the air-gapped customer network
        this product is deployed into.
        """
        if v is None or not v.strip():
            return None
        v = v.strip()
        if not v.startswith(_ALLOWED_PREFIXES):
            raise ValueError(
                "Logo must be an inline base64 image "
                "(data:image/png;base64,... or jpeg, svg+xml, webp). "
                "A URL will not work on a deployment with no internet access."
            )
        return v


class BrandingOut(BaseModel):
    """Always returned, even when nothing is configured — the admin console
    renders its header before anyone signs in, so "not configured yet" has to
    be an ordinary response carrying nulls, not a 404 the header must special-case.
    """

    company_name: str | None
    logo_data_uri: str | None
    updated_at: datetime | None
    updated_by: str | None

    model_config = {"from_attributes": True}
