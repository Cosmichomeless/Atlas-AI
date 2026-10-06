import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

PASSWORD_MIN_LENGTH = 10
PASSWORD_MAX_LENGTH = 128


class RegisterRequest(BaseModel):
    email: EmailStr = Field(max_length=254, description="Se guarda en minúsculas.")
    password: str = Field(
        description=f"Entre {PASSWORD_MIN_LENGTH} y {PASSWORD_MAX_LENGTH} caracteres.",
        json_schema_extra={"format": "password", "writeOnly": True},
    )

    @field_validator("password")
    @classmethod
    def _check_password(cls, value: str) -> str:
        if not PASSWORD_MIN_LENGTH <= len(value) <= PASSWORD_MAX_LENGTH:
            raise ValueError(
                f"La contraseña debe tener entre {PASSWORD_MIN_LENGTH} y "
                f"{PASSWORD_MAX_LENGTH} caracteres."
            )
        if not value.strip():
            raise ValueError("La contraseña no puede estar formada solo por espacios.")
        return value


class UserPublic(BaseModel):
    """Datos de la cuenta visibles para su titular. Nunca incluye el hash."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: str
    created_at: datetime
