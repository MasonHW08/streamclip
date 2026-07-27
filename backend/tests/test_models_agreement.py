from datetime import date

from app.models.agreement import Agreement, AgreementStatus, AgreementTermsVersion
from app.models.creator import Creator


def test_create_agreement(db_session):
    creator = Creator(platform="twitch", platform_channel_id="1", display_name="A")
    terms = AgreementTermsVersion(version="v1", effective_date=date.today(), body_markdown="terms text")
    db_session.add_all([creator, terms])
    db_session.commit()

    agreement = Agreement(
        creator_id=creator.id,
        terms_version_id=terms.id,
        rev_share_pct=50.0,
        signature_name="A Streamer",
        status=AgreementStatus.ACTIVE,
    )
    db_session.add(agreement)
    db_session.commit()

    assert agreement.id is not None
    assert agreement.status == AgreementStatus.ACTIVE
