from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import Optional

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
    
    @property
    def is_development(self) -> bool:
        return self.APP_ENV == "development"

    @property
    def is_production(self) -> bool:
        return self.APP_ENV == "production"

    # Automatically load from .env file for local development.
    # In Google Cloud (production), it will read directly from the environment variables.
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

settings = Settings()
