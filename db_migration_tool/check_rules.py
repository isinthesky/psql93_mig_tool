#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Quick script to check RULES for trend_history and running_time_history
"""

import sys
import psycopg2
from psycopg2.extras import RealDictCursor

# Fix Windows console encoding
if sys.platform == 'win32':
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

DB_CONFIG = {
    'host': 'localhost',
    'port': 5432,
    'user': 'postgres',
    'password': 'postgres',
    'database': 'bms93'
}

def check_rules():
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        cur = conn.cursor(cursor_factory=RealDictCursor)

        # Count rules per table
        query_count = """
        SELECT
            c.relname as tablename,
            COUNT(r.oid) as rule_count
        FROM pg_rewrite r
        JOIN pg_class c ON r.ev_class = c.oid
        JOIN pg_namespace n ON c.relnamespace = n.oid
        WHERE n.nspname = 'public'
        AND c.relname IN ('point_history', 'trend_history', 'energy_display', 'running_time_history')
        AND r.rulename != '_RETURN'
        GROUP BY c.relname
        ORDER BY c.relname;
        """

        cur.execute(query_count)
        counts = cur.fetchall()

        print("=" * 80)
        print("RULE Count Summary")
        print("=" * 80)
        for row in counts:
            print(f"  {row['tablename']}: {row['rule_count']} rules")
        print()

        # Show sample rules for TH and RT
        for table_name in ['trend_history', 'running_time_history']:
            query_sample = """
            SELECT
                r.rulename,
                pg_get_ruledef(r.oid) as rule_definition
            FROM pg_rewrite r
            JOIN pg_class c ON r.ev_class = c.oid
            JOIN pg_namespace n ON c.relnamespace = n.oid
            WHERE n.nspname = 'public'
            AND c.relname = %s
            AND r.rulename != '_RETURN'
            ORDER BY r.rulename
            LIMIT 3;
            """

            cur.execute(query_sample, (table_name,))
            rules = cur.fetchall()

            print(f"\n{table_name.upper()} - Sample Rules (first 3):")
            print("-" * 80)
            for rule in rules:
                print(f"\n  Rule: {rule['rulename']}")
                print(f"  Definition: {rule['rule_definition'][:200]}...")

        print("\n" + "=" * 80)

        cur.close()
        conn.close()

    except Exception as e:
        print(f"[ERROR] {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    check_rules()
