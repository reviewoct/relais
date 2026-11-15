"""Pydantic models: what comes in over HTTP and what the model is allowed to say."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field

Category = Literal["livraison", "remboursement", "facturation", "technique", "compte", "autre"]
Urgency = Literal["basse", "normale", "haute"]
Team = Literal["logistique", "comptabilite", "technique", "direction"]


class Classification(BaseModel):
    category: Category
    urgency: Urgency
    language: Literal["fr", "en", "ar", "autre"]
    sentiment: Literal["positif", "neutre", "negatif"]
    summary: str = Field(description="Une phrase, en français, qui résume la demande.")
    order_ref: str | None = Field(
        default=None, description="Référence de commande citée dans le message, sinon null."
    )


class InboundEmail(BaseModel):
    message_id: str = Field(min_length=1, max_length=255)
    from_email: EmailStr
    from_name: str = Field(default="", max_length=255)
    subject: str = Field(min_length=1, max_length=500)
    body: str = Field(min_length=1, max_length=20_000)


# --- Tool inputs --------------------------------------------------------------
# The JSON Schemas sent to the model live in tools.py. These models re-validate
# every call server-side, with the constraints a JSON Schema cannot be trusted for.


class _ToolInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class NoInput(_ToolInput):
    pass


class GetOrderIn(_ToolInput):
    order_ref: str = Field(min_length=3, max_length=32)


class SearchPolicyIn(_ToolInput):
    query: str = Field(min_length=2, max_length=300)


class ProposeRefundIn(_ToolInput):
    order_ref: str = Field(min_length=3, max_length=32)
    amount_eur: float = Field(gt=0, le=100_000)
    reason: str = Field(min_length=3, max_length=500)


class ProposeEscalationIn(_ToolInput):
    team: Team
    reason: str = Field(min_length=3, max_length=500)


class SubmitReplyIn(_ToolInput):
    subject: str = Field(min_length=1, max_length=500)
    body: str = Field(min_length=20, max_length=6000)
    internal_summary: str = Field(min_length=3, max_length=3000)
    confidence: float = Field(ge=0, le=1)
