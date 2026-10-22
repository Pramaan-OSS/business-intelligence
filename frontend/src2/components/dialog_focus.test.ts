import { afterEach, describe, expect, it, vi } from 'vitest'
import { nextTick, ref } from 'vue'
import { useAutofocusOnOpen } from '../../node_modules/frappe-ui/src/composables/useAutofocusOnOpen'

afterEach(() => {
	vi.unstubAllGlobals()
})

async function openWithDelayedFocus(getContainer: () => any) {
	let frame: FrameRequestCallback = () => {}
	vi.stubGlobal('window', {})
	vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) => {
		frame = callback
		return 1
	})
	useAutofocusOnOpen(ref(true), getContainer)
	await nextTick()
	return () => frame(0)
}

describe('dialog focus after opening', () => {
	it('does not crash if the dialog closes before the next frame', async () => {
		let container: any = { querySelector: vi.fn() }
		const focus = await openWithDelayedFocus(() => container)
		container = { nodeType: 8 }
		expect(focus).not.toThrow()
	})

	it('ignores an unmounted dialog', async () => {
		const focus = await openWithDelayedFocus(() => null)
		expect(focus).not.toThrow()
	})

	it('still focuses and selects the intended input in an open dialog', async () => {
		class Input {
			matches = () => true
			focus = vi.fn()
			select = vi.fn()
		}
		vi.stubGlobal('HTMLInputElement', Input)
		vi.stubGlobal('HTMLTextAreaElement', class {})
		const input = new Input()
		const focus = await openWithDelayedFocus(() => ({ querySelector: () => input }))
		focus()
		expect(input.focus).toHaveBeenCalledOnce()
		expect(input.select).toHaveBeenCalledOnce()
	})
})
