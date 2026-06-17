from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    # Core
    APP_ENV: str = "development"
    PORT: int = 8080
    CLIENT_APP_URL: str
    
    # MongoDB
    MONGODB_URI: str
    MONGODB_DB_NAME: str = "petto"
    
    # Firebase — use FIREBASE_PRIVATE_KEY_BASE64 on Cloud Run if PEM newlines are awkward
    FIREBASE_PROJECT_ID: str
    FIREBASE_CLIENT_EMAIL: str
    FIREBASE_PRIVATE_KEY: str = ""
    FIREBASE_PRIVATE_KEY_BASE64: str = ""
    
    # Deep Links
    DEEP_LINK_SCHEME: str
    DEEP_LINK_DOMAIN: str

    # Email (OTP) — Resend API (no SMTP) OR classic SMTP; else OTP logged in dev
    RESEND_API_KEY: str = ""
    RESEND_FROM_EMAIL: str = ""
    SMTP_HOST: str = ""
    SMTP_PORT: int = 587
    SMTP_USERNAME: str = ""
    SMTP_PASSWORD: str = ""
    SMTP_FROM_EMAIL: str = ""
    SMTP_USE_TLS: bool = True
    
    @property
    def is_development(self) -> bool:
        return self.APP_ENV == "development"

    @property
    def is_production(self) -> bool:
        return self.APP_ENV == "production"

    @property
    def mongodb_db_name(self) -> str:
        """Strip accidental whitespace from DB name."""
        return self.MONGODB_DB_NAME.strip()

    @property
    def resend_configured(self) -> bool:
        return bool(self.RESEND_API_KEY and self.RESEND_FROM_EMAIL)

    @property
    def smtp_configured(self) -> bool:
        return bool(
            self.SMTP_HOST
            and self.SMTP_FROM_EMAIL
            and self.SMTP_USERNAME
            and self.SMTP_PASSWORD
        )

    @property
    def email_configured(self) -> bool:
        return self.resend_configured or self.smtp_configured

    @model_validator(mode="after")
    def firebase_private_key_present(self):
        if not self.FIREBASE_PRIVATE_KEY.strip() and not self.FIREBASE_PRIVATE_KEY_BASE64.strip():
            raise ValueError("Set FIREBASE_PRIVATE_KEY or FIREBASE_PRIVATE_KEY_BASE64")
        return self

    # Automatically load from .env file for local development.
    # In Google Cloud (production), it will read directly from the environment variables.
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

settings = Settings()
