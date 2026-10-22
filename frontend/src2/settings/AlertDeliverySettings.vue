<script setup lang="ts">
import { call, toast } from 'frappe-ui'
import { computed, onBeforeUnmount, ref } from 'vue'
import { __ } from '../translation'
import session from '../session'
import SettingItem from './SettingItem.vue'

type Delivery = {
	configured: boolean
	connected: boolean
	channel_label: string
	notify_subscriber_id: string
	workflow_webhook_url: string
	accounts: { account_id: string; platform: string; handle: string; display_name: string }[]
}

const delivery = ref<Delivery>({
	configured: false,
	connected: false,
	channel_label: '',
	notify_subscriber_id: '',
	workflow_webhook_url: '',
	accounts: [],
})

const loading = ref(false)
const connecting = ref(false)
const pairing = ref<{ code: string; bot_username: string; instructions: string[] } | null>(null)
let pollTimer: ReturnType<typeof setTimeout> | null = null

async function load() {
	loading.value = true
	try {
		delivery.value = await call('insights.api.alert_channels.get_alert_delivery')
		notifyId.value = delivery.value.notify_subscriber_id || ''
		workflowUrl.value = delivery.value.workflow_webhook_url || ''
	} finally {
		loading.value = false
	}
}
load()

function startConnect() {
	connecting.value = true
	call('insights.api.alert_channels.start_channel_connect')
		.then((offer: any) => {
			pairing.value = {
				code: offer.code,
				bot_username: offer.bot_username,
				instructions: offer.instructions || [],
			}
			pollTimer = setTimeout(pollConnect, 4000)
		})
		.catch(() => toast.error(__('Could not start connecting')))
		.finally(() => (connecting.value = false))
}

function pollConnect() {
	if (!pairing.value) return
	call('insights.api.alert_channels.check_channel_connect', { code: pairing.value.code })
		.then((result: any) => {
			if (result.status === 'connected') {
				pairing.value = null
				toast.success(__('Channel connected'))
				load()
			} else if (result.status === 'expired') {
				pairing.value = null
			} else {
				pollTimer = setTimeout(pollConnect, 4000)
			}
		})
		.catch(() => (pairing.value = null))
}

onBeforeUnmount(() => {
	if (pollTimer) clearTimeout(pollTimer)
})

function disconnect() {
	call('insights.api.alert_channels.disconnect_channel').then(() => {
		toast.success(__('Channel disconnected'))
		load()
	})
}

const notifyId = ref('')
const workflowUrl = ref('')

const dirty = computed(
	() =>
		notifyId.value !== (delivery.value.notify_subscriber_id || '') ||
		workflowUrl.value !== (delivery.value.workflow_webhook_url || ''),
)

function saveTargets() {
	call('insights.api.alert_channels.save_delivery_targets', {
		notify_subscriber_id: notifyId.value,
		workflow_webhook_url: workflowUrl.value,
	})
		.then(() => {
			toast.success(__('Settings Updated'))
			load()
		})
		.catch(() => toast.error(__('Could not save')))
}

const canManage = computed(() => session.user.is_admin)
</script>

<template>
	<div class="flex w-full flex-col gap-6 overflow-y-scroll p-8 px-10">
		<h1 class="text-2xl-semibold">{{ __('Alert delivery') }}</h1>
		<p v-if="!delivery.configured" class="text-p-base text-ink-gray-6" data-testid="no-key-note">
			{{ __('Add PRAMAAN_API_KEY to .env') }}
		</p>
		<SettingItem
			:label="__('Channel message')"
			:description="
				__(
					'Connect a chat channel once. Channel alerts then post there. The pairing code is short-lived and the connection is held by the platform, not by this app.',
				)
			"
		>
			<div class="flex flex-col items-end gap-2">
				<span v-if="delivery.connected" class="text-p-sm text-ink-gray-6">
					{{ delivery.channel_label }}
				</span>
				<Button
					v-if="!pairing && !delivery.connected"
					variant="subtle"
					size="sm"
					:label="__('Connect channel')"
					:loading="connecting"
					:disabled="!canManage || !delivery.configured"
					@click="startConnect"
				/>
				<Button
					v-if="delivery.connected"
					variant="subtle"
					size="sm"
					:label="__('Disconnect')"
					:disabled="!canManage"
					@click="disconnect"
				/>
			</div>
		</SettingItem>

		<div v-if="pairing" class="flex flex-col gap-2 rounded border p-4">
			<span class="text-p-base-medium">{{ __('Add the bot to your channel, then send it this code') }}</span>
			<span class="font-mono text-lg">{{ pairing.code }}</span>
			<ul class="list-disc pl-5 text-p-sm text-ink-gray-6">
				<li v-for="line in pairing.instructions" :key="line">{{ line }}</li>
			</ul>
			<span class="text-p-sm text-ink-gray-5">{{ __('Waiting for the connection…') }}</span>
		</div>

		<SettingItem
			:label="__('Notification subscriber')"
			:description="
				__(
					'The subscriber that in-app alert notifications are sent to.',
				)
			"
		>
			<div class="flex w-full flex-col items-end gap-2">
				<FormControl
					class="w-64"
					type="text"
					v-model="notifyId"
					:placeholder="__('Subscriber id')"
					:disabled="!delivery.configured"
				/>
				<Button
					variant="solid"
					size="sm"
					:label="__('Update')"
					:disabled="!dirty || !delivery.configured"
					@click="saveTargets"
				/>
			</div>
		</SettingItem>

		<SettingItem
			:label="__('Workflow address')"
			:description="
				__(
					'The public https address of a workflow on your connected workflow account. Workflow alerts hand the payload there.',
				)
			"
		>
			<div class="flex w-full flex-col items-end gap-2">
				<FormControl
					class="w-96"
					type="text"
					v-model="workflowUrl"
					:placeholder="__('https://…')"
					:disabled="!delivery.configured"
				/>
				<Button
					variant="solid"
					size="sm"
					:label="__('Update')"
					:disabled="!dirty || !delivery.configured"
					@click="saveTargets"
				/>
			</div>
		</SettingItem>
	</div>
</template>
