import { register } from '@antv/x6-vue-shape';
import EntityModelNode from '../components/EntityModelNode.vue';

/** Register the Vue entity shape used by the domain model canvas. */
export function registerEntityNode() {
  register({
    shape: 'custom-vue-entity',
    width: 280,
    height: 260,
    component: EntityModelNode,
  });
}
