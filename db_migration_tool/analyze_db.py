#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Database Table Structure Analysis Script
Analyzes partition tables in bms93 database to understand schema differences
"""

import sys
import psycopg2
from psycopg2.extras import RealDictCursor
import json

# Fix Windows console encoding
if sys.platform == 'win32':
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

# Database connection info
DB_CONFIG = {
    'host': 'localhost',
    'port': 5432,
    'user': 'postgres',
    'password': 'postgres',
    'database': 'bms93'
}

def analyze_partition_tables():
    """Analyze partition table structures in the database"""

    try:
        conn = psycopg2.connect(**DB_CONFIG)
        cur = conn.cursor(cursor_factory=RealDictCursor)

        print("=" * 80)
        print("BMS93 Database Analysis - Partition Tables")
        print("=" * 80)
        print()

        # 1. Check for partition tables
        print("1. Searching for partition tables...")
        print("-" * 80)

        query_partitions = """
        SELECT
            schemaname,
            tablename,
            CASE
                WHEN tablename LIKE '%point_history%' THEN 'PH'
                WHEN tablename LIKE '%trend_history%' THEN 'TH'
                WHEN tablename LIKE '%energy_display%' THEN 'ED'
                WHEN tablename LIKE '%running_time_history%' THEN 'RT'
                ELSE 'UNKNOWN'
            END as table_type
        FROM pg_tables
        WHERE schemaname = 'public'
        AND (
            tablename LIKE '%point_history%'
            OR tablename LIKE '%trend_history%'
            OR tablename LIKE '%energy_display%'
            OR tablename LIKE '%running_time_history%'
        )
        ORDER BY table_type, tablename
        LIMIT 20;
        """

        cur.execute(query_partitions)
        partitions = cur.fetchall()

        if partitions:
            print(f"Found {len(partitions)} partition tables:")
            table_types = {}
            for p in partitions:
                table_type = p['table_type']
                if table_type not in table_types:
                    table_types[table_type] = []
                table_types[table_type].append(p['tablename'])

            for table_type, tables in sorted(table_types.items()):
                print(f"\n  {table_type} ({len(tables)} tables):")
                for table in tables[:5]:  # Show first 5
                    print(f"    - {table}")
                if len(tables) > 5:
                    print(f"    ... and {len(tables) - 5} more")
        else:
            print("No partition tables found!")

        print()

        # 2. Check parent tables
        print("\n2. Checking parent tables...")
        print("-" * 80)

        parent_tables = ['point_history', 'trend_history', 'energy_display', 'running_time_history']

        for parent_table in parent_tables:
            query_table_exists = """
            SELECT EXISTS (
                SELECT 1 FROM pg_tables
                WHERE schemaname = 'public' AND tablename = %s
            );
            """
            cur.execute(query_table_exists, (parent_table,))
            exists = cur.fetchone()['exists']

            if exists:
                print(f"\n  [OK] {parent_table} (parent table exists)")

                # Get column information
                query_columns = """
                SELECT
                    column_name,
                    data_type,
                    character_maximum_length,
                    is_nullable
                FROM information_schema.columns
                WHERE table_schema = 'public'
                AND table_name = %s
                ORDER BY ordinal_position;
                """
                cur.execute(query_columns, (parent_table,))
                columns = cur.fetchall()

                print(f"    Columns ({len(columns)}):")
                for col in columns:
                    nullable = "NULL" if col['is_nullable'] == 'YES' else "NOT NULL"
                    col_type = col['data_type']
                    if col['character_maximum_length']:
                        col_type += f"({col['character_maximum_length']})"
                    print(f"      - {col['column_name']}: {col_type} {nullable}")

                # Check constraints
                query_constraints = """
                SELECT
                    conname as constraint_name,
                    contype as constraint_type,
                    pg_get_constraintdef(oid) as definition
                FROM pg_constraint
                WHERE conrelid = %s::regclass
                ORDER BY contype;
                """
                cur.execute(query_constraints, (parent_table,))
                constraints = cur.fetchall()

                if constraints:
                    print(f"    Constraints ({len(constraints)}):")
                    for const in constraints:
                        const_type = {
                            'p': 'PRIMARY KEY',
                            'f': 'FOREIGN KEY',
                            'c': 'CHECK',
                            'u': 'UNIQUE'
                        }.get(const['constraint_type'], const['constraint_type'])
                        print(f"      - {const['constraint_name']} ({const_type})")
                        print(f"        {const['definition']}")
            else:
                print(f"\n  [NOT FOUND] {parent_table}")

        print()

        # 3. Check TRIGGERS
        print("\n3. Checking TRIGGERS...")
        print("-" * 80)

        query_triggers = """
        SELECT
            t.tgname as trigger_name,
            c.relname as table_name,
            p.proname as function_name,
            pg_get_triggerdef(t.oid) as trigger_definition
        FROM pg_trigger t
        JOIN pg_class c ON t.tgrelid = c.oid
        JOIN pg_proc p ON t.tgfoid = p.oid
        WHERE c.relname IN ('point_history', 'trend_history', 'energy_display', 'running_time_history')
        AND NOT t.tgisinternal
        ORDER BY c.relname, t.tgname;
        """

        cur.execute(query_triggers)
        triggers = cur.fetchall()

        if triggers:
            print(f"Found {len(triggers)} triggers:")
            for trig in triggers:
                print(f"\n  Table: {trig['table_name']}")
                print(f"  Trigger: {trig['trigger_name']}")
                print(f"  Function: {trig['function_name']}")
                print(f"  Definition:")
                print(f"    {trig['trigger_definition']}")
        else:
            print("No triggers found on parent tables")

        print()

        # 4. Check RULES
        print("\n4. Checking RULES...")
        print("-" * 80)

        query_rules = """
        SELECT
            n.nspname as schemaname,
            c.relname as tablename,
            r.rulename,
            pg_get_ruledef(r.oid) as rule_definition
        FROM pg_rewrite r
        JOIN pg_class c ON r.ev_class = c.oid
        JOIN pg_namespace n ON c.relnamespace = n.oid
        WHERE n.nspname = 'public'
        AND c.relname IN ('point_history', 'trend_history', 'energy_display', 'running_time_history')
        AND r.rulename != '_RETURN'
        ORDER BY c.relname, r.rulename;
        """

        cur.execute(query_rules)
        rules = cur.fetchall()

        if rules:
            print(f"Found {len(rules)} rules:")
            for rule in rules:
                print(f"\n  Table: {rule['tablename']}")
                print(f"  Rule: {rule['rulename']}")
                print(f"  Definition:")
                # Format the rule definition for better readability
                definition = rule['rule_definition'].replace(' DO ', '\n    DO ')
                print(f"    {definition}")
        else:
            print("No rules found on parent tables")

        print()

        # 5. Check sample partition table structure
        print("\n5. Analyzing sample partition table structures...")
        print("-" * 80)

        for table_type in ['point_history', 'trend_history', 'energy_display', 'running_time_history']:
            query_sample_partition = f"""
            SELECT tablename
            FROM pg_tables
            WHERE schemaname = 'public'
            AND tablename LIKE '{table_type}_%'
            ORDER BY tablename
            LIMIT 1;
            """
            cur.execute(query_sample_partition)
            sample = cur.fetchone()

            if sample:
                partition_name = sample['tablename']
                print(f"\n  Sample partition: {partition_name}")

                # Check inheritance
                query_inheritance = """
                SELECT
                    c1.relname as child,
                    c2.relname as parent
                FROM pg_inherits
                JOIN pg_class c1 ON inhrelid = c1.oid
                JOIN pg_class c2 ON inhparent = c2.oid
                WHERE c1.relname = %s;
                """
                cur.execute(query_inheritance, (partition_name,))
                inheritance = cur.fetchone()

                if inheritance:
                    print(f"    Inherits from: {inheritance['parent']}")

                # Check constraints (especially CHECK constraints for partition range)
                query_part_constraints = """
                SELECT
                    conname,
                    pg_get_constraintdef(oid) as definition
                FROM pg_constraint
                WHERE conrelid = %s::regclass
                AND contype = 'c'
                ORDER BY conname;
                """
                cur.execute(query_part_constraints, (partition_name,))
                part_constraints = cur.fetchall()

                if part_constraints:
                    print(f"    CHECK Constraints:")
                    for const in part_constraints:
                        print(f"      - {const['conname']}: {const['definition']}")

        print()
        print("=" * 80)
        print("Analysis Complete")
        print("=" * 80)

        cur.close()
        conn.close()

    except psycopg2.Error as e:
        print(f"\n[ERROR] Database Error: {e}")
        print(f"  SQLSTATE: {e.pgcode}")
        print(f"  Message: {e.pgerror}")
    except Exception as e:
        print(f"\n[ERROR] Error: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    analyze_partition_tables()
