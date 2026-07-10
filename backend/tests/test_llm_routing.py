from app import config


def test_verifier_routes_to_cerebras():
    assert config.role_provider_name("verifier") == "cerebras"
    assert config.provider("cerebras")["base_url"] == "https://api.cerebras.ai/v1"


def test_explicit_model_routing_keeps_ocr_on_groq():
    ocr_model = config.role("ocr")["model"]
    assert config.model_provider_name(ocr_model) == "groq"
