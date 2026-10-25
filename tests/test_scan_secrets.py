from tools.scan_secrets import find_secrets, scan_text


def test_scanner_flags_a_constructed_credential() -> None:
    sample = "AK" + "IA" + ("B" * 16)
    assert "aws-access-key" in scan_text(sample)


def test_ordinary_prose_is_clean() -> None:
    assert scan_text("Weather data by Open-Meteo.com. No key is stored here.") == []


def test_repository_has_no_secrets() -> None:
    assert find_secrets() == []
