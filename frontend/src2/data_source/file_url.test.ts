import { describe, expect, it } from 'vitest'
import { isHttpFileUrl } from './file_url'

describe('database file address editing', () => {
	it.each(['', 'file', 'https://', '/local/file.duckdb', 'file:///file.duckdb', 'ftp://localhost/file.duckdb'])(
		'refuses an incomplete or unsupported address without throwing: %s',
		(value) => expect(isHttpFileUrl(value)).toBe(false),
	)
	it.each(['https://localhost/file.duckdb', 'http://127.0.0.1:18139/file.duckdb'])(
		'accepts an absolute web address: %s',
		(value) => expect(isHttpFileUrl(value)).toBe(true),
	)
})
