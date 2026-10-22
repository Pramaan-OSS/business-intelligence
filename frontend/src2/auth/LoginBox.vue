<template>
	<div class="h-full w-full pt-4 sm:pt-16">
		<div class="relative">
			<div class="flex">
				<span class="mx-auto text-2xl font-semibold text-ink-gray-8">
					{{ __('Business Intelligence') }}
				</span>
			</div>
			<div
				class="mx-auto bg-surface-base px-4 py-8 sm:mt-6 sm:w-96 sm:rounded-6 sm:px-8 sm:shadow-xl"
			>
				<div class="mb-6 text-center">
					<span class="text-base text-ink-gray-8">{{ props.title }}</span>
				</div>
				<slot></slot>
			</div>
		</div>
	</div>
</template>

<script setup>
import { __ } from '../translation'
const props = defineProps(['title'])
</script>
