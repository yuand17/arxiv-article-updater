from arxiv_updater.config import Settings


def test_health_and_local_home_are_available_without_login(app_client):
    client, _, _ = app_client
    assert client.get("/health").json() == {"status": "ok"}
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 200
    assert "本周精选" in response.text
    assert "/login" not in response.text


def test_configuration_rejects_remote_database_urls():
    try:
        Settings(database_url="postgresql://example.invalid/arxiv")
    except ValueError as exc:
        assert "SQLite" in str(exc)
    else:
        raise AssertionError("remote database configuration should be rejected")
