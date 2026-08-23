from typing import Annotated, Literal

from pydantic import BaseModel, Field, StringConstraints, model_validator

SAFE_STR = Annotated[str, StringConstraints(max_length=200)]


class WorkbookCreate(BaseModel):
    title: str = Field(min_length=1, max_length=120)


class ExtractTextPayload(BaseModel):
    raw_text: str = Field(min_length=1, max_length=200_000)


class ImportEntry(BaseModel):
    number: int = Field(ge=1, le=999)
    answer: str = Field(default="", max_length=60)
    line: int = Field(default=0, ge=0)


class ImportHeader(BaseModel):
    type: str
    label: str = Field(min_length=1, max_length=40)
    index: int = 0
    line: int = Field(default=0, ge=0)


class SectionResolution(BaseModel):
    incoming_label: str = Field(min_length=1, max_length=120)
    action: Literal["overwrite", "keep_both", "skip_incoming"]
    target_section_id: int | None = Field(default=None, ge=1, le=2**63 - 1)


class SectionImport(BaseModel):
    structure: Literal["headers", "chunks"]
    header_type: str | None = None
    chunk_size: int | None = Field(default=None, ge=0, le=500)
    entries: list[ImportEntry] = Field(min_length=1)
    headers: list[ImportHeader] = []
    resolutions: list[SectionResolution] = []


class AttemptCreate(BaseModel):
    section_id: int = Field(ge=1, le=2**63 - 1)
    answers: dict[str, SAFE_STR] = Field(default_factory=dict)
    merge_attempt_id: int | None = Field(default=None, ge=1, le=2**63 - 1)

    @model_validator(mode="after")
    def _cap_answers(self):
        if len(self.answers) > 500:
            raise ValueError("answers 항목이 너무 많습니다 (최대 500개).")
        return self


class FromMissesPayload(BaseModel):
    attempt_id: int = Field(ge=1, le=2**63 - 1)


class ApiKeyPayload(BaseModel):
    api_key: str = Field(min_length=1, max_length=300)
