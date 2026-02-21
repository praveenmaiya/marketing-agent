"""BigQuery client wrapper."""

import logging
import re
from pathlib import Path
from typing import Any, Optional

import pandas as pd
from google.cloud import bigquery

logger = logging.getLogger(__name__)


class BQClient:
    """BigQuery client wrapper with query file support."""

    def __init__(
        self,
        project: str = None,
        dataset: str = None,
        location: str = "US",
    ):
        """Initialize BigQuery client.

        Args:
            project: GCP project ID.
            dataset: Default dataset name.
            location: BigQuery location.
        """
        self.client = bigquery.Client(project=project, location=location)
        self.project = project or self.client.project
        self.dataset = dataset
        self.location = location

    def run_query(
        self,
        query: str,
        params: dict[str, Any] = None,
        dry_run: bool = False,
    ) -> pd.DataFrame:
        """Run a SQL query.

        Args:
            query: SQL query string.
            params: Query parameters.
            dry_run: If True, only validate and estimate cost.

        Returns:
            Query results as DataFrame.
        """
        # Substitute ${VAR} placeholders
        query = self._substitute_placeholders(query, params)

        job_config = bigquery.QueryJobConfig()

        # Set up parameterized query if needed
        if params:
            query_params = self._build_query_params(params)
            job_config.query_parameters = query_params

        if dry_run:
            job_config.dry_run = True

        job = self.client.query(query, job_config=job_config)

        if dry_run:
            bytes_processed = job.total_bytes_processed
            logger.info(f"Dry run: {bytes_processed / 1e9:.2f} GB estimated")
            return pd.DataFrame()

        result = job.result()
        df = result.to_dataframe()

        logger.info(f"Query returned {len(df)} rows")
        return df

    def run_query_file(
        self,
        file_path: str,
        params: dict[str, Any] = None,
        dry_run: bool = False,
    ) -> pd.DataFrame:
        """Run a SQL query from file.

        Args:
            file_path: Path to SQL file.
            params: Query parameters.
            dry_run: If True, only validate and estimate cost.

        Returns:
            Query results as DataFrame.
        """
        path = Path(file_path)
        if not path.exists():
            raise FileNotFoundError(f"SQL file not found: {file_path}")

        query = path.read_text()
        return self.run_query(query, params, dry_run)

    def _substitute_placeholders(
        self,
        query: str,
        params: dict[str, Any] = None,
    ) -> str:
        """Substitute ${VAR} placeholders in query.

        Args:
            query: SQL query with placeholders.
            params: Parameters to substitute.

        Returns:
            Query with placeholders substituted.
        """
        # Always substitute PROJECT and DATASET
        defaults = {
            "PROJECT": self.project,
            "DATASET": self.dataset or "",
        }

        all_params = {**defaults, **(params or {})}

        def replacer(match):
            var_name = match.group(1)
            if var_name in all_params:
                return str(all_params[var_name])
            return match.group(0)

        return re.sub(r'\$\{([^}]+)\}', replacer, query)

    def _build_query_params(
        self,
        params: dict[str, Any],
    ) -> list[bigquery.ScalarQueryParameter]:
        """Build BigQuery query parameters.

        Args:
            params: Parameter dictionary.

        Returns:
            List of BigQuery query parameters.
        """
        query_params = []

        for name, value in params.items():
            if isinstance(value, int):
                param_type = "INT64"
            elif isinstance(value, float):
                param_type = "FLOAT64"
            elif isinstance(value, bool):
                param_type = "BOOL"
            else:
                param_type = "STRING"

            query_params.append(
                bigquery.ScalarQueryParameter(name, param_type, value)
            )

        return query_params

    def table_exists(self, table_id: str) -> bool:
        """Check if a table exists.

        Args:
            table_id: Full table ID (project.dataset.table).

        Returns:
            True if table exists.
        """
        try:
            self.client.get_table(table_id)
            return True
        except Exception:
            return False

    def get_table_schema(
        self,
        table_id: str,
    ) -> list[dict[str, str]]:
        """Get table schema.

        Args:
            table_id: Full table ID.

        Returns:
            List of column definitions.
        """
        table = self.client.get_table(table_id)
        return [
            {"name": field.name, "type": field.field_type}
            for field in table.schema
        ]

    def write_table(
        self,
        df: pd.DataFrame,
        table_id: str,
        write_disposition: str = "WRITE_TRUNCATE",
    ) -> None:
        """Write DataFrame to BigQuery table.

        Args:
            df: DataFrame to write.
            table_id: Full table ID.
            write_disposition: WRITE_TRUNCATE, WRITE_APPEND, or WRITE_EMPTY.
        """
        job_config = bigquery.LoadJobConfig(
            write_disposition=write_disposition,
        )

        job = self.client.load_table_from_dataframe(
            df, table_id, job_config=job_config
        )
        job.result()

        logger.info(f"Wrote {len(df)} rows to {table_id}")
