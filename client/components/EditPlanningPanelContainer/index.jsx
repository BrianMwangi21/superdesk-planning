import React from 'react'
import PropTypes from 'prop-types'
import { reduxForm, formValueSelector } from 'redux-form'
import { connect } from 'react-redux'
import * as actions from '../../actions'
import { PlanningForm } from '../index'
import { EventMetadata, PlanningHistoryContainer, AuditInformation } from '../../components'
import * as selectors from '../../selectors'
import { get } from 'lodash'
import { OverlayTrigger } from 'react-bootstrap'
import { tooltips } from '../index'
import { UserAvatar, UnlockItem } from '../'
import classNames from 'classnames'
import './style.scss'
import { ItemActionsMenu } from '../index'
import { getCreator, getLockedUser, planningUtils, isItemSpiked } from '../../utils'

// Helper enum for Publish method when saving
const saveMethods = {
    SAVE: 'save', // Save Only
    PUBLISH: 'publish', // Publish Only
    UNPUBLISH: 'unpublish', // Unpublish Only
    SAVE_PUBLISH: 'save_publish', // Save & Publish
    SAVE_UNPUBLISH: 'save_unpublish', // Save & Unpublish
}

export class EditPlanningPanel extends React.Component {

    constructor(props) {
        super(props)
        this.state = {
            openUnlockPopup: false,
            previewHistory: false,

            // Local state for the type of save to do
            saveMethod: saveMethods.SAVE,
        }

        this.handleSave = this.handleSave.bind(this)
    }

    onSubmit(planning) {
        switch (this.state.saveMethod) {
            case saveMethods.PUBLISH:
                return this.props.publish(planning)
            case saveMethods.UNPUBLISH:
                return this.props.unpublish(planning)
            case saveMethods.SAVE_PUBLISH:
                return this.props.saveAndPublish(planning)
            case saveMethods.SAVE_UNPUBLISH:
                return this.props.saveAndUnpublish(planning)
            case saveMethods.SAVE:
            default:
                return this.props.save(planning)
        }
    }

    handleSave() {
        // Runs Validation on the form, then runs the above `onSubmit` function
        return this.refs.PlanningForm.getWrappedInstance().submit()
        .then(() => {
            // Restore the saveMethod to `Save Only`
            this.setState({ saveMethod: saveMethods.SAVE })
        })
    }

    handleSaveAndPublish() {
        // If the form data has not changed, `Publish Only` otherwise `Save & Publish`
        if (this.props.pristine || this.props.readOnly) {
            return this.setState({ saveMethod: saveMethods.PUBLISH }, this.handleSave)
        } else {
            return this.setState({ saveMethod: saveMethods.SAVE_PUBLISH }, this.handleSave)
        }
    }

    handleSaveAndUnpublish() {
        // If the form data has not changed, `Unpublish Only` otherwise `Save & Unpublish`
        if (this.props.pristine || this.props.readOnly) {
            return this.setState({ saveMethod: saveMethods.UNPUBLISH }, this.handleSave)
        } else {
            return this.setState({ saveMethod: saveMethods.SAVE_UNPUBLISH }, this.handleSave)
        }
    }

    toggleOpenUnlockPopup() {
        this.setState({ openUnlockPopup: !this.state.openUnlockPopup })
    }

    getLockedUser(planning) {
        return get(planning, 'lock_user') && Array.isArray(this.props.users) ?
            this.props.users.find((u) => (u._id === planning.lock_user)) : null
    }

    viewPlanningHistory() {
        this.setState({ previewHistory: true })
    }

    closePlanningHistory() {
        this.setState({ previewHistory: false })
    }

