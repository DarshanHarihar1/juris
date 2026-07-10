from app import config


def test_verifier_routes_to_meshapi():
    assert config.role_provider_name("verifier") == "meshapi"
    assert config.provider("meshapi")["base_url"] == "https://api.meshapi.ai/v1"


def test_ocr_routes_to_meshapi_vision_model():
    ocr_model = config.role("ocr")["model"]
    assert ocr_model == "google/gemma-3-4b-it"
    assert config.model_provider_name(ocr_model) == "meshapi"
