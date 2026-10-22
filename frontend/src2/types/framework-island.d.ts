declare module '@framework/ui/island' {
	import type { App, Component } from 'vue'

	export function mountVueIsland(
		el: HTMLElement,
		options: {
			component: Component
			host?: Record<string, unknown>
			props?: Record<string, unknown>
			styles?: string[]
			configure?: (app: App) => void
			routes?: import('vue-router').RouteRecordRaw[]
		},
	): Promise<{
		app: App
		shadow_root: ShadowRoot
		update: (props: Record<string, unknown>) => void
		unmount: () => void
	}>
}
