from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    # Core
    APP_ENV: str = "development"
    PORT: int = 8080
    CLIENT_APP_URL: str
    
    # MongoDB
    MONGODB_URI: str
    MONGODB_DB_NAME: str = "petto"
    
    # Firebase
    FIREBASE_PROJECT_ID: str
    FIREBASE_CLIENT_EMAIL: str
    FIREBASE_PRIVATE_KEY: str
    
    # Deep Links
    DEEP_LINK_SCHEME: str
    DEEP_LINK_DOMAIN: str

    # Email (OTP) — optional in development (OTP logged to console)
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
    def smtp_configured(self) -> bool:
        return bool(
            self.SMTP_HOST
            and self.SMTP_FROM_EMAIL
            and self.SMTP_USERNAME
            and self.SMTP_PASSWORD
        )

    # Automatically load from .env file for local development.
    # In Google Cloud (production), it will read directly from the environment variables.
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

settings = Settings()
