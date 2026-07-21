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
    # Storage bucket for user uploads (pet/health photos). Defaults to the
    # project's Firebase Storage bucket when left empty.
    FIREBASE_STORAGE_BUCKET: str = ""
    
    # Deep Links
    DEEP_LINK_SCHEME: str
    DEEP_LINK_DOMAIN: str

    # Push notifications (Expo)
    #   EXPO_ACCESS_TOKEN     optional — only needed if you enable "Enhanced
    #                         Security for Push Notifications" in Expo.
    #   INTERNAL_TASK_SECRET  shared secret the reminder dispatcher requires.
    #                         Cloud Scheduler must send it as X-Internal-Secret.
    #                         If empty, the dispatch endpoint is disabled (safe default).
    #   DEFAULT_TIMEZONE      fallback IANA tz used when a user's tz is unknown.
    EXPO_ACCESS_TOKEN: str = ""
    INTERNAL_TASK_SECRET: str = ""
    DEFAULT_TIMEZONE: str = "UTC"

    # Email (OTP) — Resend API (no SMTP) OR classic SMTP; else OTP logged in dev
    RESEND_API_KEY: str = ""
    RESEND_FROM_EMAIL: str = ""
    SMTP_HOST: str = ""
    SMTP_PORT: int = 587
    SMTP_USERNAME: str = ""
    SMTP_PASSWORD: str = ""
    SMTP_FROM_EMAIL: str = ""
    SMTP_USE_TLS: bool = True

    # RevenueCat — webhook auth (Bearer secret you set in RC dashboard).
    # Public SDK keys live on the client only (EXPO_PUBLIC_REVENUECAT_*).
    REVENUECAT_WEBHOOK_SECRET: str = ""
    REVENUECAT_API_KEY: str = ""
    
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
    def firebase_storage_bucket(self) -> str:
        """Resolved Storage bucket name (explicit env or project default)."""
        if self.FIREBASE_STORAGE_BUCKET.strip():
            return self.FIREBASE_STORAGE_BUCKET.strip()
        if self.FIREBASE_PROJECT_ID.strip():
            return f"{self.FIREBASE_PROJECT_ID.strip()}.firebasestorage.app"
        return ""

    @property
    def resend_configured(self) -> bool:
        return bool(self.RESEND_API_KEY.strip() and self.RESEND_FROM_EMAIL.strip())

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
