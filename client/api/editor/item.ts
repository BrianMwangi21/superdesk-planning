import {cloneDeep} from 'lodash';
import {EDITOR_TYPE, IEditorAPI} from '../../interfaces';
import {planningApi} from '../../superdeskApi';

import * as selectors from '../../selectors';
import {getPlanningInstance} from './item_planning';
import {getEventsInstance} from './item_events';
import {getRelatedEventIdsForPlanning} from '../../utils/planning';

export function getItemInstance(type: EDITOR_TYPE): IEditorAPI['item'] {
    const events = getEventsInstance(type);
    const planning = getPlanningInstance(type);

    function getItemType() {
        return planningApi.editor(type).form.getProps().itemType;
    }

    function getItemId() {
        return planningApi.editor(type).form.getProps().itemId;
    }

    function getAssociatedPlannings() {
        const state = planningApi.redux.store.getState();
        const eventId = planningApi.editor(type).item.getItemId();
        const plans = selectors.planning.storedPlannings(state);

        return Object.keys(plans)
            .filter((planId) => (
                plans[planId] != null &&
                getRelatedEventIdsForPlanning(plans[planId], 'primary').includes(eventId))
            )
            .map((planId) => cloneDeep(plans[planId]));
    }

    return {
        events,
        planning,
        getItemType,
        getItemId,
        getAssociatedPlannings,
    };
}