    /*eslint-disable complexity*/
    render() {
        const {
            closePlanningEditor,
            openPlanningEditor,
            planning,
            event,
            pristine,
            submitting,
            readOnly,
            lockedInThisSession,
            users,
            planningManagementPrivilege,
            notForPublication,
        } = this.props

        const creationDate = get(planning, '_created')
        const updatedDate = get(planning, '_updated')

        const author = getCreator(planning, 'original_creator', users)
        const versionCreator = getCreator(planning, 'version_creator', users)

        const lockedUser = getLockedUser(planning, this.props.users)
        const planningSpiked = isItemSpiked(planning)
        const eventSpiked = isItemSpiked(event)

        let itemActions = [{
            label: 'View Planning History',
            callback: this.viewPlanningHistory.bind(this),
        }]

        // If the planning or event or agenda item is spiked,
        // or we don't hold a lock, enforce readOnly
        let forceReadOnly = readOnly
        if (!lockedInThisSession || eventSpiked || planningSpiked) {
            forceReadOnly = true
        }

        const showSave = planningUtils.canSavePlanning(planning, event)
        const showPublish = planningUtils.canPublishPlanning(planning, event)
        const showUnpublish = planningUtils.canUnpublishPlanning(planning, event)
        const showEdit = planningUtils.canEditPlanning(
            planning,
            event,
            planningManagementPrivilege,
            lockedInThisSession,
            lockedUser
        )

        return (
            <div className="EditPlanningPanel">
                <header className="subnav">
                    <div className={classNames('TimeAndAuthor',
                        'dropdown',
                        'dropdown--drop-right',
                        'pull-left',
                        { open: this.state.openUnlockPopup })}>
                        {(!lockedInThisSession && lockedUser)
                            && (
                            <div className="lock-avatar">
                                <button type='button' onClick={this.toggleOpenUnlockPopup.bind(this)}>
                                    <UserAvatar user={lockedUser} withLoggedInfo={true} />
                                </button>
                                {this.state.openUnlockPopup && <UnlockItem user={lockedUser}
                                    showUnlock={this.props.unlockPrivilege}
                                    onCancel={this.toggleOpenUnlockPopup.bind(this)}
                                    onUnlock={this.props.unlockItem.bind(this, planning)}/>}
                            </div>
                            )}
                    </div>
                    {!this.state.previewHistory &&
                        <div className="EditPlanningPanel__actions">
                            {!forceReadOnly &&
                                <button
                                    className="btn"
                                    type="reset"
                                    onClick={closePlanningEditor.bind(this, planning)}
                                    disabled={submitting}>Cancel</button>
                            }

                            {!forceReadOnly && showSave &&
                                <button
                                    className="btn btn--primary"
                                    onClick={this.handleSave.bind(this)}
                                    type="submit"
                                    disabled={pristine || submitting}>Save</button>
                            }

                            {showPublish &&
                                <button
                                    onClick={this.handleSaveAndPublish.bind(this)}
                                    type="button"
                                    className="btn btn--success"
                                    disabled={submitting || notForPublication}>
                                    Publish
                                </button>
                            }

                            {showUnpublish &&
                                <button
                                    onClick={this.handleSaveAndUnpublish.bind(this)}
                                    type="button"
                                    className="btn btn--hollow"
                                    disabled={submitting || notForPublication}>
                                    Unpublish
                                </button>
                            }

                            {forceReadOnly && showEdit &&
                                <OverlayTrigger placement="bottom" overlay={tooltips.editTooltip}>
                                    <button
                                        className="EditPlanningPanel__actions__edit navbtn navbtn--right"
                                        onClick={openPlanningEditor.bind(this, get(planning, '_id'))}>
                                        <i className="icon-pencil"/>
                                    </button>
                                </OverlayTrigger>
                            }

                            {forceReadOnly &&
                                <OverlayTrigger placement="bottom" overlay={tooltips.closeTooltip}>
                                    <button
                                        className="EditPlanningPanel__actions__edit navbtn navbtn--right"
                                        onClick={closePlanningEditor.bind(null, null)}>
                                        <i className="icon-close-small"/>
                                    </button>
                                </OverlayTrigger>
                            }
                        </div>
                    }
                </header>

                {!this.state.previewHistory &&
                    <div className="EditPlanningPanel__body">
                        <div>
                            <AuditInformation
                                createdBy={author}
                                updatedBy={versionCreator}
                                createdAt={creationDate}
                                updatedAt={updatedDate} />
                            <ItemActionsMenu actions={itemActions} />
                        </div>

                        {planningSpiked &&
                            <span className="PlanningSpiked label label--alert">planning spiked</span>
                        }
                        {eventSpiked &&
                            <span className="EventSpiked label label--alert">event spiked</span>
                        }
                        {event &&
                            <div>
                                <h3>Associated event</h3>
                                <EventMetadata event={event}/>
                            </div>
                        }
                        <h3>Planning</h3>
                        {(!creationDate || !author) &&
                            <span>Create a new planning</span>
                        }
                        <PlanningForm
                            ref="PlanningForm"
                            onSubmit={this.onSubmit.bind(this)}
                            readOnly={forceReadOnly}/>
                    </div>
                }
                {this.state.previewHistory &&
                    <div className="history-preview">
                        <div className="close-history">
                            <a onClick={this.closePlanningHistory.bind(this)} className="close">
                                <i className="icon-close-small" />
                            </a>
                        </div>
                        <PlanningHistoryContainer currentPlanningId={planning._id} />
                    </div>
                }
            </div>
        )
    }
    /*eslint-enable*/
}

