<script setup lang="ts">
import { call, Dialog, toast } from 'frappe-ui'
import { Sparkles, Wand2 } from 'lucide-vue-next'
import { computed, h, inject, ref } from 'vue'
import Code from '../../components/Code.vue'
import { useShortcut } from '../../composables/useShortcut'
import useDataSourceStore from '../../data_source/data_source'
import { wheneverChanges } from '../../helpers'
import { __ } from '../../translation'
import { Query } from '../query'
import ResultPane from '../../components/result_pane/ResultPane.vue'
import QueryActions from './QueryActions.vue'
import QueryDataTable from './QueryDataTable.vue'
import QueryHeader from './QueryHeader.vue'
import QueryInfo from './QueryInfo.vue'
import SchemaExplorer from './SchemaExplorer.vue'
import DataSourceSelector from './source_selector/DataSourceSelector.vue'

const query = inject<Query>('query')!
const $find = ref<HTMLElement>()
query.autoExecute = false
query.ensureResult()

const operation = query.getSQLOperation()
const data_source = ref(operation ? operation.data_source : '')
const sql = ref(operation ? operation.raw_sql : '')

function execute(force: boolean = false) {
	if (!data_source.value) {
		toast.error(__('Please select a data source first'))
		return
	}
	query.setSQL(
		{
			raw_sql: sql.value,
			data_source: data_source.value,
		},
		force,
	)
}

const formatting = ref(false)
async function format() {
	if (!sql.value.trim() || formatting.value) return

	formatting.value = true
	try {
		sql.value = await query.formatSQL({
			raw_sql: sql.value,
			data_source: data_source.value,
		})
	} catch (error) {
		toast.error(__('Failed to format SQL'))
	} finally {
		formatting.value = false
	}
}

const extraActions = () => [
	{
		label: __('Draft SQL'),
		icon: h(Sparkles, { class: 'h-3 w-3 text-ink-gray-6', strokeWidth: 1.5 }),
		onClick: () => (showDraftDialog.value = true),
	},
	{
		label: __('Format SQL'),
		icon: h(Wand2, { class: 'h-3 w-3 text-ink-gray-6', strokeWidth: 1.5 }),
		onClick: () => format(),
	},
]

const showDraftDialog = ref(false)
const draftQuestion = ref('')
const draftLoading = ref(false)
const draftError = ref('')
const draft = ref<{ sql: string; complete: boolean } | null>(null)

async function getDraft() {
	if (!data_source.value) {
		draftError.value = __('Select a data source first')
		return
	}
	if (!draftQuestion.value.trim()) {
		draftError.value = __('Ask a question first')
		return
	}
	draftLoading.value = true
	draftError.value = ''
	draft.value = null
	try {
		draft.value = await call('insights.api.draft.draft_sql', {
			data_source: data_source.value,
			question: draftQuestion.value,
		})
	} catch (error: any) {
		draftError.value = error?.messages?.[0] || error?.message || __('The draft could not be written')
	} finally {
		draftLoading.value = false
	}
}

function insertDraft() {
	if (!draft.value?.sql) return
	insertTextIntoEditor(draft.value.sql)
	showDraftDialog.value = false
}

const codeEditor = ref<InstanceType<typeof Code> | null>(null)
function insertTextIntoEditor(text: string) {
	if (codeEditor.value) {
		codeEditor.value.insertText(text)
	}
}

const dataSourceSchema = ref<Record<string, any>>({})
const dataSourceStore = useDataSourceStore()
wheneverChanges(
	data_source,
	() => {
		if (!data_source.value) {
			dataSourceSchema.value = {}
			return
		}
		dataSourceStore.getSchema(data_source.value).then((schema: any) => {
			dataSourceSchema.value = schema
		})
	},
	{ immediate: true },
)
const completions = computed(() => {
	if (!Object.keys(dataSourceSchema.value).length)
		return {
			schema: {},
			tables: [],
		}

	const schema: Record<string, any> = {}
	Object.entries(dataSourceSchema.value).forEach(([table, tableData]) => {
		schema[table] = tableData.columns.map((column: any) => ({
			label: column.label,
			detail: column.type,
		}))
	})

	const tables = Object.entries(dataSourceSchema.value).map(([table, tableData]) => ({
		label: table,
		detail: tableData.label,
	}))

	return {
		schema,
		tables,
	}
})

useShortcut('Meta+e', () => {
	execute(true)
})

// The SQL in the editor reaches the query only on a run, so the text is a
// staleness source of its own — the query's own check cannot see it.
const stale = computed(() => {
	const operation = query.getSQLOperation()
	return sql.value !== (operation ? operation.raw_sql : '') || query.isStale
})
</script>

<template>
	<div class="flex flex-1 overflow-hidden">
		<div class="relative flex h-full flex-1 flex-col gap-3 overflow-hidden px-4 pb-4 pt-3">
			<QueryHeader>
				<DataSourceSelector
					v-model="data_source"
					:placeholder="__('Select a data source')"
				/>
				<QueryActions
					:on-execute="() => execute(true)"
					:extra-actions="extraActions"
					:stale="stale"
				/>
			</QueryHeader>

			<!-- SQL Editor -->
			<div class="relative flex flex-1 flex-col overflow-hidden rounded-4 border">
				<Code
					ref="codeEditor"
					:key="completions.tables.length"
					v-model="sql"
					language="sql"
					:schema="completions.schema"
					:tables="completions.tables"
				/>
			</div>

			<!-- Results Table -->
			<div class="relative flex h-[45%] w-full flex-col gap-2">
				<div ref="$find" class="flex flex-shrink-0"></div>
				<ResultPane :query="query" :stale="stale" :find-target="$find">
					<template #grid="{ rows, currentPage, pageSize }">
						<QueryDataTable
							:query="query"
							:rows="rows"
							:current-page="currentPage"
							:page-size="pageSize"
						/>
					</template>
				</ResultPane>
			</div>
		</div>

		<!-- Right Sidebar -->
		<div
			class="relative flex h-full w-[19rem] flex-shrink-0 flex-col overflow-y-auto bg-surface-base"
		>
			<QueryInfo />

			<!-- Schema Explorer -->
			<SchemaExplorer :schema="dataSourceSchema" @insert-text="insertTextIntoEditor" />
		</div>

		<Dialog
			v-model:open="showDraftDialog"
			:title="__('Draft SQL')"
			size="2xl"
			:actions="[{
				label: __('Insert into editor'),
				variant: 'solid',
				disabled: !draft?.sql,
				onClick: insertDraft,
			}]"
		>
			<template #default>
				<div class="flex flex-col gap-3">
					<FormControl
						type="textarea"
						:label="__('What do you want to know?')"
						v-model="draftQuestion"
						:placeholder="__('e.g. monthly revenue by product category')"
					/>
					<Button
						variant="subtle"
						:label="__('Get draft')"
						:loading="draftLoading"
						@click="getDraft"
					/>
					<ErrorMessage v-if="draftError" :message="draftError" />
					<div v-if="draft?.sql" class="flex flex-col gap-1.5">
						<p class="text-p-sm text-ink-gray-6">
							{{
								draft.complete
									? __('Review the draft, then insert it to edit or run it yourself.')
									: __('The draft is incomplete. Retry with a shorter question.')
							}}
						</p>
						<pre class="overflow-x-auto rounded border bg-surface-gray-2 p-3 font-mono text-p-sm">{{ draft.sql }}</pre>
					</div>
				</div>
			</template>
		</Dialog>
	</div>
</template>
