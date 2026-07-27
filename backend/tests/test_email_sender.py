from app.services.email_sender import FakeEmailSender


def test_fake_sender_records_sent_email():
    sender = FakeEmailSender()
    message_id = sender.send(to="a@example.com", subject="Hi", html_body="<p>hi</p>")
    assert message_id
    assert sender.sent == [{"to": "a@example.com", "subject": "Hi", "html_body": "<p>hi</p>", "id": message_id}]
