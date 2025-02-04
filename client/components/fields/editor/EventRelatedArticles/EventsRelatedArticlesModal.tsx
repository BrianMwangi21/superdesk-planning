import React from 'react';

import {IArticle, IRestApiResponse, ISuperdeskQuery} from 'superdesk-api';
import {IPlanningConfig} from '../../../../interfaces';
import {superdeskApi} from '../../../../superdeskApi';
import {appConfig as config} from 'appConfig';

import {cleanArticlesFields} from './utils';

import {
    SearchBar,
    Modal,
    Dropdown,
    Spacer,
    Button,
    WithPagination,
    Loader,
    Panel,
    PanelHeader,
    PanelContent,
    PanelContentBlock,
    LayoutContainer,
    HeaderPanel,
    MainPanel,
    RightPanel,
    SubNav,
} from 'superdesk-ui-framework/react';
import {RelatedArticlesListComponent} from './RelatedArticlesListComponent';
import {PreviewArticle} from './PreviewArticle';

import '../../../../components/Archive/ArchivePreview/style.scss';

const appConfig = config as IPlanningConfig;

interface IProps {
    closeModal: () => void;
    selectedArticles?: Array<Partial<IArticle>>;
    onChange: (value: Array<Partial<IArticle>>) => void;
}

interface IState {
    articles: Array<Partial<IArticle>>;
    searchQuery: string;
    loading: boolean;
    currentlySelectedArticles?: Array<Partial<IArticle>>;
    activeLanguage: {code: string; label: string;};
    previewItem: Partial<IArticle> | null;
    repo: string | null;
    languages: Array<{label: string, code: string}>;
}


export class EventsRelatedArticlesModal extends React.Component<IProps, IState> {
    constructor(props: IProps) {
        super(props);

        this.state = {
            articles: [],
            searchQuery: '',
            loading: true,
            currentlySelectedArticles: this.props.selectedArticles,
            activeLanguage: {label: 'All languages', code: ''},
            previewItem: null,
            repo: null,
            languages: [],
        };
    }

    componentDidMount() {
        const {httpRequestJsonLocal} = superdeskApi;
        const {getLanguageVocabulary} = superdeskApi.entities.vocabulary;
        const searchProviderName = appConfig.planning.event_related_item_search_provider_name;

        if (searchProviderName != null) {
            httpRequestJsonLocal<IRestApiResponse<any>>({
                method: 'GET',
                path: '/search_providers',
                urlParams: {
                    manage: 1,
                }
            }).then((result) => {
                const repoId = result._items.find((provider) => (
                    provider.search_provider === searchProviderName
                ))?._id;

                this.setState({
                    repo: repoId,
                    languages: [
                        ...getLanguageVocabulary().items.map(({name, qcode}) => ({label: name, code: qcode})),
                        {
                            label: 'All languages',
                            code: ''
                        }
                    ]
                });
            });
        }
    }

    componentDidUpdate(_prevProps: Readonly<IProps>, prevState: Readonly<IState>): void {
        if (prevState.activeLanguage.code !== this.state.activeLanguage.code
            || prevState.searchQuery !== this.state.searchQuery
            || prevState.repo !== this.state.repo
        ) {
            // eslint-disable-next-line react/no-did-update-set-state
            this.setState({
                loading: true,
            });
        }
    }

