from datetime import UTC, date, datetime, timedelta

import pytest

from app.core.security import create_magic_link_token, verify_magic_link_token_claims
from app.models.agreement import AgreementStatus, AgreementTermsVersion
from app.models.creator import Creator, CreatorStatus
from app.services.agreement_service import (
    accept_agreement,
    get_active_terms_version,
    revoke_agreement,
)


def _seed_terms(db_session):
    terms = AgreementTermsVersion(version="v1", effective_date=date.today(), body_markdown="{rev_share_pct}")
    db_session.add(terms)
    db_session.commit()
    return terms


def test_accept_agreement_authorizes_creator(db_session):
    _seed_terms(db_session)
    creator = Creator(platform="twitch", platform_channel_id="1", display_name="A")
    db_session.add(creator)
    db_session.commit()

    agreement = accept_agreement(
        db_session, creator_id=creator.id, signature_name="A", ip="1.2.3.4", user_agent="pytest"
    )

    db_session.refresh(creator)
    assert agreement.status == AgreementStatus.ACTIVE
    assert creator.status == CreatorStatus.AUTHORIZED


def test_revoke_agreement_revokes_creator(db_session):
    _seed_terms(db_session)
    creator = Creator(platform="twitch", platform_channel_id="1", display_name="A")
    db_session.add(creator)
    db_session.commit()
    accept_agreement(db_session, creator_id=creator.id, signature_name="A", ip="1.2.3.4", user_agent="pytest")

    revoked = revoke_agreement(db_session, creator_id=creator.id)

    db_session.refresh(creator)
    assert revoked.status == AgreementStatus.REVOKED
    assert creator.status == CreatorStatus.REVOKED


def test_revoke_with_no_active_agreement_raises(db_session):
    creator = Creator(platform="twitch", platform_channel_id="1", display_name="A")
    db_session.add(creator)
    db_session.commit()
    with pytest.raises(ValueError):
        revoke_agreement(db_session, creator_id=creator.id)


def test_get_active_terms_version_returns_latest(db_session):
    _seed_terms(db_session)
    terms = get_active_terms_version(db_session)
    assert terms.version == "v1"


def test_accept_agreement_twice_raises(db_session):
    _seed_terms(db_session)
    creator = Creator(platform="twitch", platform_channel_id="1", display_name="A")
    db_session.add(creator)
    db_session.commit()
    accept_agreement(db_session, creator_id=creator.id, signature_name="A", ip="1.2.3.4", user_agent="pytest")

    with pytest.raises(ValueError):
        accept_agreement(db_session, creator_id=creator.id, signature_name="A", ip="1.2.3.4", user_agent="pytest")


def test_accept_agreement_after_revoke_reauthorizes(db_session):
    _seed_terms(db_session)
    creator = Creator(platform="twitch", platform_channel_id="1", display_name="A")
    db_session.add(creator)
    db_session.commit()
    accept_agreement(db_session, creator_id=creator.id, signature_name="A", ip="1.2.3.4", user_agent="pytest")
    revoke_agreement(db_session, creator_id=creator.id)

    agreement = accept_agreement(
        db_session, creator_id=creator.id, signature_name="A", ip="1.2.3.4", user_agent="pytest"
    )

    db_session.refresh(creator)
    assert agreement.status == AgreementStatus.ACTIVE
    assert creator.status == CreatorStatus.AUTHORIZED


def _authorized_then_revoked_creator(db_session):
    _seed_terms(db_session)
    creator = Creator(platform="twitch", platform_channel_id="1", display_name="A")
    db_session.add(creator)
    db_session.commit()
    accept_agreement(
        db_session, creator_id=creator.id, signature_name="A", ip="1.2.3.4", user_agent="pytest"
    )
    return creator


def test_stale_token_from_before_revoke_cannot_reauthorize(db_session):
    creator = _authorized_then_revoked_creator(db_session)
    # The token that was emailed originally, minted before the creator revoked.
    _stale_id, stale_iat = verify_magic_link_token_claims(
        create_magic_link_token(creator.id, "agree"), "agree"
    )
    revoke_agreement(db_session, creator_id=creator.id)

    with pytest.raises(ValueError, match="no longer valid"):
        accept_agreement(
            db_session,
            creator_id=creator.id,
            signature_name="A",
            ip="1.2.3.4",
            user_agent="pytest",
            token_issued_at=stale_iat,
        )

    db_session.refresh(creator)
    assert creator.status == CreatorStatus.REVOKED


def test_fresh_token_minted_after_revoke_still_reauthorizes(db_session):
    creator = _authorized_then_revoked_creator(db_session)
    revoke_agreement(db_session, creator_id=creator.id)
    # A brand-new link, e.g. from a follow-up outreach email.
    _fresh_id, fresh_iat = verify_magic_link_token_claims(
        create_magic_link_token(creator.id, "agree"), "agree"
    )

    agreement = accept_agreement(
        db_session,
        creator_id=creator.id,
        signature_name="A",
        ip="1.2.3.4",
        user_agent="pytest",
        token_issued_at=fresh_iat,
    )

    db_session.refresh(creator)
    assert agreement.status == AgreementStatus.ACTIVE
    assert creator.status == CreatorStatus.AUTHORIZED


def test_stale_token_check_ignores_revocations_before_the_token(db_session):
    """An old revoke must not block a link minted after it."""
    creator = _authorized_then_revoked_creator(db_session)
    revoke_agreement(db_session, creator_id=creator.id)
    old_revoke_time = datetime.now(UTC) - timedelta(days=30)

    # Token issued 30 days ago, revocation happened just now -> stale.
    with pytest.raises(ValueError, match="no longer valid"):
        accept_agreement(
            db_session,
            creator_id=creator.id,
            signature_name="A",
            ip="1.2.3.4",
            user_agent="pytest",
            token_issued_at=old_revoke_time,
        )
