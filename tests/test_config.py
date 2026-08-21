from app.config import settings


def test_config_loads():
    """Test that configuration loads from environment."""
    assert settings is not None
