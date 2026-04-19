from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class MindroomDevParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ISSUE_REF: str
    REPO: Literal["mindroom", "cinny", "nixos", "tuwunel"]
    BRANCH: str = ""
    N_REVIEWERS: int = Field(default=8, ge=1)
    IMPLEMENTER_AGENT: str = "codex"
    IS_PR: bool = True
    BASE: Literal["origin/main", "main"] = "origin/main"

    @model_validator(mode="after")
    def apply_derived_defaults(self) -> "MindroomDevParams":
        if self.BRANCH == "":
            self.BRANCH = self.ISSUE_REF.lower()
        if "BASE" not in self.model_fields_set:
            self.BASE = "origin/main" if self.IS_PR else "main"
        return self


class ParallelReviewLoopParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    N_REVIEWERS: int = Field(default=8, ge=1)


class Todo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = None
    sub_template: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    priority: Literal["low", "medium", "high", "critical"] = "medium"
    depends_on: list[int] = Field(default_factory=list)
    assigned_agent: str | None = None

    @model_validator(mode="after")
    def exactly_one_of_title_or_sub_template(self) -> "Todo":
        if (self.title is None) == (self.sub_template is None):
            raise ValueError(
                "Each todo must have exactly one of `title` or `sub_template`"
            )
        if self.title is not None:
            invalid_fields = self.model_fields_set - {
                "title",
                "priority",
                "depends_on",
                "assigned_agent",
            }
            if invalid_fields:
                invalid = ", ".join(f"`{field}`" for field in sorted(invalid_fields))
                raise ValueError(
                    "Todo with `title` cannot use "
                    f"{invalid} (sub-template field); each todo is exactly one of "
                    "regular or sub-template"
                )
        else:
            invalid_fields = self.model_fields_set - {
                "sub_template",
                "params",
                "depends_on",
            }
            if invalid_fields:
                invalid = ", ".join(f"`{field}`" for field in sorted(invalid_fields))
                raise ValueError(
                    "Todo with `sub_template` cannot use "
                    f"{invalid} (regular todo field); each todo is exactly one of "
                    "regular or sub-template"
                )
        return self


class TemplateDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    version: str
    description: str
    todos: list[Todo] = Field(min_length=1)


PARAMS_SCHEMAS: dict[str, type[BaseModel]] = {
    "mindroom-dev": MindroomDevParams,
    "parallel-review-loop": ParallelReviewLoopParams,
}
