from datetime import date

from app.models.agreement import Agreement, AgreementStatus, AgreementTermsVersion
from app.models.creator import Creator, CreatorStatus
from app.services.permission_gate import is_authorized


def _make_terms(db_session, version="v1"):
    terms = AgreementTermsVersion(version=version, effective_date=date.today(), body_markdown="terms text")
    db_session.add(terms)
    db_session.commit()
    return terms


def test_unknown_creator_not_authorized(db_session):
    assert is_authorized(db_session, 999999) is False


def test_prospect_not_authorized(db_session):
    creator = Creator(platform="twitch", platform_channel_id="1", display_name="A")
    db_session.add(creator)
    db_session.commit()
    assert is_authorized(db_session, creator.id) is False


def test_authorized_with_active_agreement(db_session):
    terms = _make_terms(db_session)
    creator = Creator(
        platform="twitch", platform_channel_id="1", display_name="A", status=CreatorStatus.AUTHORIZED
    )
    db_session.add(creator)
    db_session.commit()
    db_session.add(
        Agreement(
            creator_id=creator.id,
            terms_version_id=terms.id,
            rev_share_pct=50.0,
            status=AgreementStatus.ACTIVE,
        )
    )
    db_session.commit()
    assert is_authorized(db_session, creator.id) is True


def test_revoked_agreement_not_authorized(db_session):
    terms = _make_terms(db_session)
    creator = Creator(
        platform="twitch", platform_channel_id="1", display_name="A", status=CreatorStatus.REVOKED
    )
    db_session.add(creator)
    db_session.commit()
    db_session.add(
        Agreement(
            creator_id=creator.id,
            terms_version_id=terms.id,
            rev_share_pct=50.0,
            status=AgreementStatus.REVOKED,
        )
    )
    db_session.commit()
    assert is_authorized(db_session, creator.id) is False
