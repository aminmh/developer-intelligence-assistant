import { Graph } from '@antv/x6';

/** Register the native SVG table shape used by ErdCanvas. */
export function registerErdNode() {
  Graph.registerNode('erd-table', {
    inherit: 'rect',
    markup: [
      { tagName: 'rect', selector: 'body' },
      { tagName: 'rect', selector: 'header' },
      { tagName: 'rect', selector: 'divider' },
      { tagName: 'text', selector: 'title' },
      { tagName: 'text', selector: 'count' },
      { tagName: 'text', selector: 'label' },
    ],
  }, true);
}
