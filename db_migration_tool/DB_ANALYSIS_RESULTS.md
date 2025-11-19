# Database Analysis Results - Multi-Partition Type Support

## Analysis Date
2025-11-19

## Database Information
- **Database**: bms93 (PostgreSQL 9.3)
- **Host**: localhost:5432

## Partition Table Types Found

### 1. Point History (PH)
- **Parent Table**: `point_history`
- **Partition Count**: Not counted (uses TRIGGER-based partitioning)
- **Partitioning Mechanism**: **TRIGGER**
- **Trigger**: `point_history_trigger` → `point_history_partition_insert()` function

#### Schema
```sql
CREATE TABLE point_history (
    path_id              bigint NOT NULL,
    issued_date          bigint NOT NULL,           -- Unix timestamp (ms)
    changed_value        character varying(100),
    connection_status    boolean
);
```

#### Partitioning Logic
- **Type**: BEFORE INSERT TRIGGER
- **Function**: `point_history_partition_insert()`
- **Date Column**: `issued_date` (bigint - Unix timestamp in milliseconds)

---

### 2. Trend History (TH)
- **Parent Table**: `trend_history`
- **Partition Count**: 59+ partitions
- **Partitioning Mechanism**: **RULES** (59 rules)
- **Naming Pattern**: `trend_history_YYMM`

#### Schema
```sql
CREATE TABLE trend_history (
    path_id              bigint NOT NULL,
    issued_date          bigint NOT NULL,           -- Unix timestamp (ms)
    changed_value        character varying(100),
    connection_status    boolean
);
```

#### Partitioning Logic
- **Type**: INSERT RULES
- **Rule Pattern**: `rule_trend_history_YYMM`
- **Date Column**: `issued_date` (bigint - Unix timestamp in milliseconds)

**Sample Rule**:
```sql
CREATE RULE rule_trend_history_1811 AS
    ON INSERT TO public.trend_history
    WHERE (
        (new.issued_date >= '1540998000000'::bigint) AND
        (new.issued_date <= '1543589999999'::bigint)
    )
    DO INSTEAD INSERT INTO trend_history_1811 (
        path_id, issued_date, changed_value, connection_status
    )
    VALUES (
        new.path_id, new.issued_date, new.changed_value, new.connection_status
    );
```

---

### 3. Energy Display (ED)
- **Parent Table**: `energy_display`
- **Partition Count**: 59+ partitions (found 20 in initial scan)
- **Partitioning Mechanism**: **RULES** (59 rules)
- **Naming Pattern**: `energy_display_YYMM`

#### Schema
```sql
CREATE TABLE energy_display (
    sensor_id            bigint NOT NULL,
    issued_date          timestamp without time zone NOT NULL,  -- TIMESTAMP (not bigint!)
    station_id           character varying(20) NOT NULL,
    value                double precision,
    co2                  double precision,
    cost                 double precision
);
```

#### Partitioning Logic
- **Type**: INSERT RULES
- **Rule Pattern**: `rule_energy_display_YYMM`
- **Date Column**: `issued_date` (timestamp without time zone)
- **Date Range**: 2018-11 to 2023-06 (and beyond)

**Sample Rule**:
```sql
CREATE RULE rule_energy_display_1811 AS
    ON INSERT TO public.energy_display
    WHERE (
        (new.issued_date >= '2018-11-01 00:00:00'::timestamp without time zone) AND
        (new.issued_date <= '2018-11-30 23:59:59'::timestamp without time zone)
    )
    DO INSTEAD INSERT INTO energy_display_1811 (
        sensor_id, issued_date, station_id, value, co2, cost
    )
    VALUES (
        new.sensor_id, new.issued_date, new.station_id,
        new.value, new.co2, new.cost
    );
```

---

### 4. Running Time History (RT)
- **Parent Table**: `running_time_history`
- **Partition Count**: 59+ partitions
- **Partitioning Mechanism**: **RULES** (59 rules)
- **Naming Pattern**: `running_time_history_YYMM`

#### Schema
```sql
CREATE TABLE running_time_history (
    path_id                  integer NOT NULL,
    issued_date              bigint NOT NULL,      -- Unix timestamp (ms)
    save_type                character varying(1) NOT NULL,
    checked_time             bigint,
    running_time             bigint,
    accu_time                bigint,
    running_count            integer,
    eng_value                real,
    eng_accu_value           real,
    previous_weight_value    integer
);
```

