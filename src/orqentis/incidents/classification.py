"""Deterministic error-signature classification.

Signatures are data: deployments can add their own (``ErrorClassifier.register``) and map them
to built-in or registered taxonomy categories.
"""

from __future__ import annotations

import re
from functools import cached_property

from pydantic import BaseModel, Field

from orqentis.domain.taxonomy import CategoryName, IncidentCategory


class ErrorSignature(BaseModel):
    id: str
    category: CategoryName
    description: str
    patterns: list[str] = Field(min_length=1)
    # Named groups extracted from any matching pattern, e.g. ``(?P<column>...)``.
    entity_patterns: list[str] = Field(default_factory=list)
    priority: int = 50

    @cached_property
    def compiled(self) -> list[re.Pattern[str]]:
        return [re.compile(p) for p in self.patterns]

    @cached_property
    def compiled_entities(self) -> list[re.Pattern[str]]:
        return [re.compile(p) for p in self.entity_patterns]


class SignatureMatch(BaseModel):
    signature_id: str
    category: str
    description: str
    matched_text: str
    entities: dict[str, str] = Field(default_factory=dict)
    priority: int


DEFAULT_SIGNATURES: tuple[ErrorSignature, ...] = (
    ErrorSignature(
        id="SIG-AUTH-ENTRA",
        category=IncidentCategory.AUTH_FAILURE,
        description="Microsoft Entra ID authentication failure (expired/invalid credential or token)",
        patterns=[
            r"\bAADSTS\d{5,}\b",
            r"(?i)client secret (keys? )?.{0,60}expired",
            r"(?i)\binvalid_client\b",
            r"(?i)\b(access )?token (has )?expired\b",
            r"(?i)\b(credentials?|password) (are |is )?(invalid|expired)\b",
            r"(?i)\b401\b.{0,40}unauthori[sz]ed",
            # the analysis engine's wording when a data source refuses the credentials stored in the model
            r"(?i)credentials provided for the .{1,60} source (?:are|is) invalid",
        ],
        entity_patterns=[r"for app '(?P<app>[^']+)'", r"connection (?P<connection>conn-[\w-]+)"],
        priority=100,
    ),
    ErrorSignature(
        id="SIG-CAPACITY-LIMIT",
        category=IncidentCategory.CAPACITY_PRESSURE,
        description="Fabric capacity limits exceeded (throttling / background rejection)",
        patterns=[
            r"(?i)\bCapacityLimitExceeded\b",
            r"(?i)\bTooManyRequestsForCapacity\b",
            r"(?i)capacity .{0,60}(exceeded its limits|is overloaded|throttl)",
            r"(?i)\bHTTP 430\b",
        ],
        entity_patterns=[r"[Cc]apacity '(?P<capacity>[^']+)'"],
        priority=95,
    ),
    ErrorSignature(
        id="SIG-SCHEMA-MISSING-COLUMN",
        category=IncidentCategory.SCHEMA_DRIFT,
        description="Expected column missing / unresolvable in the input schema",
        patterns=[
            r"(?i)column\s+[\"'`]?[\w.]+[\"'`]?\s+(is missing|not found|does not exist|cannot be resolved)",
            r"\bUNRESOLVED_COLUMN\b",
            r"(?i)cannot resolve [\"'`]?\w+",
            r"(?i)\bschema mismatch\b",
        ],
        entity_patterns=[
            r"(?i)column\s+[\"'`]?(?P<column>[\w.]+)[\"'`]?\s+(?:is missing|not found|does not exist|cannot be)",
            # Spark 3.4+: "[UNRESOLVED_COLUMN.WITH_SUGGESTION] A column, variable, or function parameter with name
            # `customer_id` cannot be resolved. Did you mean one of the following? [`client_id`, `full_name`]". Fabric
            # nests this inside a JSON string, so the quotes around it may be escaped: the backticks are not.
            r"(?i)\bwith name\s+[`'\"\\]*(?P<column>[\w.]+)[`'\"\\]*\s+cannot be resolved",
            r"(?i)cannot resolve [\"'`]?(?P<column>\w+)",
            r"(?i)input schema of (?P<table>\w+)",
            r"(?i)did you mean one of the following\?\s*\[(?P<suggestions>[^\]]{1,300})\]",
        ],
        priority=90,
    ),
    ErrorSignature(
        id="SIG-DELTA-WRITE",
        category=IncidentCategory.LAKEHOUSE_WRITE_FAILURE,
        description="Delta / Lakehouse write failure",
        patterns=[
            r"\b(DeltaWriteException|ConcurrentAppendException|ConcurrentModificationException|"
            r"ProtocolChangedException)\b",
            r"(?i)failed to write (to )?(the )?(delta|table|lakehouse)",
        ],
        priority=80,
    ),
    ErrorSignature(
        id="SIG-TIMEOUT",
        category=IncidentCategory.PIPELINE_TIMEOUT,
        description="Activity or pipeline exceeded its timeout",
        patterns=[
            r"(?i)\btimed out\b",
            r"(?i)exceeded (the )?(activity )?timeout",
            r"(?i)\btimeout (expired|elapsed)\b",
        ],
        priority=75,
    ),
    ErrorSignature(
        id="SIG-API-THROTTLING",
        category=IncidentCategory.THROTTLING,
        description="Service-side API throttling (HTTP 429)",
        patterns=[r"\b429\b", r"\bTooManyRequests(?!ForCapacity)\b", r"(?i)\brate limit(ed)?\b"],
        priority=70,
    ),
    ErrorSignature(
        id="SIG-CONNECTIVITY",
        category=IncidentCategory.CONNECTIVITY_FAILURE,
        description="Network connectivity failure to a source or gateway",
        patterns=[
            r"(?i)\b(could not|failed to|unable to) (connect|establish (a )?connection)\b",
            r"(?i)\bconnection (refused|reset)\b",
            r"(?i)\b(name resolution|getaddrinfo|no such host)\b",
            r"(?i)\bgateway (is )?(offline|unreachable)\b",
        ],
        priority=60,
    ),
    ErrorSignature(
        id="SIG-MODEL-REFRESH",
        category=IncidentCategory.SEMANTIC_MODEL_REFRESH_FAILURE,
        description="Semantic model refresh failed in the analysis engine",
        patterns=[
            r"\bModelRefresh_ShortMessage_\w+",
            r"(?i)we cannot access the source (?:delta )?table",
            r"(?i)\b(?:semantic model|dataset)\b.{0,40}\brefresh\b.{0,40}\bfail",
        ],
        entity_patterns=[
            r"(?i)source (?:delta )?table '(?P<table>[^']{1,128})'",
            r"(?i)referenced by table '(?P<model_table>[^']{1,128})'",
        ],
        priority=65,
    ),
    ErrorSignature(
        id="SIG-SOURCE-UNAVAILABLE",
        category=IncidentCategory.SOURCE_UNAVAILABLE,
        description="Source object or service unavailable",
        patterns=[
            r"(?i)\b(source|file|path|object|blob|folder) (was )?not found\b",
            r"(?i)\bservice unavailable\b",
            r"\b503\b",
        ],
        priority=55,
    ),
    ErrorSignature(
        id="SIG-NOTEBOOK-EXCEPTION",
        category=IncidentCategory.ACTIVITY_FAILURE,
        description="A notebook or Spark job raised an exception (its own code or data, not the platform)",
        patterns=[
            r"Traceback \(most recent call last\)",
            r"(?i)\bnotebook execution (?:is in )?failed\b",
            r"\b(?:Py4JJavaError|PythonException|SparkException|AnalysisException)\b",
            r"(?i)errorName\W{1,8}[A-Za-z_][\w.]*(?:Error|Exception)\b",
            r"\b[A-Z][A-Za-z0-9_]*(?:Error|Exception)\s*:",
        ],
        entity_patterns=[
            # Fabric's wrapper: ..."errorName":"RuntimeError","errorValue":"<message>","message":"Notebook execution..."
            r"(?i)errorName\W{1,8}(?P<exception>[A-Za-z_][\w.]*)",
            r"(?i)errorValue\W{1,8}(?P<exception_message>[^\\\"]{1,300})",
            r"\b(?P<exception>[A-Z][A-Za-z0-9_]*(?:Error|Exception))\s*:\s*(?P<exception_message>[^\n]{1,300})",
        ],
        priority=30,
    ),
)


