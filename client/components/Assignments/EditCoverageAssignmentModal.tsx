import React from 'react';
import {connect} from 'react-redux';
import {cloneDeep, set} from 'lodash';

import {gettext} from '../../utils';

import * as selectors from '../../selectors';

import {Modal} from '../';
import {Button} from '../UI';
import {AssignmentEditor} from './AssignmentEditor';
import {IDesk, IUser} from 'superdesk-api';
import {ICoverageProvider} from 'interfaces';

interface IProps {
    handleHide: () => void;
    modalProps: {
        field?: string;
        value?: any;
        onChange?: (...args: any) => any;
        priorityPrefix?: string;
        disableDeskSelection?: boolean;
        disableUserSelection?: boolean;
        setCoverageDefaultDesk?: (...args: any) => void;
    };
    users?: Array<IUser>;
    desks?: Array<IDesk>;
    coverageProviders?: Array<ICoverageProvider>;
    priorities?: Array<any>;
}

interface IState {
    valid?: boolean;
    submitting?: boolean;
    diff?: any;
}

export class EditCoverageAssignmentModalComponent extends React.Component<IProps, IState> {
    constructor(props) {
        super(props);

        this.state = {
            submitting: false,
            diff: cloneDeep(props.modalProps.value) ?? {},
            valid: true,
        };

        this.onSubmit = this.onSubmit.bind(this);
        this.onChange = this.onChange.bind(this);
        this.setValid = this.setValid.bind(this);
    }

    onChange(field, value) {
        const diffCopy = cloneDeep(this.state.diff);

        set(diffCopy, field, value);
        this.setState({diff: diffCopy});
    }

    onSubmit() {
        this.setState({submitting: true}, () => {
            this.props.modalProps.onChange(this.props.modalProps.field, this.state.diff);
            this.props.modalProps.setCoverageDefaultDesk(this.state.diff);
            this.props.handleHide();
        });
    }

    setValid(valid) {
        this.setState({valid});
    }

    render() {
        const {handleHide, users, desks, coverageProviders, priorities} = this.props;
        const {priorityPrefix = '', disableDeskSelection, disableUserSelection} = this.props.modalProps;
        const {valid, submitting, diff} = this.state;

        return (
            <Modal
                show={true}
                onHide={handleHide}
                fill={false}
                removeTabIndexAttribute={true}
            >
                <Modal.Header>
                    <h3 className="modal__heading">{gettext('Coverage Assignment Details')}</h3>
                    {submitting ? null : (
                        <a className="icn-btn" aria-label={gettext('Close')} onClick={handleHide}>
                            <i className="icon-close-small" />
                        </a>
                    )}
                </Modal.Header>
                <Modal.Body>
                    <div className="update-assignment">
                        <AssignmentEditor
                            value={diff}
                            onChange={this.onChange}
                            users={users}
                            desks={desks}
                            coverageProviders={coverageProviders}
                            priorities={priorities}
                            priorityPrefix={priorityPrefix}
                            disableDeskSelection={disableDeskSelection}
                            disableUserSelection={disableUserSelection}
                            setValid={this.setValid}
                        />
                    </div>
                </Modal.Body>
                <Modal.Footer>
                    <Button
                        text={gettext('Cancel')}
                        disabled={submitting}
                        onClick={handleHide}
                    />
                    <Button
                        text={gettext('OK')}
                        color="primary"
                        disabled={!valid || submitting}
                        onClick={this.onSubmit}
                        enterKeyIsClick={true}
                    />
                </Modal.Footer>
            </Modal>
        );
    }
}

const mapStateToProps = (state) => ({
    users: selectors.general.users(state),
    desks: selectors.general.desks(state),
    coverageProviders: selectors.vocabs.coverageProviders(state),
    priorities: selectors.getAssignmentPriorities(state),
});

export const EditCoverageAssignmentModal = connect(
    mapStateToProps,
    null
)(EditCoverageAssignmentModalComponent);
