<script setup lang="ts">
import { computed } from 'vue';
import FolderTreeBranch, { type DirectoryNode } from './FolderTreeBranch.vue';

const props = defineProps<{
  directories: string[];
  patterns: string[];
  disabled?: boolean;
}>();
const emit = defineEmits<{ toggle: [value: { path: string; excluded: boolean }] }>();

const roots = computed(() => {
  const root: DirectoryNode = { name: '', path: '', children: [] };
  const nodes = new Map<string, DirectoryNode>();
  for (const path of props.directories) {
    const parts = path.split('/').filter(Boolean);
    let parent = root;
    let currentPath = '';
    for (const name of parts) {
      currentPath = currentPath ? `${currentPath}/${name}` : name;
      let node = nodes.get(currentPath);
      if (!node) {
        node = { name, path: currentPath, children: [] };
        nodes.set(currentPath, node);
        parent.children.push(node);
      }
      parent = node;
    }
  }
  const sort = (items: DirectoryNode[]) => {
    items.sort((left, right) => left.name.localeCompare(right.name, undefined, { numeric: true, sensitivity: 'base' }));
    items.forEach((item) => sort(item.children));
  };
  sort(root.children);
  return root.children;
});
</script>

<template>
  <div class="max-h-80 overflow-auto rounded-lg border border-slate-800 bg-slate-950/70 p-2" aria-label="Project folder exclusions">
    <p v-if="!directories.length" class="p-3 text-sm text-slate-500">No folders were found in this revision.</p>
    <ul v-else role="tree" aria-label="Project folders" class="space-y-0.5">
      <FolderTreeBranch v-for="node in roots" :key="node.path" :node="node" :depth="0" :patterns="patterns" :disabled="disabled" @toggle="emit('toggle', $event)" />
    </ul>
  </div>
</template>