class ErrorClassifier:
    def __init__(self, signatures: tuple[ErrorSignature, ...] | list[ErrorSignature] = DEFAULT_SIGNATURES) -> None:
        self._signatures = sorted(signatures, key=lambda s: -s.priority)

    def register(self, signature: ErrorSignature) -> None:
        self._signatures = sorted([*self._signatures, signature], key=lambda s: -s.priority)

    @property
    def signatures(self) -> list[ErrorSignature]:
        return list(self._signatures)

    def matches(self, text: str) -> list[SignatureMatch]:
        found: list[SignatureMatch] = []
        for signature in self._signatures:
            hit = next((m for p in signature.compiled if (m := p.search(text))), None)
            if hit is None:
                continue
            entities: dict[str, str] = {}
            for pattern in signature.compiled_entities:
                if m := pattern.search(text):
                    for key, value in m.groupdict().items():
                        if value and key not in entities:
                            entities[key] = value
            found.append(
                SignatureMatch(
                    signature_id=signature.id,
                    category=signature.category,
                    description=signature.description,
                    matched_text=hit.group(0),
                    entities=entities,
                    priority=signature.priority,
                )
            )
        return found

    def classify(self, text: str) -> SignatureMatch | None:
        """Highest-priority matching signature, or ``None`` (the incident stays UNKNOWN)."""
        matches = self.matches(text)
        return matches[0] if matches else None
