import { register } from '@antv/x6-vue-shape';
import ErdTableNode from '../components/ErdTableNode.vue';

/** Register the Vue table shape used by ErdCanvas. */
export function registerErdNode() {
  register({
    shape: 'erd-vue-table',
    width: 280,
    height: 38,
    component: ErdTableNode,
  });
}
