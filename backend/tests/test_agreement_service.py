from datetime import date

import pytest

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