    render(): React.ReactNode {
        const {closeModal} = this.props;
        const {gettext} = superdeskApi.localization;
        const {getProjectedFieldsArticle} = superdeskApi.entities.article;
        const {httpRequestJsonLocal} = superdeskApi;
        const {superdeskToElasticQuery} = superdeskApi.helpers;

        return (
            <Modal
                headerTemplate={gettext('Search Related Articles')}
                visible
                contentPadding="none"
                contentBg="medium"
                size="x-large"
                onHide={closeModal}
                footerTemplate={(
                    <Spacer h gap="8" noWrap alignItems="end" justifyContent="end">
                        <Button
                            onClick={() => {
                                closeModal();
                            }}
                            text={gettext('Cancel')}
                            style="filled"
                        />
                        <Button
                            onClick={() => {
                                this.props.onChange(cleanArticlesFields(this.state.currentlySelectedArticles ?? []));
                                closeModal();
                            }}
                            disabled={JSON.stringify(this.props.selectedArticles)
                                === JSON.stringify(this.state.currentlySelectedArticles)}
                            text={gettext('Apply')}
                            style="filled"
                            type="primary"
                        />
                    </Spacer>
                )}
            >
                <LayoutContainer>
                    <HeaderPanel>
                        <SubNav className="px-2">
                            <SearchBar
                                value={this.state.searchQuery}
                                onSubmit={(value: string) => {
                                    this.setState({
                                        searchQuery: value,
                                    });
                                }}
                                placeholder={gettext('Search...')}
                                boxed
                            >
                                <Dropdown
                                    maxHeight={300}
                                    items={[
                                        {
                                            type: 'group',
                                            items: this.state.languages.map((language) => ({
                                                label: language.label,
                                                onSelect: () => {
                                                    this.setState({
                                                        activeLanguage: language
                                                    });
                                                }
                                            }))
                                        },
                                    ]}
                                >
                                    {this.state.activeLanguage.label}
                                </Dropdown>
                            </SearchBar>
                        </SubNav>
                    </HeaderPanel>
                    <MainPanel>
                        <WithPagination
                            key={this.state.activeLanguage.code + this.state.searchQuery + this.state.repo}
                            pageSize={20}
                            getItems={(pageNo, pageSize, signal) => {
                                if (this.state.repo == null) {
                                    return Promise.resolve({items: [], itemCount: 0});
                                }

                                const query: ISuperdeskQuery = {
                                    filter: {},
                                    page: pageNo,
                                    max_results: pageSize,
                                    sort: [{versioncreated: 'desc'}],
                                };
                                const urlParams: {[key: string]: any} = {
                                    aggregations: 0,
                                    es_highlight: 1,
                                    repo: this.state.repo,
                                    projections: JSON.stringify(getProjectedFieldsArticle()),
                                };

                                if (this.state.activeLanguage.code !== '') {
                                    query.filter.language = {$eq: this.state.activeLanguage.code};
                                    urlParams.params = {languages: this.state.activeLanguage.code};
                                }

                                if (this.state.searchQuery !== '') {
                                    query.fullTextSearch = this.state.searchQuery.toLowerCase();
                                }

                                return httpRequestJsonLocal<IRestApiResponse<Partial<IArticle>>>({
                                    method: 'GET',
                                    path: '/search_providers_proxy',
                                    urlParams: {
                                        ...urlParams,
                                        ...superdeskToElasticQuery(query),
                                    },
                                    abortSignal: signal,
                                })
                                    .then((res) => {
                                        this.setState({
                                            loading: false,
                                        });

                                        return {items: res._items, itemCount: res._meta.total};
                                    });
                            }}
                        >
                            {
                                (items: Array<Partial<IArticle>>) => (
                                    <div className="sd-padding-y--1-5">
                                        <Spacer
                                            v
                                            gap="4"
                                            justifyContent="center"
                                            alignItems="center"
                                            noWrap
                                        >
                                            {items.map((articleFromArchive) => (
                                                <RelatedArticlesListComponent
                                                    key={articleFromArchive.guid}
                                                    article={articleFromArchive}
                                                    setPreview={(itemToPreview) => {
                                                        this.setState({
                                                            previewItem: itemToPreview,
                                                        });
                                                    }}
                                                    removeArticle={(articleId: string) => {
                                                        const filteredArray =
                                                [...(this.state.currentlySelectedArticles ?? [])]
                                                    .filter(({guid}) => guid !== articleId);

                                                        this.setState({
                                                            currentlySelectedArticles: filteredArray
                                                        });
                                                    }}
                                                    prevSelected={(this.props.selectedArticles ?? [])
                                                        .find((x) => x.guid === articleFromArchive.guid) != null
                                                    }
                                                    addArticle={(article: Partial<IArticle>) => {
                                                        this.setState({
                                                            currentlySelectedArticles: [
                                                                ...(this.state.currentlySelectedArticles ?? []),
                                                                {
                                                                    ...article,
                                                                    search_provider: this.state.repo,
                                                                },
                                                            ]
                                                        });
                                                    }}
                                                    openInPreview={
                                                this.state.previewItem?.guid === articleFromArchive.guid
                                                    }
                                                />
                                            ))}
                                        </Spacer>
                                    </div>
                                )
                            }
                        </WithPagination>
                        {this.state.loading && (
                            <div
                                style={{
                                    justifySelf: 'center',
                                    alignSelf: 'center',
                                    display: 'flex',
                                    width: '100%',
                                    height: '100%',
                                }}
                            >
                                <Loader overlay />
                            </div>
                        )}
                    </MainPanel>
                    <RightPanel open={this.state.previewItem != null}>
                        <Panel
                            open={this.state.previewItem != null}
                            side="right"
                            size="medium"
                            className="sd-panel-bg--000"
                        >
                            <PanelHeader
                                title={gettext('Article preview')}
                                onClose={() => {
                                    this.setState({
                                        previewItem: null,
                                    });
                                }}
                            />
                            <PanelContent empty={this.state.previewItem == null} >
                                <PanelContentBlock>
                                    {this.state.previewItem && (<PreviewArticle item={this.state.previewItem} />)}
                                </PanelContentBlock>
                            </PanelContent>
                        </Panel>
                    </RightPanel>
                </LayoutContainer>
            </Modal>
        );
    }
}
