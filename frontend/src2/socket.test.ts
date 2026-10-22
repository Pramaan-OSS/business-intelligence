import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { io } from 'socket.io-client'

vi.mock('socket.io-client', () => ({ io: vi.fn() }))

beforeEach(() => {
	vi.resetModules()
	vi.mocked(io).mockReset()
	vi.mocked(io).mockReturnValue({ id: 'fixture' } as ReturnType<typeof io>)
})
afterEach(() => {
	vi.unstubAllGlobals()
})

it('uses the app address and tenant on a spare local port', async () => {
	vi.stubGlobal('window', {
		location: new URL('http://127.0.0.1:18129'),
		site_name: 'bi.localhost',
	})
	const { getSocket } = await import('./socket')
	getSocket()
	expect(io).toHaveBeenCalledWith('http://127.0.0.1:18129/bi.localhost', {
		withCredentials: true,
		reconnectionAttempts: 5,
	})
})

it('preserves HTTPS and its explicit port', async () => {
	vi.stubGlobal('window', {
		location: new URL('https://reports.example.com:18443'),
		site_name: 'reports.example.com',
	})
	const { getSocket } = await import('./socket')
	getSocket()
	expect(io).toHaveBeenCalledWith(
		'https://reports.example.com:18443/reports.example.com',
		expect.any(Object),
	)
})

it('uses the hostname when boot data is absent and reuses the connection', async () => {
	vi.stubGlobal('window', { location: new URL('http://bi.localhost:8080') })
	const { getSocket } = await import('./socket')
	expect(getSocket()).toBe(getSocket())
	expect(io).toHaveBeenCalledTimes(1)
	expect(io).toHaveBeenCalledWith('http://bi.localhost:8080/bi.localhost', expect.any(Object))
})
