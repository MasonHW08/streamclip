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


def test_authorized_creator_no_agreement_rows_not_authorized(db_session):
    """Isolates the Agreement-side filter: creator status alone is AUTHORIZED,
    but there is no Agreement row at all, so it must still be False."""
    creator = Creator(
        platform="twitch", platform_channel_id="1", display_name="A", status=CreatorStatus.AUTHORIZED
    )
    db_session.add(creator)
    db_session.commit()
    assert is_authorized(db_session, creator.id) is False


def test_authorized_creator_only_non_active_agreements_not_authorized(db_session):
    """Isolates the Agreement.status == ACTIVE filter: creator status is
    AUTHORIZED, and Agreement rows exist, but none is ACTIVE (one revoked,
    one pending) — must still be False. A refactor that dropped or weakened
    the ACTIVE filter would incorrectly pass this test if it only checked
    for "any Agreement row exists"."""
    terms_a = _make_terms(db_session, version="v1")
    terms_b = _make_terms(db_session, version="v2")
    creator = Creator(
        platform="twitch", platform_channel_id="1", display_name="A", status=CreatorStatus.AUTHORIZED
    )
    db_session.add(creator)
    db_session.commit()
    db_session.add_all(
        [
            Agreement(
                creator_id=creator.id,
                terms_version_id=terms_a.id,
                rev_share_pct=50.0,
                status=AgreementStatus.REVOKED,
            ),
            Agreement(
                creator_id=creator.id,
                terms_version_id=terms_b.id,
                rev_share_pct=50.0,
                status=AgreementStatus.PENDING,
            ),
        ]
    )
    db_session.commit()
    assert is_authorized(db_session, creator.id) is False


def test_authorized_creator_old_revoked_plus_new_active_agreement_is_authorized(db_session):
    """Creator status is AUTHORIZED, with an old REVOKED agreement plus a
    separate newer ACTIVE agreement — must be True. A current active
    agreement governs authorization regardless of prior revocation history."""
    terms_a = _make_terms(db_session, version="v1")
    terms_b = _make_terms(db_session, version="v2")
    creator = Creator(
        platform="twitch", platform_channel_id="1", display_name="A", status=CreatorStatus.AUTHORIZED
    )
    db_session.add(creator)
    db_session.commit()
    db_session.add_all(
        [
            Agreement(
                creator_id=creator.id,
                terms_version_id=terms_a.id,
                rev_share_pct=50.0,
                status=AgreementStatus.REVOKED,
            ),
            Agreement(
                creator_id=creator.id,
                terms_version_id=terms_b.id,
                rev_share_pct=50.0,
                status=AgreementStatus.ACTIVE,
            ),
        ]
    )
    db_session.commit()
    assert is_authorized(db_session, creator.id) is True
