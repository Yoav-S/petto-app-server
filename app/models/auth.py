"""
auth.py — Request/response models for registration and OTP verification.
"""
from pydantic import BaseModel, EmailStr, Field


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)


class VerifyOtpRequest(BaseModel):
    email: EmailStr
    otp: str = Field(min_length=4, max_length=4, pattern=r"^\d{4}$")
    password: str = Field(min_length=8, max_length=128)


class ResendOtpRequest(BaseModel):
    email: EmailStr


class AuthMessageResponse(BaseModel):
    message: str
