"""Conservative, deterministic Laravel and Doctrine migration candidates."""
from __future__ import annotations

import re
from analysis.schema_migrations import UnsupportedMigration

_IDENTIFIER = re.compile(r'^[A-Za-z_][A-Za-z0-9_]{0,62}$', re.ASCII)
_TYPES = {'integer': ('integer', 'INTEGER'), 'int': ('integer', 'INTEGER'),
          'bigint': ('bigInteger', 'BIGINT'), 'text': ('text', 'TEXT'),
          'boolean': ('boolean', 'BOOLEAN'), 'bool': ('boolean', 'BOOLEAN'),
          'date': ('date', 'DATE')}


def _name(value):
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise UnsupportedMigration('Identifiers must contain only ASCII letters, digits, and underscores and start with a letter or underscore')
    return value


def _column(column, *, adding=False):
    _name(column['name'])
    kind = column['type'].lower()
    if kind not in _TYPES or column['auto_increment'] or column['default'] is not None:
        raise UnsupportedMigration('Unsupported column type, default, or auto-increment; edit the migration manually')
    if adding and not column['nullable']:
        raise UnsupportedMigration('Adding a non-null column without a known default is unsafe')
    return _TYPES[kind]


def _mysql_definition(column):
    details = column.get('mysql_definition')
    if not details or not details['other_attributes_absent']:
        raise UnsupportedMigration('Confirm the complete MySQL column definition and absence of other attributes in Draw.io')
    kind = column['type'].lower()
    if kind not in _TYPES or (details['unsigned'] and kind not in ('int', 'integer', 'bigint')):
        raise UnsupportedMigration('Unsupported MySQL column type or unsigned modifier')
    if details['charset'] or details['collation']:
        raise UnsupportedMigration('MySQL character sets and collations require manual migration')
    if details['comment'] is not None and not re.fullmatch(r'[A-Za-z0-9 _.,-]{0,256}', details['comment']):
        raise UnsupportedMigration('MySQL comment contains unsupported characters')
    default_kind = details['default_kind']
    default = column['default']
    if default_kind == 'literal':
        if isinstance(default, bool):
            literal = '1' if default else '0'
        elif isinstance(default, (int, float)):
            literal = str(default)
        elif isinstance(default, str) and re.fullmatch(r'[A-Za-z0-9 _.,-]{0,256}', default):
            literal = "'" + default + "'"
        else:
            raise UnsupportedMigration('MySQL default must be a simple literal')
    elif default is not None:
        raise UnsupportedMigration('MySQL default value conflicts with its confirmed default kind')
    else:
        literal = 'NULL'
    if default_kind == 'null' and not column['nullable']:
        raise UnsupportedMigration('A NOT NULL column cannot use DEFAULT NULL')
    if column['auto_increment'] and (kind not in ('int', 'integer', 'bigint') or column['nullable'] or default_kind != 'none'):
        raise UnsupportedMigration('MySQL auto-increment requires a non-null integer column without an explicit default')
    sql = _TYPES[kind][1] + (' UNSIGNED' if details['unsigned'] else '')
    sql += ' NULL' if column['nullable'] else ' NOT NULL'
    if default_kind != 'none':
        sql += ' DEFAULT ' + literal
    if column['auto_increment']:
        sql += ' AUTO_INCREMENT'
    if details['comment'] is not None:
        sql += " COMMENT '" + details['comment'] + "'"
    return sql