#### Partitioning Logic
- **Type**: INSERT RULES
- **Rule Pattern**: `rule_running_time_history_YYMM`
- **Date Column**: `issued_date` (bigint - Unix timestamp in milliseconds)

**Sample Rule**:
```sql
CREATE RULE rule_running_time_history_1811 AS
    ON INSERT TO public.running_time_history
    WHERE (
        (new.issued_date >= '1540998000000'::bigint) AND
        (new.issued_date <= '1543589999999'::bigint)
    )
    DO INSTEAD INSERT INTO running_time_history_1811 (
        path_id, issued_date, save_type, checked_time, running_time,
        accu_time, running_count, eng_value, eng_accu_value, previous_weight_value
    )
    VALUES (
        new.path_id, new.issued_date, new.save_type, new.checked_time, new.running_time,
        new.accu_time, new.running_count, new.eng_value, new.eng_accu_value, new.previous_weight_value
    );
```

---

## Key Findings Summary

### Partitioning Mechanisms
| Table Type | Mechanism | Count | Date Type | Date Column |
|------------|-----------|-------|-----------|-------------|
| point_history | TRIGGER | N/A | bigint | issued_date |
| trend_history | RULES | 59 | bigint | issued_date |
| energy_display | RULES | 59 | timestamp | issued_date |
| running_time_history | RULES | 59 | bigint | issued_date |

### Critical Differences

1. **Date Column Type Variance**:
   - `energy_display` uses `timestamp without time zone`
   - All others use `bigint` (Unix timestamp milliseconds)
   - This requires different WHERE clause generation for RULES

2. **Partitioning Strategy**:
   - `point_history`: Dynamic TRIGGER-based (calls PL/pgSQL function)
   - Others: Static RULE-based (one rule per partition)

3. **Schema Complexity**:
   - `point_history`, `trend_history`: 4 columns (simple)
   - `energy_display`: 6 columns (medium)
   - `running_time_history`: 10 columns (complex)

---

## Implementation Implications

### For `partition_discovery.py`
- Must support filtering by multiple table types
- Query should use `IN ('point_history', 'trend_history', ...)` clause
- May need table-specific logic for partition naming patterns

### For `table_creator.py`
- Must detect table type and choose:
  - **TRIGGER** generation for `point_history`
  - **RULE** generation for `trend_history`, `energy_display`, `running_time_history`
- Must handle different date column types:
  - `bigint` for PH, TH, RT
  - `timestamp` for ED
- Must generate correct INSERT statements based on column count/names

### For Migration Workers
- Checkpoint logic should remain table-agnostic (already stores table name)
- Copy logic should work transparently across all types

---

## Testing Requirements

### Unit Tests
- [ ] Test partition discovery for each table type
- [ ] Test TRIGGER DDL generation for PH
- [ ] Test RULE DDL generation for TH, ED, RT
- [ ] Test correct date type handling in WHERE clauses

### Integration Tests
- [ ] Migrate PH partitions (TRIGGER-based)
- [ ] Migrate TH partitions (RULE-based, bigint dates)
- [ ] Migrate ED partitions (RULE-based, timestamp dates)
- [ ] Migrate RT partitions (RULE-based, bigint dates, 10 columns)
- [ ] Verify schema correctness in target database
- [ ] Verify TRIGGER/RULE creation in target database

---

## Recommended Implementation Order

1. ✅ **Database Analysis** - COMPLETED
2. **Core Logic Updates**:
   - Modify `partition_discovery.py` to accept table type list
   - Create table type enum/constants
   - Update schema detection logic
3. **Table Creator Refactoring**:
   - Extract TRIGGER generation logic
   - Implement RULE generation logic
   - Add date type detection and handling
4. **UI Updates**:
   - Add table type selector (checkboxes/multi-select)
   - Default to PH for backward compatibility
   - Update dialog validation
5. **Testing**:
   - Unit tests for each component
   - Integration tests for each table type
   - Manual verification

---

## Notes

- PostgreSQL 9.3 supports both TRIGGERS and RULES for partitioning
- RULES are considered legacy but still widely used
- Target database should match source partitioning strategy
- Rule naming convention: `rule_{table_name}_{YYMM}`
- Partition naming convention: `{table_name}_{YYMM}`
