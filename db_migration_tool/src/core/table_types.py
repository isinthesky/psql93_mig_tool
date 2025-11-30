"""
Table Types Module

Defines constants and metadata for different partition table types
supported by the migration tool.
"""

from enum import Enum
from dataclasses import dataclass
from typing import List, Tuple


class TableType(str, Enum):
    """Supported partition table types"""
    POINT_HISTORY = "PH"
    TREND_HISTORY = "TH"
    ENERGY_DISPLAY = "ED"
    RUNNING_TIME_HISTORY = "RT"

    @property
    def table_name(self) -> str:
        """Get the actual database table name"""
        return TABLE_TYPE_CONFIG[self].table_name

    @property
    def display_name(self) -> str:
        """Get the human-readable display name"""
        return TABLE_TYPE_CONFIG[self].display_name

    @property
    def uses_trigger(self) -> bool:
        """Check if this table type uses TRIGGER-based partitioning"""
        return TABLE_TYPE_CONFIG[self].uses_trigger

    @property
    def uses_rules(self) -> bool:
        """Check if this table type uses RULE-based partitioning"""
        return TABLE_TYPE_CONFIG[self].uses_rules

    @property
    def date_column(self) -> str:
        """Get the date column name"""
        return TABLE_TYPE_CONFIG[self].date_column

    @property
    def date_is_timestamp(self) -> bool:
        """Check if date column is timestamp (True) or bigint (False)"""
        return TABLE_TYPE_CONFIG[self].date_is_timestamp

    @property
    def columns(self) -> List[str]:
        """Get the list of column names"""
        return TABLE_TYPE_CONFIG[self].columns


@dataclass
class TableTypeConfig:
    """Configuration for a specific table type"""
    table_name: str
    display_name: str
    uses_trigger: bool
    uses_rules: bool
    date_column: str
    date_is_timestamp: bool  # True for timestamp, False for bigint
    columns: List[str]
    description: str


# Table type configurations
TABLE_TYPE_CONFIG = {
    TableType.POINT_HISTORY: TableTypeConfig(
        table_name="point_history",
        display_name="Point History",
        uses_trigger=True,
        uses_rules=False,
        date_column="issued_date",
        date_is_timestamp=False,  # bigint (Unix timestamp ms)
        columns=["path_id", "issued_date", "changed_value", "connection_status"],
        description="Point history data with TRIGGER-based partitioning"
    ),

    TableType.TREND_HISTORY: TableTypeConfig(
        table_name="trend_history",
        display_name="Trend History",
        uses_trigger=False,
        uses_rules=True,
        date_column="issued_date",
        date_is_timestamp=False,  # bigint (Unix timestamp ms)
        columns=["path_id", "issued_date", "changed_value", "connection_status"],
        description="Trend history data with RULE-based partitioning"
    ),

    TableType.ENERGY_DISPLAY: TableTypeConfig(
        table_name="energy_display",
        display_name="Energy Display",
        uses_trigger=False,
        uses_rules=True,
        date_column="issued_date",
        date_is_timestamp=True,  # timestamp without time zone
        columns=["sensor_id", "issued_date", "station_id", "value", "co2", "cost"],
        description="Energy display data with RULE-based partitioning (timestamp)"
    ),

    TableType.RUNNING_TIME_HISTORY: TableTypeConfig(
        table_name="running_time_history",
        display_name="Running Time History",
        uses_trigger=False,
        uses_rules=True,
        date_column="issued_date",
        date_is_timestamp=False,  # bigint (Unix timestamp ms)
        columns=[
            "path_id", "issued_date", "save_type", "checked_time",
            "running_time", "accu_time", "running_count",
            "eng_value", "eng_accu_value", "previous_weight_value"
        ],
        description="Running time history data with RULE-based partitioning"
    ),
}


def get_table_type(table_name: str) -> TableType:
    """
    Get TableType enum from table name

    Args:
        table_name: Database table name (e.g., 'point_history')

    Returns:
        TableType enum

    Raises:
        ValueError: If table name is not recognized
    """
    for table_type, config in TABLE_TYPE_CONFIG.items():
        if config.table_name == table_name:
            return table_type
    raise ValueError(f"Unknown table name: {table_name}")


def get_table_name(table_type: TableType) -> str:
    """
    Get database table name from TableType

    Args:
        table_type: TableType enum

    Returns:
        Database table name
    """
    return TABLE_TYPE_CONFIG[table_type].table_name


def get_all_table_types() -> List[TableType]:
    """Get list of all supported table types"""
    return list(TableType)


def get_all_table_names() -> List[str]:
    """Get list of all supported table names"""
    return [config.table_name for config in TABLE_TYPE_CONFIG.values()]


# Default table type for backward compatibility
DEFAULT_TABLE_TYPE = TableType.POINT_HISTORY