def _check(operations, database):
    if database not in ('mysql', 'postgresql'):
        raise UnsupportedMigration('Select mysql or postgresql as the target database')
    if not operations:
        raise UnsupportedMigration('The saved draft has no schema changes')
    extended = any(op['kind'] in ('key', 'index', 'relationship') or op['action'] == 'change' for op in operations)
    if extended and len(operations) != 1:
        raise UnsupportedMigration('Combined constraint, index, relationship, or column changes require manual migration')
    for op in operations:
        if op['kind'] not in ('table', 'column', 'key', 'index', 'relationship') or op['action'] not in ('add', 'drop', 'change'):
            raise UnsupportedMigration(f"Unsupported {op['kind']} {op['action']}; no migration was generated")
        value = op['after'] or op['before']
        if op['kind'] in ('key', 'index'):
            if op['action'] == 'change':
                raise UnsupportedMigration('Changing a constraint or index requires manual migration')
            _name(op['table_name'])
            _name(value['name'])
            for name in op['column_names']:
                _name(name)
            if op['kind'] == 'key' and value['kind'] == 'primary' and op['action'] == 'add' and op['columns_nullable']:
                raise UnsupportedMigration('Primary keys on nullable columns require manual migration')
        elif op['kind'] == 'relationship':
            if op['action'] == 'change':
                raise UnsupportedMigration('Changing a relationship requires manual migration')
            _name(value['name'])
            for name in [op['source_table_name'], op['target_table_name'], *op['source_column_names'], *op['target_column_names']]:
                _name(name)
            if op['relationship_namespaced'] or not op['compatible_types'] or not op['target_is_key']:
                raise UnsupportedMigration('Relationship requires unnamespaced tables, compatible types, and a target key')
            if 'set default' in (value['on_delete'], value['on_update']):
                raise UnsupportedMigration('SET DEFAULT foreign keys require manual migration')
            if 'set null' in (value['on_delete'], value['on_update']) and not op['nullable_source']:
                raise UnsupportedMigration('SET NULL requires nullable source columns')
        elif op['kind'] == 'table':
            if op['action'] == 'change':
                raise UnsupportedMigration('Table rename requires manual migration')
            _name(value['name'])
            if value['namespace'] is not None or value['keys'] or value['indexes']:
                raise UnsupportedMigration('Namespaced tables, keys, and indexes require manual migration')
            if op['action'] == 'add':
                if not value['columns']:
                    raise UnsupportedMigration('Cannot generate an empty table')
                for column in value['columns']:
                    _column(column)
        else:
            _name(op['table_name'])
            if op['action'] == 'change':
                before, after = op['before'], op['after']
                _name(before['name'])
                _name(after['name'])
                if database == 'mysql' and (before['type'] != after['type'] or before['nullable'] != after['nullable'] or before['default'] != after['default'] or before['auto_increment'] != after['auto_increment']):
                    _mysql_definition(after)
                    if after['auto_increment'] and not op.get('mysql_auto_increment_indexed'):
                        raise UnsupportedMigration('MySQL auto-increment requires a key or index beginning with this column')
                elif before['auto_increment'] != after['auto_increment']:
                    raise UnsupportedMigration('Changing auto-increment requires verified live metadata and manual migration')
                elif before['default'] != after['default'] and after['default'] is not None and not isinstance(after['default'], (int, float, bool)):
                    raise UnsupportedMigration('PostgreSQL defaults must be numeric or boolean literals, or removed; expressions and strings require manual migration')
                if before['type'] != after['type'] or before['nullable'] != after['nullable']:
                    if before['type'].lower() not in _TYPES or after['type'].lower() not in _TYPES:
                        raise UnsupportedMigration('Column type is unsupported for automatic migration')
                    if database == 'postgresql' and before['type'] != after['type'] and 'text' in (before['type'].lower(), after['type'].lower()):
                        if after['type'].lower() != 'text' or not after.get('collation'):
                            raise UnsupportedMigration('Text type changes require an explicitly confirmed target collation; migrate manually')
                        for part in after['collation'].split('.'):
                            _name(part)
            elif op['action'] == 'add':
                _column(value, adding=True)
            else:
                _name(value['name'])
    # Sequential name reuse and mixed drop/add can depend on ordering and hidden constraints.
    if any(op['ambiguous'] for op in operations):
        raise UnsupportedMigration('Possible rename requires manual resolution before generation')


def _laravel_column(column):
    method, _ = _column(column)
    nullable = '->nullable()' if column['nullable'] else ''
    return f"$table->{method}('{column['name']}'){nullable};"


def _php_sql(sql):
    return sql.replace('\\', '\\\\').replace("'", "\\'")


