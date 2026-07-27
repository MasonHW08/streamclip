from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.db import get_db
from app.core.rate_limit import limiter
from app.core.security import (
    InvalidMagicLinkToken,
    create_magic_link_token,
    verify_magic_link_token,
    verify_magic_link_token_claims,
)
from app.models.creator import Creator
from app.services.agreement_service import (
    accept_agreement,
    get_active_terms_version,
    revoke_agreement,
)

router = APIRouter(prefix="/partner", tags=["partner"])
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))


def build_revoke_url(creator_id: int) -> str:
    """Mint a fresh single-purpose revoke magic link for this creator."""
    settings = get_settings()
    return (
        f"{settings.public_base_url}/partner/revoke"
        f"?token={create_magic_link_token(creator_id, 'revoke')}"
    )


def _token_error(request: Request, exc: InvalidMagicLinkToken) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "token_error.html", {"message": str(exc)}, status_code=400
    )


def _confirmation(
    request: Request, message: str, status_code: int = 200, revoke_url: str | None = None
) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "confirmation.html",
        {"message": message, "revoke_url": revoke_url},
        status_code=status_code,
    )


@router.get("/agree", response_class=HTMLResponse)
@limiter.limit("10/minute")
def show_agreement(request: Request, token: str, db: Session = Depends(get_db)):
    try:
        creator_id = verify_magic_link_token(token, expected_purpose="agree")
    except InvalidMagicLinkToken as exc:
        return _token_error(request, exc)

    creator = db.get(Creator, creator_id)
    if creator is None:
        return _token_error(request, InvalidMagicLinkToken("This link isn't valid."))

    settings = get_settings()
    try:
        terms = get_active_terms_version(db)
    except ValueError:
        return templates.TemplateResponse(
            request,
            "token_error.html",
            {"message": "This page isn't ready yet. Please try again shortly."},
            status_code=500,
        )
    terms_body = terms.body_markdown.format(rev_share_pct=settings.default_rev_share_pct)
    return templates.TemplateResponse(
        request,
        "agreement.html",
        {
            "creator_name": creator.display_name,
            "terms": terms,
            "terms_body": terms_body,
            "token": token,
            "revoke_url": build_revoke_url(creator.id),
        },
    )


@router.post("/agree", response_class=HTMLResponse)
@limiter.limit("10/minute")
def submit_agreement(
    request: Request,
    token: str = Form(...),
    signature_name: str = Form(...),
    db: Session = Depends(get_db),
):
    try:
        creator_id, token_issued_at = verify_magic_link_token_claims(
            token, expected_purpose="agree"
        )
    except InvalidMagicLinkToken as exc:
        return _token_error(request, exc)

    client_ip = request.client.host if request.client else "unknown"
    user_agent = request.headers.get("user-agent", "unknown")
    try:
        accept_agreement(
            db,
            creator_id=creator_id,
            signature_name=signature_name,
            ip=client_ip,
            user_agent=user_agent,
            token_issued_at=token_issued_at,
        )
    except ValueError as exc:
        return _confirmation(request, str(exc), status_code=409)

    return _confirmation(
        request,
        "You're all set — thanks for partnering with us.",
        revoke_url=build_revoke_url(creator_id),
    )


@router.get("/revoke", response_class=HTMLResponse)
@limiter.limit("10/minute")
def show_revoke(request: Request, token: str):
    try:
        verify_magic_link_token(token, expected_purpose="revoke")
    except InvalidMagicLinkToken as exc:
        return _token_error(request, exc)

    return templates.TemplateResponse(request, "revoke.html", {"token": token})


@router.post("/revoke", response_class=HTMLResponse)
@limiter.limit("10/minute")
def submit_revoke(request: Request, token: str = Form(...), db: Session = Depends(get_db)):
    try:
        creator_id = verify_magic_link_token(token, expected_purpose="revoke")
    except InvalidMagicLinkToken as exc:
        return _token_error(request, exc)

    try:
        revoke_agreement(db, creator_id=creator_id)
    except ValueError as exc:
        return _confirmation(request, str(exc), status_code=409)

    return _confirmation(
        request, "You've been removed from the partner program. This takes effect immediately."
    )
