<script setup lang="ts">
import { computed, ref, watchEffect } from 'vue';
import { Check, ChevronDown, ChevronRight, Folder, Minus } from 'lucide-vue-next';

export type DirectoryNode = { name: string; path: string; children: DirectoryNode[] };
const props = defineProps<{
  node: DirectoryNode;
  depth: number;
  patterns: string[];
  disabled?: boolean;
}>();
const emit = defineEmits<{ toggle: [value: { path: string; excluded: boolean }] }>();
const expanded = ref(props.depth === 0);
const checkbox = ref<HTMLInputElement | null>(null);
const hasChildren = computed(() => props.node.children.length > 0);

const segmentMatches = (pattern: string, value: string) => {
  let source = '^';
  for (let index = 0; index < pattern.length; index++) {
    const character = pattern[index];
    if (character === '*') source += '.*';
    else if (character === '?') source += '.';
    else if (character === '[') {
      const close = pattern.indexOf(']', index + 1);
      if (close > index + 1) {
        let group = pattern.slice(index + 1, close);
        if (group[0] === '!') group = '^' + group.slice(1);
        source += `[${group}]`;
        index = close;
      } else source += '\\[';
    } else source += character.replace(/[|\\{}()[\]^$+?.]/g, '\\$&');
  }
  return new RegExp(source + '$').test(value);
};
const matchesSegments = (pattern: string[], path: string[]) => {
  const visit = (patternIndex: number, pathIndex: number): boolean => {
    if (patternIndex === pattern.length) return pathIndex === path.length;
    if (pattern[patternIndex] === '**') {
      return visit(patternIndex + 1, pathIndex)
        || (pathIndex < path.length && visit(patternIndex, pathIndex + 1));
    }
    return pathIndex < path.length
      && segmentMatches(pattern[patternIndex], path[pathIndex])
      && visit(patternIndex + 1, pathIndex + 1);
  };
  return visit(0, 0);
};
const patternMatchesDirectory = (pattern: string, directory: string) => {
  const patternParts = pattern.replace(/\/$/, '').split('/');
  const pathParts = directory.split('/');
  return pathParts.some((_, index) => matchesSegments(patternParts, pathParts.slice(0, index + 1)));
};
const excludedByPattern = (path: string) => props.patterns.some((pattern) => patternMatchesDirectory(pattern, path));
const hasDirectExclusion = computed(() => props.patterns.some((pattern) =>
  pattern === props.node.path || pattern === `${props.node.path}/**`));
type NodeState = { checked: boolean; indeterminate: boolean };
const stateFor = (node: DirectoryNode): NodeState => {
  if (props.patterns.some((pattern) => patternMatchesDirectory(pattern, node.path))) {
    return { checked: true, indeterminate: false };
  }
  if (!node.children.length) return { checked: false, indeterminate: false };
  const children = node.children.map(stateFor);
  const checkedChildren = children.filter((child) => child.checked).length;
  const hasMixedChild = children.some((child) => child.indeterminate);
  return {
    checked: checkedChildren === children.length && !hasMixedChild,
    indeterminate: hasMixedChild || (checkedChildren > 0 && checkedChildren < children.length),
  };
};
const state = computed(() => stateFor(props.node));
const checked = computed(() => state.value.checked);
const indeterminate = computed(() => state.value.indeterminate);
const inherited = computed(() => !hasDirectExclusion.value && excludedByPattern(props.node.path));
watchEffect(() => {
  if (checkbox.value) checkbox.value.indeterminate = indeterminate.value;
});
const toggle = (event: Event) => {
  emit('toggle', { path: props.node.path, excluded: (event.target as HTMLInputElement).checked });
};
</script>

<template>
  <li role="treeitem" :aria-expanded="hasChildren ? expanded : undefined" :aria-level="depth + 1">
    <div class="flex min-h-9 items-center gap-1 rounded-md pr-2 hover:bg-slate-800/50" :style="{ paddingLeft: `${depth * 18 + 4}px` }">
      <button v-if="hasChildren" type="button" class="grid h-7 w-7 shrink-0 place-items-center rounded text-slate-500 hover:bg-slate-700 hover:text-slate-200 focus-visible:outline focus-visible:outline-2 focus-visible:outline-indigo-300" :aria-label="`${expanded ? 'Collapse' : 'Expand'} ${node.name}`" @click="expanded = !expanded">
        <ChevronDown v-if="expanded" :size="14" /><ChevronRight v-else :size="14" />
      </button>
      <span v-else class="w-7 shrink-0" aria-hidden="true"></span>
      <span class="relative grid h-[18px] w-[18px] shrink-0 place-items-center" :title="inherited ? 'This folder is already excluded by a broader pattern. Remove that pattern below to change it.' : undefined">
        <input ref="checkbox" type="checkbox" :checked="checked" :disabled="disabled || inherited" :aria-checked="indeterminate ? 'mixed' : checked ? 'true' : 'false'" :aria-label="`Exclude folder ${node.path}`" :class="checked || indeterminate ? 'border-indigo-400 bg-indigo-500' : 'border-slate-600 bg-slate-950/80'" class="absolute inset-0 z-10 m-0 h-[18px] w-[18px] cursor-pointer appearance-none rounded-[5px] border transition-colors hover:border-indigo-300 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-indigo-300 focus-visible:ring-offset-2 focus-visible:ring-offset-slate-900 disabled:cursor-not-allowed disabled:border-slate-700 disabled:bg-slate-900 disabled:opacity-55 forced-colors:appearance-auto" @change="toggle" />
        <Minus v-if="indeterminate" :size="11" :stroke-width="3" class="pointer-events-none absolute z-20 text-white" aria-hidden="true" />
        <Check v-else-if="checked" :size="11" :stroke-width="3" class="pointer-events-none absolute z-20 text-white" aria-hidden="true" />
      </span>
      <Folder :size="15" class="shrink-0 text-amber-300" aria-hidden="true" />
      <span class="truncate font-mono text-xs text-slate-300" :title="node.path">{{ node.name }}</span>
      <span v-if="inherited" class="ml-auto shrink-0 text-[10px] text-slate-500">excluded by pattern</span>
    </div>
    <ul v-if="hasChildren && expanded" role="group" class="border-l border-slate-800/80">
      <FolderTreeBranch v-for="child in node.children" :key="child.path" :node="child" :depth="depth + 1" :patterns="patterns" :disabled="disabled" @toggle="emit('toggle', $event)" />
    </ul>
  </li>
</template>
