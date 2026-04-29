from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def row_hash(row: dict[str, Any]) -> str:
    payload = json.dumps(row, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def new_batch_id() -> str:
    return str(uuid.uuid4())


class _OpenDataRecord(BaseModel):
    """Bronze boundary: accept source-shaped rows, require only stable identity."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    external_id: str = Field(alias="_id")

    @field_validator("external_id", mode="before")
    @classmethod
    def _external_id_present(cls, value: Any) -> str:
        if value is None or str(value).strip() == "":
            raise ValueError("_id is required")
        return str(value)


class FedGrantRecord(_OpenDataRecord):
    recipient_legal_name: str | None = None
    recipient_business_number: str | None = None
    agreement_value: float | None = None

    @field_validator("agreement_value", mode="before")
    @classmethod
    def _coerce_amount(cls, value: Any) -> float | None:
        if value is None or value == "":
            return None
        return float(value)


class CraT3010Record(_OpenDataRecord):
    bn: str | None = Field(default=None, alias="BN")

    @field_validator("bn")
    @classmethod
    def _bn_present(cls, value: str | None) -> str | None:
        if value is None or not str(value).strip():
            raise ValueError("BN is required")
        return value


class CraDoneesRecord(_OpenDataRecord):
    bn: str | None = Field(default=None, alias="BN")
    donee_bn: str | None = Field(default=None, alias="Donee BN")
    donee_name: str | None = Field(default=None, alias="Donee Name")
    total_gifts: float | None = Field(default=None, alias="Total Gifts")

    @field_validator("bn", "donee_bn")
    @classmethod
    def _some_bn_text(cls, value: str | None) -> str | None:
        return str(value).strip() if value is not None else None

    @field_validator("total_gifts", mode="before")
    @classmethod
    def _coerce_gifts(cls, value: Any) -> float | None:
        if value is None or value == "":
            return None
        return float(value)


_MODELS: dict[str, type[_OpenDataRecord]] = {
    "fed_grants": FedGrantRecord,
    "cra_t3010": CraT3010Record,
    "cra_donees": CraDoneesRecord,
}


def validate_rows(
    source: str,
    rows: list[dict[str, Any]],
    batch_id: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return bronze-valid rows and quarantine records with reasons."""
    model = _MODELS.get(source)
    if model is None:
        return rows, []

    valid: list[dict[str, Any]] = []
    quarantine: list[dict[str, Any]] = []
    for row in rows:
        digest = row_hash(row)
        try:
            model.model_validate(row)
        except (ValidationError, ValueError) as exc:
            quarantine.append(
                {
                    "batch_id": batch_id,
                    "source": source,
                    "external_id": str(row.get("_id")) if row.get("_id") is not None else None,
                    "row_hash": digest,
                    "raw_row": row,
                    "error": str(exc),
                }
            )
            continue
        enriched = dict(row)
        enriched["_batch_id"] = batch_id
        enriched["_row_hash"] = digest
        enriched["_bronze_validated_at"] = utcnow_iso()
        valid.append(enriched)
    return valid, quarantine