EditPlanningPanel.propTypes = {
    closePlanningEditor: PropTypes.func.isRequired,
    openPlanningEditor: PropTypes.func.isRequired,
    planning: PropTypes.object,
    event: PropTypes.object,
    pristine: PropTypes.bool.isRequired,
    submitting: PropTypes.bool.isRequired,
    users: PropTypes.oneOfType([
        PropTypes.array,
        PropTypes.object,
    ]),
    readOnly: PropTypes.bool,
    unlockPrivilege: PropTypes.bool,
    planningManagementPrivilege: PropTypes.bool,
    unlockItem: PropTypes.func,
    lockedInThisSession: PropTypes.bool,
    save: PropTypes.func,
    saveAndPublish: PropTypes.func,
    saveAndUnpublish: PropTypes.func,
    publish: PropTypes.func,
    unpublish: PropTypes.func,
    notForPublication: PropTypes.bool,
}

const selector = formValueSelector('planning') // Selector for the Planning form
const mapStateToProps = (state) => ({
    planning: selectors.getCurrentPlanning(state),
    event: selectors.getCurrentPlanningEvent(state),
    users: selectors.getUsers(state),
    readOnly: selectors.getPlanningItemReadOnlyState(state),
    unlockPrivilege: selectors.getPrivileges(state).planning_unlock ? true : false,
    planningManagementPrivilege: selectors.getPrivileges(state).planning_planning_management ? true : false,
    lockedInThisSession: selectors.isCurrentPlanningLockedInThisSession(state),
    notForPublication: selector(state, 'flags.marked_for_not_publication'),
})

const mapDispatchToProps = (dispatch) => ({
    closePlanningEditor: (planning) => dispatch(actions.planning.ui.closeEditor(planning)),
    openPlanningEditor: (planning) => (dispatch(actions.planning.ui.openEditor(planning))),
    unlockItem: (planning) => (dispatch(actions.planning.ui.unlockAndOpenEditor(planning))),

    save: (planning) => (dispatch(actions.planning.ui.saveAndReloadCurrentAgenda(planning))),
    saveAndPublish: (planning) => (dispatch(actions.planning.ui.saveAndPublish(planning))),
    saveAndUnpublish: (planning) => (dispatch(actions.planning.ui.saveAndUnpublish(planning))),

    publish: (planning) => (dispatch(actions.planning.ui.publish(planning))),
    unpublish: (planning) => (dispatch(actions.planning.ui.unpublish(planning))),
})

export const EditPlanningPanelContainer = connect(
    mapStateToProps, mapDispatchToProps
// connect to the form in order to have pristine and submitting in props
)(reduxForm({ form: 'planning' })(EditPlanningPanel))
