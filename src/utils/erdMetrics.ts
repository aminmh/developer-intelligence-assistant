import type { ErdTable } from '../types';

export const ERD_NODE_METRICS = {
  HEADER_HEIGHT: 38,
  ROW_HEIGHT: 28,
  MIN_WIDTH: 240,
  CHAR_WIDTH_ESTIMATE: 7.5,
  HORIZONTAL_PADDING: 14,
  PORT_SIZE: 8,
} as const;

export function tableFieldLabel(field: ErdTable['fields'][number]) {
  return `${field.name}: ${field.type}${field.nullable ? ' · null' : ''}`;
}

export function calculateMaxContentWidth(table: ErdTable) {
  const longest = Math.max(
    table.name.length,
    ...table.fields.map((field) => tableFieldLabel(field).length),
    12,
  );
  return Math.ceil(
    longest * ERD_NODE_METRICS.CHAR_WIDTH_ESTIMATE
      + ERD_NODE_METRICS.HORIZONTAL_PADDING * 2
      + ERD_NODE_METRICS.PORT_SIZE,
  );
}

export function erdTableDimensions(table: ErdTable) {
  return {
    width: Math.max(ERD_NODE_METRICS.MIN_WIDTH, calculateMaxContentWidth(table)),
    height: ERD_NODE_METRICS.HEADER_HEIGHT + table.fields.length * ERD_NODE_METRICS.ROW_HEIGHT,
  };
}

