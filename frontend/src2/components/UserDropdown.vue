<template>
	<div>
		<Dropdown :options="userDropdownOptions">
			<template v-slot="{ open }">
				<button
					class="flex h-16 items-center rounded-5 py-2 duration-300 ease-in-out"
					:class="
						props.isCollapsed
							? 'w-auto px-0'
							: open
							  ? 'w-52 bg-surface-elevation-2 px-2 shadow-sm'
							  : 'w-52 px-2 hover:bg-surface-gray-3'
					"
				>
					<img
						src="../assets/app-mark.svg"
						alt="logo"
						class="h-8 w-8 flex-shrink-0 rounded-4"
					/>
					<div
						class="flex min-w-0 flex-1 flex-col text-left duration-300 ease-in-out"
						:class="
							props.isCollapsed
								? 'ml-0 w-0 overflow-hidden opacity-0'
								: 'ml-2 w-auto opacity-100'
						"
					>
						<div class="text-sm leading-tight text-ink-gray-8">Business Intelligence</div>
						<div class="mt-1 truncate text-sm leading-none text-ink-gray-6">
							{{
								session.user.full_name == 'Administrator'
									? __(session.user.full_name)
									: session.user.full_name
							}}
						</div>
					</div>
					<div
						class="duration-300 ease-in-out"
						:class="
							props.isCollapsed
								? 'ml-0 w-0 overflow-hidden opacity-0'
								: 'ml-2 w-auto opacity-100'
						"
					>
						<ChevronDown class="h-4 w-4 text-ink-gray-5" aria-hidden="true" />
					</div>
				</button>
			</template>
		</Dropdown>

	</div>
</template>

<script setup lang="ts">
import { Dropdown, useColorScheme } from 'frappe-ui'
import { ChevronDown, LogOut, Moon, Sun, ToggleRight } from 'lucide-vue-next'
import { computed, h, ref } from 'vue'
import { confirmDialog } from '../helpers/confirm_dialog'
import session from '../session'
import { __ } from '../translation'

const props = defineProps<{ isCollapsed?: boolean }>()

const { colorScheme, toggleColorScheme } = useColorScheme()
const isDark = computed(() => colorScheme.value === 'dark')

const userDropdownOptions = computed(() => {
	const options: { label: string; icon: any; onClick: () => void }[] = []

	if (session.user.is_admin) {
		options.push({
			label: __('Switch to Desk'),
			icon: h(ToggleRight),
			onClick: () => window.open('/app', '_blank'),
		})
	}
	options.push({
		label: isDark.value ? __('Light mode') : __('Dark mode'),
		icon: h(isDark.value ? Sun : Moon),
		onClick: toggleColorScheme,
	})
	options.push({
		label: __('Log out'),
		icon: h(LogOut),
		onClick: () =>
			confirmDialog({
				title: __('Log out'),
				message: __('Are you sure you want to log out?'),
				onSuccess: session.logout,
			}),
	})

	return options
})

</script>
