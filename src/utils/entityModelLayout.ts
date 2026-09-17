export interface EntityLayoutNode {
  id: string;
  width: number;
  height: number;
}

export interface EntityLayoutPosition {
  x: number;
  y: number;
}

const intersects = (
  left: EntityLayoutPosition & EntityLayoutNode,
  right: EntityLayoutPosition & EntityLayoutNode,
  gap: number,
) => (
  left.x < right.x + right.width + gap
  && left.x + left.width + gap > right.x
  && left.y < right.y + right.height + gap
  && left.y + left.height + gap > right.y
);

/** Returns whether any node pair overlaps or violates the requested minimum gap. */
export const hasEntityLayoutCollisions = (
  nodes: EntityLayoutNode[],
  positions: Map<string, EntityLayoutPosition>,
  gap = 80,
) => nodes.some((node, index) => {
  const position = positions.get(node.id);
  if (!position) return false;
  const rect = { ...node, ...position };
  return nodes.slice(index + 1).some((other) => {
    const otherPosition = positions.get(other.id);
    return otherPosition && intersects(rect, { ...other, ...otherPosition }, gap);
  });
});

/**
 * Keeps the layout engine's left-to-right ordering, then moves a node below
 * prior collisions until every rendered rectangle has the requested gap.
 */
export const resolveEntityLayoutCollisions = (
  nodes: EntityLayoutNode[],
  positions: Map<string, EntityLayoutPosition>,
  gap = 80,
) => {
  const resolved = new Map<string, EntityLayoutPosition>();
  const ordered = [...nodes].sort((left, right) => {
    const leftPosition = positions.get(left.id) || { x: 0, y: 0 };
    const rightPosition = positions.get(right.id) || { x: 0, y: 0 };
    return leftPosition.x - rightPosition.x || leftPosition.y - rightPosition.y || left.id.localeCompare(right.id);
  });

  for (const node of ordered) {
    const initial = positions.get(node.id) || { x: 40, y: 40 };
    const position = { ...initial };
    for (let attempt = 0; attempt <= resolved.size; attempt += 1) {
      const collisions = [...resolved.entries()].flatMap(([id, otherPosition]) => {
        const other = nodes.find((candidate) => candidate.id === id);
        return other && intersects({ ...node, ...position }, { ...other, ...otherPosition }, gap) ? [{ ...other, ...otherPosition }] : [];
      });
      if (!collisions.length) break;
      position.y = Math.max(position.y, ...collisions.map((collision) => collision.y + collision.height + gap));
    }
    resolved.set(node.id, position);
  }
  return resolved;
};