def _changed_column_sql(op, database):
    before, after = op['before'], op['after']
    quote = '`' if database == 'mysql' else '"'
    table = f"{quote}{op['table_name']}{quote}"
    old_name = f"{quote}{before['name']}{quote}"
    name = f"{quote}{after['name']}{quote}"
    statements = []
    if before['name'] != after['name']:
        statements.append(f'ALTER TABLE {table} RENAME COLUMN {old_name} TO {name}')
    if database == 'mysql':
        if any(before[field] != after[field] for field in ('type', 'nullable', 'default', 'auto_increment')):
            statements.append(f'ALTER TABLE {table} MODIFY COLUMN {name} {_mysql_definition(after)}')
        return statements
    if before['type'] != after['type']:
        collation = ''
        if after['type'].lower() == 'text':
            collation = ' COLLATE ' + '.'.join(f'"{part}"' for part in after['collation'].split('.'))
        statements.append(f"ALTER TABLE {table} ALTER COLUMN {name} TYPE {_TYPES[after['type'].lower()][1]}{collation}")
    if before['nullable'] != after['nullable']:
        action = 'DROP NOT NULL' if after['nullable'] else 'SET NOT NULL'
        statements.append(f'ALTER TABLE {table} ALTER COLUMN {name} {action}')
    if before['default'] != after['default']:
        default = after['default']
        if default is None:
            action = 'DROP DEFAULT'
        else:
            literal = ('TRUE' if default else 'FALSE') if isinstance(default, bool) else str(default)
            action = f'SET DEFAULT {literal}'
        statements.append(f'ALTER TABLE {table} ALTER COLUMN {name} {action}')
    return statements


class LaravelMigrationAdapter:
    def generate(self, operations, database):
        _check(operations, database)
        statements = []
        for op in operations:
            value = op['after'] or op['before']
            if op['kind'] == 'column' and op['action'] == 'change':
                statements.extend(f"DB::statement('{_php_sql(sql)}');" for sql in _changed_column_sql(op, database))
                continue
            if op['kind'] in ('key', 'index'):
                if op['kind'] == 'index' and value['unique']:
                    raise UnsupportedMigration('Unique index migrations require manual handling for Laravel')
                if op['action'] == 'add':
                    method = ('primary' if op['kind'] == 'key' and value['kind'] == 'primary'
                              else 'unique' if op['kind'] == 'key' or value['unique'] else 'index')
                    columns = ', '.join(f"'{name}'" for name in op['column_names'])
                    statement = f"$table->{method}([{columns}], '{value['name']}');"
                else:
                    method = ('dropPrimary' if op['kind'] == 'key' and value['kind'] == 'primary'
                              else 'dropUnique' if op['kind'] == 'key' or value['unique'] else 'dropIndex')
                    statement = f"$table->{method}('{value['name']}');"
                statements.append(f"Schema::table('{op['table_name']}', function (Blueprint $table) {{ {statement} }});")
                continue
            if op['kind'] == 'relationship':
                if op['action'] == 'drop':
                    statement = f"$table->dropForeign('{value['name']}');"
                else:
                    source = ', '.join(f"'{name}'" for name in op['source_column_names'])
                    target = ', '.join(f"'{name}'" for name in op['target_column_names'])
                    statement = (f"$table->foreign([{source}], '{value['name']}')->references([{target}])"
                                 f"->on('{op['target_table_name']}')")
                    for field, method in (('on_delete', 'onDelete'), ('on_update', 'onUpdate')):
                        if value[field]:
                            statement += f"->{method}('{value[field]}')"
                    statement += ';'
                statements.append(f"Schema::table('{op['source_table_name']}', function (Blueprint $table) {{ {statement} }});")
                continue
            if op['kind'] == 'table' and op['action'] == 'add':
                columns = ' '.join(_laravel_column(c) for c in value['columns'])
                statements.append(f"Schema::create('{value['name']}', function (Blueprint $table) {{ {columns} }});")
            elif op['kind'] == 'table':
                statements.append(f"Schema::drop('{value['name']}');")
            else:
                column = _laravel_column(value) if op['action'] == 'add' else f"$table->dropColumn('{value['name']}');"
                statements.append(f"Schema::table('{op['table_name']}', function (Blueprint $table) {{ {column} }});")
        body = '\n        '.join(statements)
        db_import = 'use Illuminate\\Support\\Facades\\DB;\n' if any(op['kind'] == 'column' and op['action'] == 'change' for op in operations) else ''
        content = ("<?php\n\nuse Illuminate\\Database\\Migrations\\Migration;\nuse Illuminate\\Database\\Schema\\Blueprint;\nuse Illuminate\\Support\\Facades\\Schema;\n" + db_import + "\nreturn new class extends Migration {\n    public function up(): void\n    {\n        " + body + "\n    }\n\n    public function down(): void\n    {\n        throw new \\RuntimeException('Manual rollback required');\n    }\n};\n")
        return [{'name': 'migration.php', 'content': content}]


