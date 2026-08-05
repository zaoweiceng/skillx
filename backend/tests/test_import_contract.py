def test_application_imports_from_locked_dependencies() -> None:
    from app.main import app

    assert app.title
