<script setup lang="ts">
import { computed, onBeforeUnmount, ref } from 'vue';
import { BaseEdge, type EdgeProps } from '@vue-flow/core';

type ControlPoint = { x: number; y: number };
type MovableEdgeData = {
  controlPoint?: ControlPoint;
  onControlPointChange?: (edgeId: string, clientX: number, clientY: number) => void;
};

const props = defineProps<EdgeProps<MovableEdgeData>>();
const dragging = ref(false);

const controlPoint = computed<ControlPoint>(() => props.data?.controlPoint || {
  x: (props.sourceX + props.targetX) / 2,
  y: (props.sourceY + props.targetY) / 2,
});

const path = computed(() => {
  const point = controlPoint.value;
  return `M ${props.sourceX},${props.sourceY} Q ${point.x},${point.y} ${props.targetX},${props.targetY}`;
});

const labelPoint = computed(() => {
  const point = controlPoint.value;
  return {
    x: (props.sourceX + 2 * point.x + props.targetX) / 4,
    y: (props.sourceY + 2 * point.y + props.targetY) / 4,
  };
});

const stopDragging = () => {
  dragging.value = false;
  window.removeEventListener('pointermove', moveControlPoint);
  window.removeEventListener('pointerup', stopDragging);
};

const moveControlPoint = (event: PointerEvent) => {
  if (!dragging.value) return;
  props.data?.onControlPointChange?.(props.id, event.clientX, event.clientY);
};

const startDragging = (event: PointerEvent) => {
  event.preventDefault();
  event.stopPropagation();
  dragging.value = true;
  window.addEventListener('pointermove', moveControlPoint);
  window.addEventListener('pointerup', stopDragging);
};

onBeforeUnmount(stopDragging);
</script>

<template>
  <g class="movable-schema-edge">
    <BaseEdge
      :id="id"
      :path="path"
      :label="label"
      :label-x="labelPoint.x"
      :label-y="labelPoint.y"
      :interaction-width="24"
      :class="selected ? 'schema-edge-selected' : ''"
    />
    <circle
      :cx="controlPoint.x"
      :cy="controlPoint.y"
      :r="dragging ? 8 : 6"
      class="schema-edge-control-point"
      :class="{ 'schema-edge-control-point-active': dragging }"
      @pointerdown="startDragging"
    />
  </g>
</template>
