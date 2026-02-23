from pydantic_settings import BaseSettings
from pathlib import Path


class Settings(BaseSettings):
    # eBay API
    ebay_app_id: str = ""
    ebay_cert_id: str = ""
    ebay_dev_id: str = ""
    ebay_redirect_uri: str = ""
    ebay_environment: str = "SANDBOX"
    ebay_marketplace: str = "EBAY_DE"
    ebay_verification_token: str = ""

    # Ollama
    ollama_host: str = "http://localhost:11434"
    ollama_model: str = "qwen2.5vl:7b"

    # DHL
    dhl_api_key: str = ""
    dhl_api_secret: str = ""
    dhl_username: str = ""
    dhl_password: str = ""
    dhl_environment: str = "SANDBOX"
    dhl_billing_number: str = ""

    # Sender address
    sender_name: str = ""
    sender_street: str = ""
    sender_postal_code: str = ""
    sender_city: str = ""
    sender_country: str = "DE"

    # SMTP
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    notify_email: str = ""

    # Camera
    camera_device: str = "/dev/video0"
    camera_type: str = "usb"

    # App
    app_host: str = "0.0.0.0"
    app_port: int = 8080
    data_dir: str = "/app/data"
    secret_key: str = "change-me-to-a-random-string"

    @property
    def images_dir(self) -> Path:
        p = Path(self.data_dir) / "images"
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def db_url(self) -> str:
        db_path = Path(self.data_dir) / "iceseller.db"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        return f"sqlite:///{db_path}"

    @property
    def ebay_api_base(self) -> str:
        if self.ebay_environment == "PRODUCTION":
            return "https://api.ebay.com"
        return "https://api.sandbox.ebay.com"

    @property
    def ebay_auth_base(self) -> str:
        if self.ebay_environment == "PRODUCTION":
            return "https://auth.ebay.com"
        return "https://auth.sandbox.ebay.com"

    @property
    def dhl_api_base(self) -> str:
        if self.dhl_environment == "PRODUCTION":
            return "https://api-eu.dhl.com"
        return "https://api-sandbox.dhl.com"

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


settings = Settings()
