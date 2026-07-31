from datetime import UTC, datetime, timedelta


def test_health_and_login_redirect(app_client):
    client, _, _ = app_client
    assert client.get("/health").json() == {"status": "ok"}
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_invite_registration_and_login(app_client):
    client, session_factory, models = app_client
    from arxiv_updater.auth import create_invite

    with session_factory() as db:
        token = create_invite(db)

    response = client.post(
        "/register",
        data={
            "invite": token,
            "email": "reader@example.com",
            "display_name": "Reader",
            "password": "a-strong-password",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    with session_factory() as db:
        user = db.query(models.User).filter_by(email="reader@example.com").one()
        assert user.display_name == "Reader"

    client.post("/logout")
    response = client.post(
        "/login",
        data={"email": "reader@example.com", "password": "a-strong-password"},
        follow_redirects=False,
    )
    assert response.status_code == 303


def test_expired_invite_is_rejected(app_client):
    client, session_factory, models = app_client
    import hashlib

    raw = "expired-token"
    with session_factory() as db:
        db.add(
            models.Invite(
                token_hash=hashlib.sha256(raw.encode()).hexdigest(),
                expires_at=datetime.now(UTC) - timedelta(days=1),
            )
        )
        db.commit()
    response = client.post(
        "/register",
        data={
            "invite": raw,
            "email": "late@example.com",
            "display_name": "Late",
            "password": "a-strong-password",
        },
    )
    assert response.status_code == 400
    assert "已过期" in response.text

