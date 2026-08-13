"""
auth.py — Request/response models for passwordless email OTP auth.
"""
from pydantic import BaseModel, EmailStr, Field


class SendOtpRequest(BaseModel):
    email: EmailStr
    # App/device language for the OTP email (en|he|ro|ru).
    locale: str | None = Field(default=None, max_length=16)


class VerifyOtpRequest(BaseModel):
    email: EmailStr
    otp: str = Field(min_length=6, max_length=6, pattern=r"^\d{6}$")


class ResendOtpRequest(BaseModel):
    email: EmailStr
    locale: str | None = Field(default=None, max_length=16)


class AuthMessageResponse(BaseModel):
    message: str


class VerifyOtpResponse(BaseModel):
    custom_token: str
