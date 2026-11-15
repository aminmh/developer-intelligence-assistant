/* Schema commands for the pinned self-hosted Draw.io image. */
Draw.loadPlugin(function (ui) {
  var pluginSettings = ui.actions.get('plugins');
  if (pluginSettings) pluginSettings.setEnabled(false);
  var graph = ui.editor.graph;
  var model = graph.getModel();
  function uid() { return 'new-' + crypto.randomUUID(); }
  function promptValue(label, value) {
    var result = window.prompt(label, value || '');
    if (result === null) return null;
    result = result.trim();
    if (!result || result.length > 256 || /[\r\n<>]/.test(result)) { window.alert('Enter plain text between 1 and 256 characters.'); return null; }
    return result;
  }
  function tableOf(cell) {
    if (!cell) return null;
    if (/^table-/.test(cell.id || '')) return cell;
    var parent = model.getParent(cell);
    return parent && /^table-/.test(parent.id || '') ? parent : null;
  }
  function metadata() {
    var cell = model.getCell('schema-metadata');
    if (!cell) return null;
    try {
      var value = JSON.parse(model.getValue(cell));
      return Array.isArray(value) ? { cell: cell, value: value } : null;
    } catch (_) { return null; }
  }
  mxResources.parse('schemaAddTable=Add schema table\nschemaAddColumn=Add schema column\nschemaColumnProperties=Edit column properties\nschemaTableConstraints=Edit table keys and indexes\nschemaRelationship=Add or edit relationship\nschemaFind=Find schema table or column\nschemaRuntime=Show table runtime usage\nschemaInspect=Inspect database metadata\nschemaShowInApp=Show original schema details\nschemaExport=Export schema JSON\nschemaExportDbml=Export schema DBML\nschemaMigration=Preview migration');
  ui.actions.addAction('schemaAddTable', function () {
    var name = promptValue('Table name'); if (!name) return;
    var data = metadata();
    if (!data || !data.value.some(function (entry) { return entry && entry.kind === 'constraints-v1'; })) { window.alert('Schema metadata is unavailable. Reopen the editor.'); return; }
    var parent = graph.getDefaultParent();
    var x = 80;
    for (var i = 0; i < model.getChildCount(parent); i++) {
      var sibling = model.getChildAt(parent, i);
      var bounds = model.getGeometry(sibling);
      if (/^table-/.test(sibling.id || '') && bounds) x = Math.max(x, bounds.x + bounds.width + 40);
    }
    model.beginUpdate();
    try {
      var id = uid();
      var cell = graph.insertVertex(parent, 'table-' + id, name, x, 80, 240, 32, 'swimlane;startSize=32;horizontal=1;');
      data.value.push({ table_id: id, keys: [], indexes: [] });
      model.setValue(data.cell, JSON.stringify(data.value));
      graph.setSelectionCell(cell);
    } finally { model.endUpdate(); }
  });
  ui.actions.addAction('schemaAddColumn', function () {
    var table = tableOf(graph.getSelectionCell());
    if (!table) { window.alert('Select a schema table or one of its columns first.'); return; }
    var name = promptValue('Column name'); if (!name) return;
    var type = promptValue('Column type', 'varchar(255)'); if (!type) return;
    if (name.indexOf(': ') >= 0 || type.indexOf(': ') >= 0) { window.alert('Names and types cannot contain colon followed by a space.'); return; }
    model.beginUpdate();
    try {
      var y = 32;
      for (var i = 0; i < model.getChildCount(table); i++) {
        var child = model.getChildAt(table, i);
        var bounds = model.getGeometry(child);
        if (/^column-/.test(child.id || '') && bounds) y = Math.max(y, bounds.y + bounds.height);
      }
      var geometry = model.getGeometry(table).clone();
      var cell = graph.insertVertex(table, 'column-' + uid(), name + ': ' + type, 0, y, geometry.width, 26, 'text;align=left;verticalAlign=middle;spacingLeft=8;');
      var data = metadata();
      if (data) {
        data.value.push({ id: cell.id.slice('column-'.length), nullable: false, default: null, auto_increment: false });
        model.setValue(data.cell, JSON.stringify(data.value));
      }
      geometry.height = Math.max(geometry.height, y + 26);
      model.setGeometry(table, geometry);
      graph.setSelectionCell(cell);
    } finally { model.endUpdate(); }
  });
  ui.actions.addAction('schemaColumnProperties', function () {
    var cell = graph.getSelectionCell();
    if (!cell || !/^column-/.test(cell.id || '')) { window.alert('Select a schema column first.'); return; }
    var data = metadata();
    var id;
    try { id = decodeURIComponent(cell.id.slice('column-'.length)); } catch (_) { window.alert('Invalid column ID.'); return; }
    var matches = data ? data.value.filter(function (item) { return item && item.id === id; }) : [];
    if (matches.length !== 1) { window.alert('Column properties are unavailable or ambiguous in this layout. Save the schema and rebuild the diagram first.'); return; }
    var current = matches[0];
    var input = window.prompt('Edit column properties as JSON. default is a literal; default_expression and identity retain catalog expressions and identity behavior. auto_increment may be true, false, or null when unknown. For MySQL changes, replace mysql_definition null with an object containing unsigned, charset, collation, comment, default_kind (none, null, or literal), and other_attributes_absent. Set other_attributes_absent true only after verifying the complete target definition.', JSON.stringify(Object.assign({ collation: null, mysql_definition: null }, current), null, 2));
    if (input === null) return;
    var value;
    try { value = JSON.parse(input); } catch (_) { window.alert('Invalid JSON. No column properties were changed.'); return; }
    var definition = value && value.mysql_definition;
    var validDefinition = definition == null || (typeof definition === 'object' && !Array.isArray(definition) && Object.keys(definition).sort().join(',') === 'charset,collation,comment,default_kind,other_attributes_absent,unsigned' && typeof definition.unsigned === 'boolean' && typeof definition.other_attributes_absent === 'boolean' && ['none', 'null', 'literal'].indexOf(definition.default_kind) >= 0 && [definition.charset, definition.collation, definition.comment].every(function (item) { return item === null || (typeof item === 'string' && item.length <= 256); }));
    if (!value || typeof value !== 'object' || Array.isArray(value) || Object.keys(value).filter(function (key) { return ['collation', 'mysql_definition', 'default_expression', 'identity'].indexOf(key) < 0; }).sort().join(',') !== 'auto_increment,default,id,nullable' || value.id !== id || typeof value.nullable !== 'boolean' || !(value.auto_increment === null || typeof value.auto_increment === 'boolean') || !validDefinition || ['default_expression', 'identity'].some(function (key) { return key in value && !(value[key] === null || (typeof value[key] === 'string' && value[key].length <= 65536)); }) || ('collation' in value && !(value.collation === null || (typeof value.collation === 'string' && value.collation.trim() && value.collation.length <= 256))) || !(value.default === null || typeof value.default === 'string' || typeof value.default === 'boolean' || (typeof value.default === 'number' && Number.isFinite(value.default)))) { window.alert('Invalid properties. No column properties were changed.'); return; }
    data.value[data.value.indexOf(current)] = value;
    model.setValue(data.cell, JSON.stringify(data.value));
  });
  ui.actions.addAction('schemaTableConstraints', function () {
    var table = tableOf(graph.getSelectionCell());
    if (!table) { window.alert('Select a schema table or one of its columns first.'); return; }
    var data = metadata(), id;
    try { id = decodeURIComponent(table.id.slice('table-'.length)); } catch (_) { window.alert('Invalid table ID.'); return; }
    var matches = data ? data.value.filter(function (item) { return item && item.table_id === id; }) : [];
    if (matches.length !== 1) { window.alert('Table constraints are unavailable or ambiguous. Reopen the editor.'); return; }
    var current = matches[0];
    var valid = function (v) { return typeof v === 'string' && !!v.trim() && v.length <= 256; };
    var columns = [], columnLabels = [];
    for (var i = 0; i < model.getChildCount(table); i++) {
      var child = model.getChildAt(table, i);
      if (/^column-/.test(child.id || '')) { try { var columnId = decodeURIComponent(child.id.slice('column-'.length)); columns.push(columnId); columnLabels.push(model.getValue(child) + ' = ' + columnId); } catch (_) { window.alert('Invalid column ID.'); return; } }
    }
    var input = window.prompt('Edit keys and indexes as JSON. Leave a new constraint id empty; preserve existing IDs. Example key: {"id":"","name":null,"kind":"primary","column_ids":["COLUMN_ID"]}. Example index: {"id":"","name":"idx_name","unique":false,"column_ids":["COLUMN_ID"]}. Column IDs:\n' + columnLabels.join('\n'), JSON.stringify({ keys: current.keys, indexes: current.indexes }, null, 2));
    if (input === null) return;
    var value;
    try { value = JSON.parse(input); } catch (_) { window.alert('Invalid JSON. No constraints were changed.'); return; }
    var oldIds = { key: new Set(current.keys.map(function (entry) { return entry.id; })), index: new Set(current.indexes.map(function (entry) { return entry.id; })) };
    var ids = new Set(), primary = 0;
    try {
      if (!value || typeof value !== 'object' || Array.isArray(value) || Object.keys(value).sort().join(',') !== 'indexes,keys' || !Array.isArray(value.keys) || !Array.isArray(value.indexes)) throw Error();
      [['key', value.keys], ['index', value.indexes]].forEach(function (group) {
        group[1].forEach(function (entry) {
          if (!entry || typeof entry !== 'object' || Array.isArray(entry) || Object.keys(entry).sort().join(',') !== (group[0] === 'key' ? 'column_ids,id,kind,name' : 'column_ids,id,name,unique')) throw Error();
          if (entry.id === '') entry.id = uid();
          else if (!oldIds[group[0]].has(entry.id)) throw Error();
          if (!valid(entry.id) || ids.has(entry.id) || !Array.isArray(entry.column_ids) || !entry.column_ids.length || entry.column_ids.some(function (column) { return columns.indexOf(column) < 0; }) || new Set(entry.column_ids).size !== entry.column_ids.length) throw Error();
          if (group[0] === 'key') { if (!(entry.name === null || valid(entry.name)) || (entry.kind !== 'primary' && entry.kind !== 'unique') || (entry.kind === 'primary' && ++primary > 1)) throw Error(); }
          else if (!valid(entry.name) || typeof entry.unique !== 'boolean') throw Error();
          ids.add(entry.id);
        });
      });
    } catch (_) { window.alert('Invalid constraints, IDs, or column references. No constraints were changed.'); return; }
    data.value[data.value.indexOf(current)] = Object.assign({}, current, { table_id: id, keys: value.keys, indexes: value.indexes });
    model.setValue(data.cell, JSON.stringify(data.value));
  });
  ui.actions.addAction('schemaRelationship', function () {
    var data = metadata();
    if (!data || !data.value.some(function (entry) { return entry && entry.kind === 'relationships-v1'; })) { window.alert('Relationship metadata is unavailable. Reopen the editor.'); return; }
    var selected = graph.getSelectionCell();
    var existing = selected && /^relationship-/.test(selected.id || '') ? selected : null;
    var current = null;
    if (existing) {
      var id;
      try { id = decodeURIComponent(existing.id.slice('relationship-'.length)); } catch (_) { window.alert('Invalid relationship ID.'); return; }
      var matches = data.value.filter(function (entry) { return entry && entry.relationship && entry.relationship.id === id; });
      if (matches.length !== 1) { window.alert('Relationship metadata is unavailable or ambiguous.'); return; }
      current = matches[0];
    }
    var tables = Object.create(null), descriptions = [];
    var parent = graph.getDefaultParent();
    for (var i = 0; i < model.getChildCount(parent); i++) {
      var table = model.getChildAt(parent, i);
      if (!/^table-/.test(table.id || '')) continue;
      var tableId;
      try { tableId = decodeURIComponent(table.id.slice('table-'.length)); } catch (_) { window.alert('Invalid table ID.'); return; }
      var columns = [], labels = [];
      for (var j = 0; j < model.getChildCount(table); j++) {
        var child = model.getChildAt(table, j);
        if (!/^column-/.test(child.id || '')) continue;
        try { var columnId = decodeURIComponent(child.id.slice('column-'.length)); columns.push(columnId); labels.push(model.getValue(child) + ' = ' + columnId); } catch (_) { window.alert('Invalid column ID.'); return; }
      }
      tables[tableId] = { cell: table, columns: columns };
      descriptions.push(model.getValue(table) + ' = ' + tableId + '\n  ' + labels.join('\n  '));
    }
    var initial = current ? current.relationship : { id: '', name: null, source_table_id: '', source_column_ids: [], target_table_id: '', target_column_ids: [], on_delete: null, on_update: null };
    var input = window.prompt('Edit relationship as JSON. For a new relationship leave id empty. Pair source and target columns in matching order. Actions: null, cascade, restrict, set null, no action, set default. Tables and columns:\n' + descriptions.join('\n'), JSON.stringify(initial, null, 2));
    if (input === null) return;
    var rel;
    try { rel = JSON.parse(input); } catch (_) { window.alert('Invalid JSON. No relationship was changed.'); return; }
    var valid = function (value) { return typeof value === 'string' && !!value.trim() && value.length <= 256; };
    var action = function (value) { return value === null || ['cascade', 'restrict', 'set null', 'no action', 'set default'].indexOf(value) >= 0; };
    var validColumns = function (value, available) { return Array.isArray(value) && value.length > 0 && value.every(function (column) { return available.indexOf(column) >= 0; }) && new Set(value).size === value.length; };
    if (!rel || typeof rel !== 'object' || Array.isArray(rel) || Object.keys(rel).sort().join(',') !== 'id,name,on_delete,on_update,source_column_ids,source_table_id,target_column_ids,target_table_id' || (existing ? rel.id !== id : rel.id !== '') || !(rel.name === null || valid(rel.name)) || !Object.prototype.hasOwnProperty.call(tables, rel.source_table_id) || !Object.prototype.hasOwnProperty.call(tables, rel.target_table_id) || !validColumns(rel.source_column_ids, tables[rel.source_table_id].columns) || !validColumns(rel.target_column_ids, tables[rel.target_table_id].columns) || rel.source_column_ids.length !== rel.target_column_ids.length || !action(rel.on_delete) || !action(rel.on_update)) { window.alert('Invalid relationship fields or references. No relationship was changed.'); return; }
    if (!existing) rel.id = uid();
    model.beginUpdate();
    try {
      if (existing) {
        data.value[data.value.indexOf(current)] = { relationship: rel };
        model.setTerminal(existing, tables[rel.source_table_id].cell, true);
        model.setTerminal(existing, tables[rel.target_table_id].cell, false);
      } else {
        existing = graph.insertEdge(parent, 'relationship-' + rel.id, '', tables[rel.source_table_id].cell, tables[rel.target_table_id].cell, 'edgeStyle=orthogonalEdgeStyle;endArrow=block;');
        data.value.push({ relationship: rel });
      }
      model.setValue(data.cell, JSON.stringify(data.value));
      graph.setSelectionCell(existing);
    } finally { model.endUpdate(); }
  });
  var hostOrigin = '';
  try { hostOrigin = new URL(document.referrer).origin; } catch (_) { /* The host announces itself after connecting. */ }
  var pending = new Map();
  var runtimeCounts = Object.create(null);
  window.addEventListener('message', function (event) {
    if (event.source !== window.opener || typeof event.data !== 'string') return;
    var data;
    try { data = JSON.parse(event.data); } catch (_) { return; }
    if (data.action === 'schema-migration-host') { hostOrigin = event.origin; return; }
    if (event.origin !== hostOrigin) return;
    if (data.action === 'schema-reveal-selection') {
      if ((data.kind !== 'table' && data.kind !== 'relationship') || typeof data.id !== 'string' || !data.id.trim() || data.id.length > 256 || /[\u0000-\u001f]/.test(data.id)) return;
      var target;
      try { target = model.getCell(data.kind + '-' + encodeURIComponent(data.id)); } catch (_) { return; }
      if (!target) { window.alert('The selected entity is not present in this Draw.io draft.'); return; }
      graph.setSelectionCell(target); graph.scrollCellToVisible(target); return;
    }
    if (data.action === 'schema-runtime-counts') {
      if (!data.counts || typeof data.counts !== 'object' || Array.isArray(data.counts)) return;
      var counts = Object.create(null);
      Object.keys(data.counts).slice(0, 10000).forEach(function (name) {
        var count = data.counts[name];
        if (name.length <= 256 && typeof count === 'number' && Number.isFinite(count) && count >= 0) counts[name.toLowerCase()] = count;
      });
      runtimeCounts = counts; return;
    }
    if (data.action === 'schema-draft-save-result') { if (data.ok === false) window.alert('Schema save failed: ' + (typeof data.error === 'string' ? data.error : 'Unknown error')); return; }
    if (!pending.has(data.requestId) || pending.get(data.requestId).action !== data.action) return;
    var resolve = pending.get(data.requestId).resolve; pending.delete(data.requestId); resolve(data);
  });
  ui.actions.addAction('schemaFind', function () {
    var input = window.prompt('Find a schema table or column by name:');
    if (input === null || !input.trim()) return;
    var query = input.trim().toLowerCase(), matches = [];
    var parent = graph.getDefaultParent();
    for (var i = 0; i < model.getChildCount(parent); i++) {
      var table = model.getChildAt(parent, i);
      if (!/^table-/.test(table.id || '')) continue;
      var tableName = String(model.getValue(table) || '');
      if (tableName.toLowerCase().indexOf(query) >= 0) matches.push({ cell: table, label: tableName });
      for (var j = 0; j < model.getChildCount(table); j++) {
        var column = model.getChildAt(table, j);
        if (!/^column-/.test(column.id || '')) continue;
        var label = tableName + '.' + String(model.getValue(column) || '');
        if (label.toLowerCase().indexOf(query) >= 0) matches.push({ cell: column, label: label });
      }
    }
    if (!matches.length) { window.alert('No schema tables or columns match that search.'); return; }
    var selected = 0;
    if (matches.length > 1) {
      var choices = matches.slice(0, 50);
      var choice = window.prompt('Choose a result by number' + (matches.length > 50 ? ' (first 50 shown)' : '') + ':\n' + choices.map(function (item, index) { return (index + 1) + '. ' + item.label; }).join('\n'));
      if (choice === null) return;
      if (!/^[1-9][0-9]*$/.test(choice.trim()) || Number(choice) > choices.length) { window.alert('Enter a number from the displayed results.'); return; }
      selected = Number(choice) - 1;
    }
    graph.setSelectionCell(matches[selected].cell);
    graph.scrollCellToVisible(matches[selected].cell);
  });
  ui.actions.addAction('schemaRuntime', function () {
    var table = tableOf(graph.getSelectionCell());
    if (!table) { window.alert('Select a schema table or one of its columns first.'); return; }
    var name = String(model.getValue(table) || '');
    var count = runtimeCounts[name.toLowerCase()];
    if (count === undefined && name.indexOf('.') >= 0) count = runtimeCounts[name.slice(name.lastIndexOf('.') + 1).toLowerCase()];
    window.alert(name + ': ' + (count === undefined ? 'No observed operations for the selected runtime project.' : count.toLocaleString() + ' observed operations.'));
  });
  ui.actions.addAction('schemaShowInApp', function () {
    if (!window.opener || !hostOrigin) { window.alert('The application is disconnected. Reopen the editor from the draft.'); return; }
    var cell = graph.getSelectionCell();
    if (!cell) { window.alert('Select a schema table, column, or relationship first.'); return; }
    if (/^column-/.test(cell.id || '')) cell = tableOf(cell);
    var match = cell && /^(table|relationship)-(.+)$/.exec(cell.id || '');
    if (!match) { window.alert('Select a schema table, column, or relationship first.'); return; }
    var id;
    try { id = decodeURIComponent(match[2]); } catch (_) { window.alert('The selected schema ID is invalid.'); return; }
    if (!id.trim() || id.length > 256 || /[\u0000-\u001f]/.test(id)) { window.alert('The selected schema ID is invalid.'); return; }
    window.opener.postMessage(JSON.stringify({ event: 'schema-selection', kind: match[1], id: id }), hostOrigin);
    window.opener.focus();
  });
  function requestHost(payload, action) {
    return new Promise(function (resolve, reject) {
      if (!window.opener || !hostOrigin) { reject(new Error('The application is disconnected. Reopen the editor from the draft.')); return; }
      var requestId = crypto.randomUUID();
      var timer = setTimeout(function () { pending.delete(requestId); reject(new Error('The request timed out.')); }, 90000);
      pending.set(requestId, { action: action, resolve: function (result) { clearTimeout(timer); result.ok ? resolve(result) : reject(new Error(result.error || 'Request failed.')); } });
      window.opener.postMessage(JSON.stringify(Object.assign({ requestId: requestId, xml: mxUtils.getXml(new mxCodec().encode(model)) }, payload)), hostOrigin);
    });
  }
  function requestMigration(payload) { return requestHost(payload, 'schema-migration-result'); }
  async function exportSchema(format) {
    try {
      var result = await requestHost({ event: 'schema-export-' + format }, 'schema-export-result');
      if (typeof result.content !== 'string' || new TextEncoder().encode(result.content).length > 4 * 1024 * 1024) throw new Error('The exported schema is invalid or too large.');
      var url = URL.createObjectURL(new Blob([result.content], { type: format === 'json' ? 'application/json' : 'text/plain' }));
      var link = document.createElement('a'); link.href = url; link.download = 'schema.' + format; link.click(); URL.revokeObjectURL(url);
    } catch (error) { window.alert('Schema export failed: ' + error.message); }
  }
  ui.actions.addAction('schemaExport', function () { return exportSchema('json'); });
  ui.actions.addAction('schemaExportDbml', function () {
    if (!window.confirm('DBML is an interchange format and does not preserve stable IDs, all column metadata, or the distinction between unique keys and indexes. Use schema JSON for a complete draft. Export DBML?')) return;
    return exportSchema('dbml');
  });
  ui.actions.addAction('schemaInspect', async function () {
    var target = window.prompt('Configured read-only inspection target name:');
    if (target === null) return;
    target = target.trim();
    if (!/^[A-Za-z0-9_-]{1,64}$/.test(target)) { window.alert('Enter a valid configured target name.'); return; }
    if (!window.confirm('Connect to ' + target + ' and read its column metadata? Use a least-privilege database account. The report is partial and will not change the draft or its baseline.')) return;
    try {
      var result = await requestHost({ event: 'schema-inspect', target: target, consent: true }, 'schema-inspect-result');
      var backdrop = document.createElement('div');
      backdrop.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,.55);z-index:10000;display:flex;align-items:center;justify-content:center';
      var panel = document.createElement('div'); panel.style.cssText = 'background:white;color:#222;padding:20px;width:min(900px,90vw);max-height:90vh;overflow:auto;font:14px sans-serif';
      var title = document.createElement('h2'); title.textContent = 'Read-only database inspection'; panel.appendChild(title);
      var note = document.createElement('p'); note.textContent = 'Column metadata only. Keys, indexes, relationships and other database objects are not inspected. This report does not certify or change the original schema.'; panel.appendChild(note);
      var output = document.createElement('pre'); output.style.cssText = 'white-space:pre-wrap;overflow:auto;max-height:60vh'; output.textContent = JSON.stringify(result.report, null, 2); panel.appendChild(output);
      var close = document.createElement('button'); close.textContent = 'Close'; close.onclick = function () { backdrop.remove(); }; panel.appendChild(close);
      backdrop.appendChild(panel); document.body.appendChild(backdrop);
    } catch (error) { window.alert('Inspection failed: ' + error.message); }
  });
  ui.actions.addAction('schemaMigration', function () {
    var backdrop = document.createElement('div');
    backdrop.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,.55);z-index:10000;display:flex;align-items:center;justify-content:center';
    var panel = document.createElement('div');
    panel.style.cssText = 'background:white;color:#222;padding:20px;width:min(900px,90vw);max-height:90vh;overflow:auto;font:14px sans-serif';
    var title = document.createElement('h2'); title.textContent = 'Preview migration'; panel.appendChild(title);
    var warning = document.createElement('p'); warning.textContent = 'Review the schema changes and generated PHP. AI generates the migration from the structured diff; adapter validation is bounded and does not prove the generated code is correct.'; panel.appendChild(warning);
    function selector(label, choices) {
      var wrapper = document.createElement('label'); wrapper.textContent = label + ' ';
      var select = document.createElement('select');
      choices.forEach(function (choice) { var option = document.createElement('option'); option.value = choice[0]; option.textContent = choice[1]; select.appendChild(option); });
      wrapper.appendChild(select); panel.appendChild(wrapper); panel.appendChild(document.createTextNode('  ')); return select;
    }
    var framework = selector('Framework', [['laravel', 'Laravel'], ['symfony', 'Symfony / Doctrine']]);
    var database = selector('Database', [['mysql', 'MySQL'], ['postgresql', 'PostgreSQL']]);
    var timestampLabel = document.createElement('label'); timestampLabel.textContent = 'Timestamp (YYYY_MM_DD_HHMMSS) ';
    var timestamp = document.createElement('input'); timestamp.placeholder = '2026_11_02_120000'; timestampLabel.appendChild(timestamp); panel.appendChild(timestampLabel);
    var status = document.createElement('p'); status.setAttribute('role', 'status'); panel.appendChild(status);
    var output = document.createElement('pre'); output.style.cssText = 'white-space:pre-wrap;overflow:auto;max-height:45vh;background:#f4f4f4;padding:12px'; panel.appendChild(output);
    var confirmLabel = document.createElement('label'); var confirm = document.createElement('input'); confirm.type = 'checkbox'; confirmLabel.appendChild(confirm); confirmLabel.appendChild(document.createTextNode(' I reviewed the destructive or ambiguous changes.')); confirmLabel.hidden = true; panel.appendChild(confirmLabel);
    var preview = document.createElement('button'); preview.textContent = 'Save and preview'; panel.appendChild(preview);
    var download = document.createElement('button'); download.textContent = 'Download previewed migration'; download.disabled = true; panel.appendChild(download);
    var close = document.createElement('button'); close.textContent = 'Close'; close.onclick = function () { backdrop.remove(); }; panel.appendChild(close);
    backdrop.appendChild(panel); document.body.appendChild(backdrop);
    function invalidatePreview() { download.disabled = true; confirmLabel.hidden = true; confirm.checked = false; output.textContent = ''; status.textContent = 'Settings changed. Generate a new preview.'; }
    framework.onchange = invalidatePreview; database.onchange = invalidatePreview; timestamp.oninput = invalidatePreview;
    function setPending(value) { preview.disabled = value; framework.disabled = value; database.disabled = value; timestamp.disabled = value; }
    preview.onclick = async function () {
      if (!/^\d{4}_\d{2}_\d{2}_\d{6}$/.test(timestamp.value)) { status.textContent = 'Enter a timestamp in YYYY_MM_DD_HHMMSS format.'; return; }
      setPending(true); download.disabled = true; confirm.checked = false; confirmLabel.hidden = true; status.textContent = 'Saving draft and generating preview…'; output.textContent = '';
      try {
        var result = await requestMigration({ event: 'schema-migration-preview', framework: framework.value, database: database.value, timestamp: timestamp.value });
        var data = result.preview;
        output.textContent = JSON.stringify({ operations: data.operations, warnings: data.warnings, error: data.generation_error, files: data.files }, null, 2);
        confirmLabel.hidden = !data.requires_confirmation; download.disabled = data.generation_blocked; status.textContent = data.generation_blocked ? 'Generation blocked.' : 'Review the preview before downloading.';
      } catch (error) { status.textContent = error.message; }
      finally { setPending(false); }
    };
    download.onclick = async function () {
      setPending(true); download.disabled = true; status.textContent = 'Preparing download…';
      try {
        var result = await requestMigration({ event: 'schema-migration-download', confirmed: confirm.checked });
        var url = URL.createObjectURL(new Blob([result.content], { type: 'application/x-httpd-php' }));
        var link = document.createElement('a'); link.href = url; link.download = result.filename; link.click(); URL.revokeObjectURL(url); status.textContent = 'Download requested.';
      } catch (error) { status.textContent = error.message; }
      finally { setPending(false); download.disabled = false; }
    };
  });
  var menu = ui.menus.get('extras');
  var previous = menu.funct;
  menu.funct = function (menu, parent) {
    previous.apply(this, arguments);
    ui.menus.addMenuItems(menu, ['-', 'schemaAddTable', 'schemaAddColumn', 'schemaColumnProperties', 'schemaTableConstraints', 'schemaRelationship', 'schemaFind', 'schemaRuntime', 'schemaInspect', 'schemaShowInApp', 'schemaExport', 'schemaExportDbml', 'schemaMigration'], parent);
  };
});
