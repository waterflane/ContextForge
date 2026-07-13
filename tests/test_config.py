from contextforge.config import Settings


def test_default_settings() -> None:
    settings = Settings()

    assert settings.app_name == "ContextForge"
    assert settings.environment == "development"
    assert settings.log_level == "INFO"
