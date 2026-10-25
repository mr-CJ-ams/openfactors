"""Ingestion connector."""

from openfactors.ingestion.checkpoints import (
    NATIONAL_LOCATION,
    CheckpointKey,
    MemoryCheckpoints,
    job_identity,
)
from openfactors.ingestion.era5 import (
    DailyWeather,
    Era5Slice,
    build_archive_request,
    fetch_era5_daily,
    parse_era5_daily,
    urllib_transport,
)
from openfactors.ingestion.errors import ConnectorError, ErrorCategory, UpstreamTimeout
from openfactors.ingestion.holidays import (
    CompiledYear,
    HolidayDay,
    compile_year,
    join_names,
    load_publication_calendar,
    resolve_day,
)
from openfactors.ingestion.http_client import (
    Clock,
    FetchResult,
    HttpConnector,
    HttpRequest,
    HttpResponse,
    RetryPolicy,
)
from openfactors.ingestion.raw_store import (
    FileObjectStore,
    RawArtifact,
    RetentionLabel,
    commit_raw_artifact,
)
from openfactors.ingestion.runs import MemoryRunLog, RunRecord, execute_run

__all__ = [
    "NATIONAL_LOCATION",
    "CheckpointKey",
    "Clock",
    "CompiledYear",
    "ConnectorError",
    "DailyWeather",
    "Era5Slice",
    "ErrorCategory",
    "FetchResult",
    "FileObjectStore",
    "HttpConnector",
    "HttpRequest",
    "HttpResponse",
    "HolidayDay",
    "MemoryCheckpoints",
    "MemoryRunLog",
    "RawArtifact",
    "RetentionLabel",
    "RetryPolicy",
    "RunRecord",
    "UpstreamTimeout",
    "build_archive_request",
    "commit_raw_artifact",
    "compile_year",
    "execute_run",
    "fetch_era5_daily",
    "job_identity",
    "join_names",
    "load_publication_calendar",
    "parse_era5_daily",
    "resolve_day",
    "urllib_transport",
]
