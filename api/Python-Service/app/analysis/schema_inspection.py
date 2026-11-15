"""Opt-in, read-only observation of configured database column metadata.

Inspection is deliberately partial: it never rewrites a draft or certifies a
baseline as complete. Connection settings stay in the service environment.
"""
from __future__ import annotations

import json
import os
import re


class InspectionError(ValueError):
    pass


def inspect_columns(target_name: str) -> dict:
    if not isinstance(target_name, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', target_name):
        raise InspectionError('Enter a configured inspection target name.')
    try:
        targets = json.loads(os.environ.get('SCHEMA_INSPECTION_TARGETS', '{}'))
        config = targets[target_name]
        if not isinstance(config, dict) or config.get('engine') not in ('postgresql', 'mysql'):
            raise ValueError()
        if any(not isinstance(config.get(k), str) or not config[k] for k in ('host', 'database', 'username', 'password', 'ca_file')):
            raise ValueError()
        port = config.get('port', 5432 if config['engine'] == 'postgresql' else 3306)
        if type(port) is not int or not 1 <= port <= 65535:
            raise ValueError()
        namespace = config.get('schema', 'public')
        if not isinstance(namespace, str) or not namespace or len(namespace) > 128:
            raise ValueError()
    except (ValueError, KeyError, TypeError):
        raise InspectionError('Inspection target is not configured correctly.') from None

    connection = None
    try:
        if config['engine'] == 'postgresql':
            import psycopg
            connection = psycopg.connect(host=config['host'], port=port, dbname=config['database'],
                                         user=config['username'], password=config['password'],
                                         sslmode='verify-full', sslrootcert=config['ca_file'], connect_timeout=5)
            with connection.cursor() as cursor:
                cursor.execute('SET TRANSACTION READ ONLY')
                cursor.execute("SET LOCAL statement_timeout = '5s'")
                cursor.execute('SELECT table_name, column_name, data_type, is_nullable, LEFT(column_default, 512) '
                               'FROM information_schema.columns WHERE table_schema = %s '
                               'ORDER BY table_name, ordinal_position LIMIT 5001', (namespace,))
                rows = cursor.fetchall()
        else:
            import pymysql
            connection = pymysql.connect(host=config['host'], port=port, database=config['database'],
                                         user=config['username'], password=config['password'],
                                         ssl={'ca': config['ca_file']}, ssl_verify_cert=True, ssl_verify_identity=True,
                                         connect_timeout=5, read_timeout=10, write_timeout=10, autocommit=False)
            with connection.cursor() as cursor:
                cursor.execute('SET TRANSACTION READ ONLY')
                cursor.execute('START TRANSACTION')
                cursor.execute('SELECT table_name, column_name, column_type, is_nullable, LEFT(column_default, 512) '
                               'FROM information_schema.columns WHERE table_schema = DATABASE() '
                               'ORDER BY table_name, ordinal_position LIMIT 5001')
                rows = cursor.fetchall()
        return {'engine': config['engine'], 'namespace': namespace if config['engine'] == 'postgresql' else config['database'],
                'truncated': len(rows) > 5000,
                'columns': [{'table': row[0], 'name': row[1], 'type': row[2],
                             'nullable': row[3] == 'YES', 'default_excerpt': row[4]} for row in rows[:5000]],
                'coverage': 'columns-only; keys, indexes, relationships and other database objects are not inspected'}
    except Exception:
        # Driver messages can contain host names, user names, or credentials.
        raise InspectionError('Inspection failed. Check the configured target, TLS, read-only credentials, and connectivity.') from None
    finally:
        if connection is not None:
            try:
                connection.rollback()
                connection.close()
            except Exception:
                pass


def compare_column_names(baseline: dict, report: dict) -> dict:
    """Compare names only; this cannot establish complete metadata coverage."""
    expected = set()
    skipped = []
    for table in baseline.get('tables', []):
        namespace = table.get('namespace')
        if namespace and namespace != report['namespace']:
            skipped.append(table['name'])
            continue
        expected.update((table['name'], column['name']) for column in table.get('columns', []))
    observed = {(column['table'], column['name']) for column in report['columns']}
    def entries(values):
        return [{'table': table, 'column': column} for table, column in sorted(values)]
    return {'missing_from_database': None if report['truncated'] else entries(expected - observed),
            'not_in_baseline': entries(observed - expected), 'skipped_other_namespace_tables': skipped}
