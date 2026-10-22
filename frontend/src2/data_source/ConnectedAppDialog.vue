<script setup lang="ts">
import { call } from 'frappe-ui'
import { onBeforeUnmount, ref } from 'vue'
import useDataSourceStore from './data_source'

const show = defineModel<boolean>()
const store = useDataSourceStore()
const query = ref('')
const results = ref<{ app: string; description: string }[]>([])
const app = ref('')
const connected = ref(false)
const connectUrl = ref('')
const title = ref('')
const path = ref('')
const rowsKey = ref('rows')
const error = ref('')
const busy = ref(false)
let timer: ReturnType<typeof setTimeout> | undefined
let polls = 0
onBeforeUnmount(() => clearTimeout(timer))
async function request<T>(method: string, args = {}): Promise<T | undefined> {
    error.value = ''
    busy.value = true
    try { return await call(`insights.api.connected_sources.${method}`, args) as T }
    catch (e: any) { error.value = e.messages?.[0] || e.message || 'Connected sources are not available yet.' }
    finally { busy.value = false }
}
async function search() {
    results.value = await request<{ app: string; description: string }[]>('search', { query: query.value }) || []
}
async function connect(selected: string) {
    clearTimeout(timer)
    app.value = selected
    connected.value = false
    connectUrl.value = ''
    const offer = await request<{ connectUrl: string }>('connect', { app: selected })
    if (!offer) return
    connectUrl.value = offer.connectUrl
    polls = 0
    timer = setTimeout(poll, 3000)
}
async function poll() {
    if (!show.value) return
    const rows = await request<{ app: string; status: string }[]>('connections')
    connected.value = rows?.some((r: any) => r.app === app.value && r.status === 'active') || false
    if (!connected.value && !error.value && ++polls < 60) timer = setTimeout(poll, 3000)
    else if (!connected.value && !error.value) error.value = 'Connection timed out. Start connecting again.'
}
async function save() {
    const name = await request<string>('create', { title: title.value, app: app.value, path: path.value, rows_key: rowsKey.value })
    if (name) { await store.getSources(); show.value = false }
}
</script>

<template>
    <Dialog v-model:open="show" title="Connected app data source" size="xl">
        <template #default>
            <div class="flex flex-col gap-3">
                <p>Connections can grant broad account access. This source only reads data.</p>
                <p>Available apps depend on the catalogue. Unavailable reads show “not available yet”.</p>
                <FormControl label="Find a spreadsheet or app" v-model="query" />
                <Button label="Search catalogue" :loading="busy" :disabled="!query" @click="search" />
                <Button v-for="(item, index) in results" :key="index" :label="item.description || item.app" :disabled="busy" @click="connect(item.app)" />
                <a v-if="connectUrl" :href="connectUrl" target="_blank" rel="noopener noreferrer">Open secure connection</a>
                <p v-if="connectUrl">{{ connected ? 'Connected. Review the read below.' : 'Waiting for the connection.' }}</p>
                <FormControl label="Source title" v-model="title" />
                <FormControl label="Reviewed read path (for example /records)" v-model="path" />
                <FormControl label="Rows field (blank for an array)" v-model="rowsKey" />
                <p>Saving approves this read now and hourly. Up to 1000 records per read.</p>
                <p>Refresh later with Update Tables. Incomplete reads keep stored rows.</p>
                <p v-if="error" role="alert">{{ error }}</p>
                <Button label="Save and read data" variant="solid" :disabled="!connected || !title || !path || busy" :loading="busy" @click="save" />
            </div>
        </template>
    </Dialog>
</template>
