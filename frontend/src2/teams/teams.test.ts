import { describe, expect, it, vi } from 'vitest'

const mocks = vi.hoisted(() => ({ call: vi.fn(), confirm: vi.fn() }))
vi.mock('frappe-ui', () => ({ call: mocks.call, toast: { success: vi.fn() } }))
vi.mock('../translation', () => ({ __: (text: string) => text }))
vi.mock('../helpers', () => ({ showErrorToast: vi.fn() }))
vi.mock('../helpers/confirm_dialog', () => ({ confirmDialog: mocks.confirm }))

const team = {
	name: 'Reviewers',
	team_name: 'Reviewers',
	team_members: [],
	team_permissions: [],
	creation: '2026-10-18',
}

async function readyStore() {
	vi.resetModules()
	mocks.call.mockReset()
	mocks.call.mockResolvedValue([team])
	const { default: useTeams } = await import('./teams')
	const store = useTeams()
	await store.getTeams()
	return store
}

describe('team mutations', () => {
	it('keeps a rename pending until the refreshed names arrive', async () => {
		const store = await readyStore()
		mocks.call.mockClear()
		let finish: (rows: unknown[]) => void = () => {}
		mocks.call.mockImplementation((method: string) =>
			method.endsWith('get_teams')
				? new Promise((resolve) => {
						finish = resolve
				  })
				: Promise.resolve(),
		)
		let done = false
		const update = store.updateTeam({ ...team, team_name: 'Renamed' } as any).then(() => {
			done = true
		})
		await vi.waitFor(() =>
			expect(mocks.call).toHaveBeenCalledWith('insights.api.user.get_teams', {
				search_term: '',
			}),
		)
		await Promise.resolve()
		expect(done).toBe(false)
		expect(store.updatingTeam).toBe(true)
		finish([{ ...team, name: 'Renamed', team_name: 'Renamed' }])
		await update
		expect(store.teams[0].name).toBe('Renamed')
		expect(store.updatingTeam).toBe(false)
	})

	it('keeps a failed update open for correction', async () => {
		const store = await readyStore()
		mocks.call.mockRejectedValueOnce(new Error('Save refused'))
		await expect(store.updateTeam(team as any)).rejects.toThrow('Save refused')
		expect(store.updatingTeam).toBe(false)
	})

	it('resolves a confirmed deletion after the team list is refreshed', async () => {
		const store = await readyStore()
		mocks.confirm.mockImplementation(({ onSuccess }) => onSuccess())
		mocks.call.mockImplementation((method: string) =>
			Promise.resolve(method.endsWith('get_teams') ? [] : undefined),
		)
		await store.deleteTeam(team.name)
		expect(store.teams).toEqual([])
		expect(store.deletingTeam).toBe(false)
	})
})
