<script setup lang="ts">
import { nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue';
import { Graph } from '@antv/x6';
import { DagreLayout } from '@antv/layout';
import type { Edge, Node } from '@antv/x6';
import type { DomainEntity, DomainEntityBehavior, DomainEntityRelationship, DomainModelGraphEdge, DomainModelResponse } from '../types';
import { registerEntityNode } from '../utils/registerEntityNode';
import { entityMatchesModelSearch, entityModelBehaviorKey, entityModelBehaviors } from '../utils/entityModelSearch';

const props = defineProps<{
  model: DomainModelResponse | null;
  search?: string;
  selectedEntityId?: string;
  selectedBehaviorKey?: string;
  selectedRelationshipId?: string;
}>();

const emit = defineEmits<{
  (event: 'select-entity', entity: DomainEntity): void;
  (event: 'select-behavior', entity: DomainEntity, behavior: DomainEntityBehavior): void;
  (event: 'select-relationship', relationship: DomainEntityRelationship): void;
  (event: 'ready', graph: Graph): void;
}>();

const container = ref<HTMLElement | null>(null);
let graph: Graph | null = null;
let localPositions = new Map<string, { x: number; y: number }>();
let focusZoomFrame: number | null = null;

const entityId = (entity: DomainEntity) => entity.id || `entity:${entity.name}`;
const entityFromNodeId = (id: string) => {
  const model = props.model;
  const needle = String(id || '').toLowerCase();
  return model?.entities.find((item) => (
    entityId(item) === id
    || `node-${item.name}` === id
    || item.name === id
    || entityId(item).toLowerCase() === needle
    || `node-${item.name}`.toLowerCase() === needle
    || item.name.toLowerCase() === needle
  )) || null;
};
const edgeValue = (edge: DomainModelGraphEdge, key: 'source' | 'target') => {
  const endpoint = edge[key];
  return typeof endpoint === 'string' ? endpoint : endpoint?.cell;
};
const normalizedEndpoint = (value: string | undefined) => String(value || '').replace(/^node-/, '');
const relationshipForEdge = (edge: DomainModelGraphEdge, index: number): DomainEntityRelationship | null => {
  const source = entityFromNodeId(edgeValue(edge, 'source') || '');
  const target = entityFromNodeId(edgeValue(edge, 'target') || '');
  if (!source || !target) return null;
  const data = edge.data || {};
  const label = edge.label || String((edge.labels?.[0] as any)?.attrs?.text?.text || '');
  return {
    id: edge.id || `relation:${entityId(source)}:${entityId(target)}:${index}`,
    source_entity: source.name,
    target_entity: target.name,
    name: String(data.name || data.relation_name || label || 'relationship'),
    relation_name: String(data.relation_name || data.name || label || ''),
    type: String(data.type || ''),
    cardinality: String(data.cardinality || label || ''),
    foreign_key: (data.foreign_key as string | null | undefined) ?? null,
    description: (data.description as string | null | undefined) ?? null,
  };
};

const dimensions = (entity: DomainEntity) => ({ width: 280, height: Math.max(150, 92 + Math.min(14, entity.properties.length) * 25 + Math.min(10, entityModelBehaviors(entity).length) * 25 + (entity.properties.length > 14 ? 20 : 0)) });
const visible = (entity: DomainEntity) => {
  return entityMatchesModelSearch(entity, props.search || '');
};

const nodeFor = (entity: DomainEntity, index: number, isVisible: boolean) => {
  const id = entityId(entity);
  const size = dimensions(entity);
  const backendNode = (props.model?.antv_x6?.nodes || []).find((node) => node.id === id || node.id === `node-${entity.name}`);
  const backendPosition = backendNode?.position || (typeof backendNode?.x === 'number' && typeof backendNode?.y === 'number' ? { x: backendNode.x, y: backendNode.y } : null);
  const position = localPositions.get(id) || backendPosition || { x: 40 + (index % 3) * 360, y: 40 + Math.floor(index / 3) * 300 };
  const portY = (fieldIndex: number) => 58 + fieldIndex * 25 + 12;
  return {
    id,
    shape: 'custom-vue-entity',
    x: position.x,
    y: position.y,
    width: size.width,
    height: size.height,
    visible: isVisible,
    data: {
      entity,
      name: entity.name,
      table: entity.table,
      framework: entity.framework,
      selected: props.selectedEntityId === id,
      selectedBehaviorKey: props.selectedBehaviorKey || '',
      onBehaviorClick: (behavior: DomainEntityBehavior) => emit('select-behavior', entity, behavior),
    },
      ports: {
      groups: {
        left: { position: 'absolute', attrs: { circle: { r: 4, magnet: true, stroke: '#0f172a', strokeWidth: 1.5, fill: '#818cf8' } } },
        right: { position: 'absolute', attrs: { circle: { r: 4, magnet: true, stroke: '#0f172a', strokeWidth: 1.5, fill: '#22d3ee' } } },
      },
      items: (entity.properties || []).flatMap((field, fieldIndex) => [
        { id: `field:${field.name}`, group: 'left', args: { x: 0, y: portY(fieldIndex) } },
        { id: `field:${field.name}:out`, group: 'right', args: { x: size.width, y: portY(fieldIndex) } },
      ]),
    },
  };
};

const rebuild = () => {
  if (!graph || !props.model) return;
  graph.clearCells();
  const entities = props.model.entities || [];
  const shown = new Set(entities.filter(visible).map(entityId));
  graph.addNodes(entities.map((entity, index) => nodeFor(entity, index, shown.has(entityId(entity)))));
  const rawEdges = props.model.antv_x6?.edges || [];
  const relationships = rawEdges.length ? rawEdges : entities.flatMap((entity) => (entity.relationships || []).map((relationship) => ({
    id: relationship.id,
    source: entityId(entity),
    target: relationship.target_entity,
    label: [relationship.cardinality, relationship.name || relationship.relation_name].filter(Boolean).join(' '),
    data: relationship,
  })));
  graph.addEdges(relationships.flatMap((rawEdge, index) => {
    const sourceId = entityFromNodeId(normalizedEndpoint(edgeValue(rawEdge, 'source') || ''));
    const targetId = entityFromNodeId(normalizedEndpoint(edgeValue(rawEdge, 'target') || ''));
    const relationship = relationshipForEdge(rawEdge, index) || (rawEdge.data as DomainEntityRelationship | undefined);
    if (!sourceId || !targetId || !shown.has(entityId(sourceId)) || !shown.has(entityId(targetId))) return [];
    return [{
      id: rawEdge.id || relationship?.id || `relation:${entityId(sourceId)}:${entityId(targetId)}:${index}`,
      shape: 'edge',
      source: { cell: entityId(sourceId), port: (rawEdge as any).sourcePort || undefined },
      target: { cell: entityId(targetId), port: (rawEdge as any).targetPort || (relationship?.foreign_key ? `field:${relationship.foreign_key}` : undefined) },
      router: { name: 'manhattan', args: { padding: 18 } },
      connector: { name: 'rounded', args: { radius: 8 } },
      attrs: { line: { stroke: props.selectedRelationshipId === (relationship?.id || rawEdge.id) ? '#22d3ee' : '#89b4fa', strokeWidth: props.selectedRelationshipId === (relationship?.id || rawEdge.id) ? 2.8 : 1.5, targetMarker: { name: 'classic', width: 8, height: 7 } } },
      labels: [{ position: 0.5, attrs: { label: { text: [relationship?.cardinality, relationship?.name || relationship?.relation_name].filter(Boolean).join(' · ') || rawEdge.label || 'relation', fill: '#bac2de', fontSize: 10 }, body: { fill: '#181825', stroke: '#334155', rx: 4, ry: 3 } } }],
      data: relationship || rawEdge.data || {},
    } as any];
  }));
  graph.zoomToFit({ padding: 24, maxScale: 1.2 });
};

const selectNode = (cell: Node) => { const entity = entityFromNodeId(cell.id); if (entity) emit('select-entity', entity); };
const selectEdge = (cell: Edge) => { const relationship = cell.getData() as DomainEntityRelationship; if (relationship?.target_entity) emit('select-relationship', relationship); };
const syncSelection = () => {
  if (!graph) return;
  graph.getNodes().forEach((node) => {
    const data = node.getData() || {};
    const selected = props.selectedEntityId === node.id;
    const selectedBehaviorKey = props.selectedBehaviorKey || '';
    if (data.selected !== selected || data.selectedBehaviorKey !== selectedBehaviorKey) node.setData({ ...data, selected, selectedBehaviorKey });
  });
  graph.getEdges().forEach((edge) => {
    const data = edge.getData() as DomainEntityRelationship | undefined;
    const selected = props.selectedRelationshipId === (data?.id || edge.id);
    edge.setAttrs({
      line: {
        stroke: selected ? '#22d3ee' : '#89b4fa',
        strokeWidth: selected ? 2.8 : 1.5,
      },
    });
  });
};
const fit = () => graph?.zoomToFit({ padding: 24, maxScale: 1.2 });
const zoomIn = () => graph?.zoom(0.15);
const zoomOut = () => graph?.zoom(-0.15);
const animateFocusZoom = (node: Node, targetZoom: number) => {
  if (!graph) return;
  if (focusZoomFrame !== null) cancelAnimationFrame(focusZoomFrame);
  const startZoom = graph.zoom();
  if (Math.abs(targetZoom - startZoom) < 0.01 || window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
    graph.zoomTo(targetZoom, { maxScale: targetZoom });
    graph.centerCell(node);
    return;
  }
  const startedAt = performance.now();
  const duration = 180;
  const frame = (now: number) => {
    if (!graph) return;
    const progress = Math.min(1, (now - startedAt) / duration);
    const eased = 1 - (1 - progress) ** 3;
    graph.zoomTo(startZoom + (targetZoom - startZoom) * eased, { maxScale: targetZoom });
    graph.centerCell(node);
    if (progress < 1) focusZoomFrame = requestAnimationFrame(frame);
    else focusZoomFrame = null;
  };
  focusZoomFrame = requestAnimationFrame(frame);
};
const focusNode = async (id: string, zoom = false) => {
  await nextTick();
  const node = graph?.getCellById(id);
  if (!node || !graph) return;
  graph.centerCell(node);
  if (zoom) animateFocusZoom(node, 1.25);
  graph.centerCell(node);
};
const reset = () => { localPositions = new Map((props.model?.entities || []).map((entity, index) => [entityId(entity), { x: 40 + (index % 3) * 360, y: 40 + Math.floor(index / 3) * 300 }])); rebuild(); };
const prettify = async () => {
  if (!props.model) return;
  const entities = props.model.entities || [];
  const nodes = entities.map((entity) => {
    const size = dimensions(entity);
    return { id: entityId(entity), width: size.width, height: size.height };
  });
  const edges = (props.model.antv_x6?.edges || []).flatMap((edge) => {
    const source = entityFromNodeId(edgeValue(edge, 'source') || '');
    const target = entityFromNodeId(edgeValue(edge, 'target') || '');
    if (!source || !target) return [];
    return [{
      id: edge.id || `${entityId(source)}-${entityId(target)}`,
      source: entityId(source),
      target: entityId(target),
    }];
  });
  try {
    const layout = new DagreLayout({
      type: 'dagre',
      rankdir: 'LR',
      nodesep: 70,
      ranksep: 150,
      controlPoints: false,
      nodeSize: (node: { width: number; height: number }) => [node.width, node.height],
    });
    await layout.execute({ nodes, edges }, { width: 1600, height: 1000 });
    const positions = new Map<string, { x: number; y: number }>();
    layout.forEachNode((node) => positions.set(String(node.id), {
      x: Math.max(40, node.x - ((node.size as any)?.[0] || 0) / 2),
      y: Math.max(40, node.y - ((node.size as any)?.[1] || 0) / 2),
    }));
    localPositions = positions;
  } catch {
    reset();
    return;
  }
  rebuild();
};

defineExpose({ fit, zoomIn, zoomOut, focusNode, reset, prettify });

onMounted(() => {
  if (!container.value) return;
  registerEntityNode();
  graph = new Graph({ container: container.value, background: { color: '#0b1020' }, grid: { visible: true, type: 'dot', args: { color: '#1e293b', thickness: 1 } }, panning: true, mousewheel: { enabled: true, modifiers: ['ctrl', 'meta'] }, interacting: { nodeMovable: true, edgeMovable: false, edgeLabelMovable: false } });
  graph.on('node:click', ({ node, e }) => {
    const target = e?.target as Element | null;
    const behaviorElement = target?.closest?.('[data-behavior-key]') as HTMLElement | null;
    if (behaviorElement) {
      const entity = entityFromNodeId(node.id);
      const behavior = entity && entityModelBehaviors(entity).find((item) => entityModelBehaviorKey(entity, item) === behaviorElement.dataset.behaviorKey);
      if (entity && behavior) emit('select-behavior', entity, typeof behavior === 'string' ? { name: behavior, behavior_key: entityModelBehaviorKey(entity, behavior) } : behavior);
      return;
    }
    selectNode(node);
  });
  graph.on('edge:click', ({ edge }) => selectEdge(edge));
  graph.on('node:moved', ({ node }) => localPositions.set(node.id, node.getPosition()));
  emit('ready', graph);
  rebuild();
});
// Model loads and enrichment replace the model object. Watching it deeply makes
// X6 node interaction look like a model update, rebuilding cells during a drag.
watch(() => [props.model, props.search], rebuild);
watch(() => [props.selectedEntityId, props.selectedBehaviorKey, props.selectedRelationshipId], syncSelection);
onBeforeUnmount(() => { if (focusZoomFrame !== null) cancelAnimationFrame(focusZoomFrame); graph?.dispose(); graph = null; });
</script>

<template><div ref="container" class="h-full w-full" /></template>
