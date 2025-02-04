/* eslint-disable react/no-multi-comp */
import React from 'react';
import classNames from 'classnames';
import {get} from 'lodash';

import {Button} from '../../';
import {Row, LineInput} from '../';
import './style.scss';
import {superdeskApi} from '../../../../superdeskApi';
import {planningApis} from '../../../../api';
import {
    EDITOR_TYPE,
    IAssignmentItem,
    IG2ContentType,
    IPlanningCoverageItem,
    IPlanningNewsCoverageStatus,
} from 'interfaces';
import {IDesk} from 'superdesk-api';

interface IProps {
    field: string;
    value: Array<any>;
    onChange(field: string, value: Array<any>): void;
    addButtonComponent: React.ComponentClass;
    addButtonProps: any;
    addButtonText: string;
    maxCount: number;
    addOnly: boolean;
    originalCount: number;
    element: React.ComponentClass<any>;
    defaultElement: any;
    readOnly: boolean;
    message: any;
    invalid: boolean;
    row: boolean;
    buttonWithLabel: boolean;
    label: string;
    labelClassName: string;
    hint: string;
    required: boolean;
    boxed: boolean;
    noMargin: boolean;
    item: any;
    diff: any;
    formProfile: any;
    errors: {[key: string]: any};
    showErrors: boolean;
    testId?: string;
    onRemoveAssignment: (assignment: IAssignmentItem) => Promise<void>;
    getRef?(field: string, value: any): React.RefObject<any>;
    popupContainer(): HTMLElement;
    onPopupOpen(): void;
    onPopupClose(): void;
    setCoverageDefaultDesk(coverage: IPlanningCoverageItem): void;
    contentTypes: Array<IG2ContentType>;
    defaultDesk: IDesk;
    newsCoverageStatus: Array<IPlanningNewsCoverageStatus>;
    navigation: any;
    openCoverageIds: Array<string>;
    preferredCoverageDesks: {[key: string]: string};
    editorType: EDITOR_TYPE;
}

export class InputArray extends React.PureComponent<IProps> {
    constructor(props) {
        super(props);

        this.onAdd = this.onAdd.bind(this);
        this.remove = this.remove.bind(this);
    }

    onAdd(...args) {
        let currentValue = this.props.value ?? [];
        const newElement = typeof this.props.defaultElement === 'function' ?
            this.props.defaultElement(...args) :
            this.props.defaultElement;

        this.props.onChange(this.props.field, [...currentValue, newElement]);
    }

    remove(index: number) {
        const {gettext} = superdeskApi.localization;
        const {confirm, notify} = superdeskApi.ui;

        confirm(
            gettext('Remove Coverage')
        ).then((response) => {
            if (response) {
                this.props.onChange(
                    this.props.field,
                    (this.props.value ?? []).filter((value, i) => i !== index)
                );
                notify.success(
                    gettext('The coverage has been removed')
                );
            }
        });
    }

    renderButton() {
        if (this.props.addButtonComponent != null) {
            const AddButton = this.props.addButtonComponent;

            return (
                <AddButton
                    onAdd={this.onAdd}
                    {...this.props.addButtonProps}
                />
            );
        }

        const props = this.props.row ? {
            onAdd: this.onAdd,
            text: this.props.addButtonText,
        } : {
            onAdd: this.onAdd,
            text: this.props.addButtonText,
            tabIndex: 0,
            enterKeyIsClick: true,
        };

        return <Button {...props} />;
    }

    render() {
        const {
            field,
            value = [],
            onChange,
            addButtonComponent,
            addButtonProps,
            addButtonText,
            maxCount = 0,
            addOnly,
            originalCount,
            element,
            defaultElement = {},
            readOnly,
            message,
            invalid,
            row = true,
            buttonWithLabel,
            label,
            labelClassName,
            testId,
            ...props
        } = this.props;

        const Component = element;
        const showAddButton = (maxCount ? value.length < maxCount : true) && !readOnly;
        const isIndexReadOnly = (index) => (addOnly && index === originalCount) ? false : readOnly;
        const addButton = this.renderButton();

        return (
            <Row
                noPadding={!!message}
                testId={testId}
            >
                {!label?.length ? null : (
                    <div>
                        <div className={classNames('InputArray__label', labelClassName)}>{label}</div>
                        {buttonWithLabel && showAddButton && addButton}
                    </div>
                )}
                {get(message, field) && (
                    <LineInput
                        invalid={true}
                        message={get(message, field)}
                        readOnly
                        noLabel
                    />
                )}
                {(value || []).map((val, index) => (
                    <Component
                        {...props}
                        onRemoveAssignment={(val) =>
                            planningApis.assignments.getById(val.assigned_to.assignment_id)
                                .then((assignment) => this.props.onRemoveAssignment(assignment))
                        }
                        key={index}
                        ref={this.props.getRef == null ? null : this.props.getRef(field, val)}
                        testId={`${testId}[${index}]`}
                        index={index}
                        field={`${field}[${index}]`}
                        onChange={onChange}
                        value={val}
                        remove={() => this.remove(index)}
                        readOnly={isIndexReadOnly(index)}
                        message={get(message, `[${index}]`)}
                        invalid={!!get(message, `[${index}]`)}
                    />
                ))}
                {!buttonWithLabel && showAddButton && addButton}
            </Row>
        );
    }
}