def _sql_column(column, quote):
    _, sql_type = _column(column)
    null = '' if column['nullable'] else ' NOT NULL'
    return f"{quote}{column['name']}{quote} {sql_type}{null}"


class DoctrineMigrationAdapter:
    def generate(self, operations, database):
        _check(operations, database)
        quote = '`' if database == 'mysql' else '"'
        statements = []
        for op in operations:
            value = op['after'] or op['before']
            if op['kind'] == 'column' and op['action'] == 'change':
                statements.extend(f"$this->addSql('{_php_sql(sql)}');" for sql in _changed_column_sql(op, database))
                continue
            if op['kind'] in ('key', 'index'):
                table, name = f"{quote}{op['table_name']}{quote}", f"{quote}{value['name']}{quote}"
                columns = ', '.join(f"{quote}{name}{quote}" for name in op['column_names'])
                if op['action'] == 'add':
                    if op['kind'] == 'key':
                        constraint = 'PRIMARY KEY' if value['kind'] == 'primary' else 'UNIQUE'
                        sql = f'ALTER TABLE {table} ADD CONSTRAINT {name} {constraint} ({columns})'
                    else:
                        unique = 'UNIQUE ' if value['unique'] else ''
                        sql = f'CREATE {unique}INDEX {name} ON {table} ({columns})'
                elif op['kind'] == 'key' and value['kind'] == 'primary' and database == 'mysql':
                    sql = f'ALTER TABLE {table} DROP PRIMARY KEY'
                elif op['kind'] == 'key' and database == 'postgresql':
                    sql = f'ALTER TABLE {table} DROP CONSTRAINT {name}'
                elif database == 'mysql':
                    sql = f'DROP INDEX {name} ON {table}'
                else:
                    sql = f'DROP INDEX {name}'
                statements.append(f"$this->addSql('{sql}');")
                continue
            if op['kind'] == 'relationship':
                source, target = f"{quote}{op['source_table_name']}{quote}", f"{quote}{op['target_table_name']}{quote}"
                name = f"{quote}{value['name']}{quote}"
                if op['action'] == 'drop':
                    kind = 'FOREIGN KEY' if database == 'mysql' else 'CONSTRAINT'
                    sql = f'ALTER TABLE {source} DROP {kind} {name}'
                else:
                    left = ', '.join(f"{quote}{name}{quote}" for name in op['source_column_names'])
                    right = ', '.join(f"{quote}{name}{quote}" for name in op['target_column_names'])
                    sql = f'ALTER TABLE {source} ADD CONSTRAINT {name} FOREIGN KEY ({left}) REFERENCES {target} ({right})'
                    for field, clause in (('on_delete', 'ON DELETE'), ('on_update', 'ON UPDATE')):
                        if value[field]:
                            sql += f" {clause} {value[field].upper()}"
                statements.append(f"$this->addSql('{sql}');")
                continue
            if op['kind'] == 'table':
                table = f"{quote}{value['name']}{quote}"
                if op['action'] == 'add':
                    columns = ', '.join(_sql_column(c, quote) for c in value['columns'])
                    sql = f'CREATE TABLE {table} ({columns})'
                else:
                    sql = f'DROP TABLE {table}'
            else:
                table = f"{quote}{op['table_name']}{quote}"
                col = _sql_column(value, quote) if op['action'] == 'add' else f"{quote}{value['name']}{quote}"
                sql = f"ALTER TABLE {table} {'ADD' if op['action'] == 'add' else 'DROP'} COLUMN {col}"
            statements.append(f"$this->addSql('{sql}');")
        body = '\n        '.join(statements)
        platform = 'MySQLPlatform' if database == 'mysql' else 'PostgreSQLPlatform'
        content = ("<?php\n\nnamespace DoctrineMigrations;\n\nuse Doctrine\\DBAL\\Platforms\\" + platform + ";\nuse Doctrine\\DBAL\\Schema\\Schema;\nuse Doctrine\\Migrations\\AbstractMigration;\n\nfinal class Version20000101000000 extends AbstractMigration\n{\n    public function up(Schema $schema): void\n    {\n        $this->abortIf(!($this->connection->getDatabasePlatform() instanceof " + platform + "), 'Wrong database platform');\n        " + body + "\n    }\n\n    public function down(Schema $schema): void\n    {\n        $this->throwIrreversibleMigrationException();\n    }\n}\n")
        return [{'name': 'Version20000101000000.php', 'content': content}]
