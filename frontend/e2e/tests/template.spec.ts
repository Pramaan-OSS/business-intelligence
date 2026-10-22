import type { Page } from '@playwright/test'
import { expect, test } from '../fixtures'
import { INSIGHTS_PATH, VIEWER_EMAIL } from '../helpers/auth'
import { createWorkbook, deleteWorkbook, uniqueTitle } from '../helpers/insights'

async function settings(page: Page, panel: string) {
	await page.goto(`${INSIGHTS_PATH}/data-source`)
	await page.getByText('Settings', { exact: true }).click()
	if (panel !== 'Profile')
		await page.getByRole('dialog').getByText(panel, { exact: true }).first().click()
	await expect(page.getByRole('heading', { name: panel, exact: true })).toBeVisible()
}

async function changeFirstDayOfWeek(page: Page) {
	const week = page.getByRole('dialog').getByRole('combobox')
	const day = (await week.innerText()).trim() === 'Tuesday' ? 'Monday' : 'Tuesday'
	await week.click()
	await page.getByRole('option', { name: day, exact: true }).click()
	return day
}

test.describe('template connections and settings', () => {
	test('an administrator can open the retained local desk', async ({ page }) => {
		await page.goto(`${INSIGHTS_PATH}/dashboards`)
		await page.getByRole('button', { name: /Business Intelligence/ }).click()
		const opened = page.waitForEvent('popup')
		await page.getByRole('menuitem', { name: 'Switch to Desk', exact: true }).click()
		const desk = await opened
		await desk.waitForURL(/\/desk/)
		await expect(desk.locator('.main-section')).toBeVisible()
		await expect(desk.locator('body')).not.toContainText('Page Not Found')
		await desk.close()
	})

	test('a workbook can be copied as JSON and duplicated', async ({
		page,
		context,
		adminApi,
		workbookWithQuery,
	}) => {
		const { workbook } = workbookWithQuery
		await context.grantPermissions(['clipboard-read', 'clipboard-write'])
		await page.goto(`${INSIGHTS_PATH}/workbook/${workbook.name}`)
		await expect(page.getByRole('cell', { name: 'order_id', exact: true })).toBeVisible()
		const menu = page.locator('div.h-11').locator('button[aria-haspopup="menu"]')
		await menu.click()
		await page.getByRole('menuitem', { name: 'Copy JSON', exact: true }).click()
		await expect(page.getByText('Copied to clipboard', { exact: true })).toBeVisible()
		const exported = JSON.parse(await page.evaluate(() => navigator.clipboard.readText()))
		expect(JSON.stringify(exported)).toContain('orders')
		await menu.click()
		await page.getByRole('menuitem', { name: 'Duplicate', exact: true }).click()
		await page
			.getByRole('dialog', { name: 'Duplicate Workbook', exact: true })
			.getByRole('button', { name: 'Confirm', exact: true })
			.click()
		await expect(page).not.toHaveURL(new RegExp(`/workbook/${workbook.name}/`))
		await expect(page.getByRole('cell', { name: 'order_id', exact: true })).toBeVisible()
		const duplicate = page.url().match(/\/workbook\/([^/]+)/)![1]
		await deleteWorkbook(adminApi, duplicate)
	})

	test('workbook title search, access filter and load more work together', async ({
		page,
		adminApi,
	}) => {
		const prefix = uniqueTitle('Pagination')
		const records = []
		try {
			for (let i = 0; i < 23; i++)
				records.push(await createWorkbook(adminApi, `${prefix} ${i}`))
			await page.goto(`${INSIGHTS_PATH}/workbook`)
			await page.getByPlaceholder('Title', { exact: true }).fill(prefix)
			const links = page.getByRole('link', { name: new RegExp(prefix) })
			await expect(links).toHaveCount(20)
			await page.getByRole('button', { name: 'Load more', exact: true }).click()
			await expect(links).toHaveCount(23)
			await expect(page.getByRole('button', { name: 'Load more', exact: true })).toBeHidden()
			await page.getByRole('button', { name: 'Access', exact: true }).click()
			await page.getByRole('option', { name: 'Shared', exact: true }).click()
			await page.keyboard.press('Escape')
			await expect(links).toHaveCount(0)
			await page.getByRole('button', { name: 'Shared', exact: true }).click()
			await page.getByRole('option', { name: 'Shared', exact: true }).click()
			await page.keyboard.press('Escape')
			await expect(links).toHaveCount(20)
		} finally {
			for (const record of records) await deleteWorkbook(adminApi, record.name)
		}
	})

	test('password recovery uses the local sign-in page and fake mail', async ({ guestPage }) => {
		const errors: string[] = []
		guestPage.on('pageerror', (error) => errors.push(error.message))
		await guestPage.goto('/login')
		await guestPage.getByRole('link', { name: 'Forgot password?', exact: true }).click()
		await guestPage.locator('#forgot_email').fill(VIEWER_EMAIL)
		await guestPage.getByRole('button', { name: 'Send Link', exact: true }).click()
		await expect(guestPage.locator('.for-forgot')).toContainText(/sent|email.*link|check your/i)
		expect(errors).toEqual([])
	})

	test('a dashboard preview can be refreshed locally', async ({
		page,
		workbookWithDashboard,
	}, testInfo) => {
		const { dashboard } = workbookWithDashboard
		await page.goto(`${INSIGHTS_PATH}/dashboards`)
		const card = page.locator('[class~="group/card"]').filter({ hasText: dashboard.title })
		await card.locator('button[aria-haspopup="menu"]').click()
		await page.getByRole('menuitem', { name: 'Refresh Preview', exact: true }).click()
		await expect(
			page.getByText('Preview image updated successfully', { exact: true }),
		).toBeVisible({ timeout: 30000 })
		await expect(card.locator('img')).toBeVisible()
		await expect
			.poll(() =>
				card
					.locator('img')
					.evaluate(
						(image: HTMLImageElement) => image.complete && image.naturalWidth > 0,
					),
			)
			.toBe(true)
		await page.screenshot({ path: testInfo.outputPath('preview-working.png') })
	})

	test('a SQL draft opens and is inserted only after review', async ({ page, workbook }) => {
		await page.goto(`${INSIGHTS_PATH}/workbook/${workbook.name}`)
		await page.getByText('SQL Editor', { exact: true }).click()
		await page.getByRole('button', { name: 'Select a data source' }).click()
		await page.getByRole('option', { name: 'Demo Data', exact: true }).click()
		await page
			.getByRole('button', { name: 'Execute', exact: true })
			.locator('..')
			.getByRole('button')
			.last()
			.click()
		await page.getByText('Draft SQL', { exact: true }).click()
		const dialog = page.getByRole('dialog', { name: 'Draft SQL', exact: true })
		await expect(dialog).toBeVisible()
		await dialog.getByLabel('What do you want to know?').fill('Count the orders')
		await dialog.getByRole('button', { name: 'Get draft', exact: true }).click()
		await expect(dialog.locator('pre')).toContainText('SELECT 1 AS one')
		await expect(page.locator('.cm-content')).not.toContainText('SELECT 1 AS one')
		await dialog.getByRole('button', { name: 'Insert into editor', exact: true }).click()
		await expect(page.locator('.cm-content')).toContainText('SELECT 1 AS one')
	})

	test('saved delivery targets survive reopening and channel pairing finishes', async ({
		page,
	}) => {
		const subscriber = `qa-member-${Date.now()}`
		const workflow = `https://example.com/qa-workflow/${Date.now()}`
		await settings(page, 'Alert delivery')
		await page.getByPlaceholder('Subscriber id').fill(subscriber)
		await page.getByPlaceholder('https://…').fill(workflow)
		await page.getByRole('button', { name: 'Update', exact: true }).first().click()
		await expect(page.getByText('Settings Updated', { exact: true })).toBeVisible()
		await settings(page, 'Alert delivery')
		await expect(page.getByPlaceholder('Subscriber id')).toHaveValue(subscriber)
		await expect(page.getByPlaceholder('https://…')).toHaveValue(workflow)
		await page.getByRole('button', { name: 'Connect channel', exact: true }).click()
		await expect(page.getByText('Fixture channel', { exact: true })).toBeVisible()
		await page.getByRole('button', { name: 'Disconnect', exact: true }).click()
		await expect(
			page.getByRole('button', { name: 'Connect channel', exact: true }),
		).toBeVisible()
	})

	test('a connected read is stored, can refresh, and can be removed', async ({
		page,
		adminApi,
		context,
	}) => {
		await context.route('https://fake-pramaan/**', (route) =>
			route.fulfill({ contentType: 'text/html', body: '<h1>Connected test account</h1>' }),
		)
		const title = uniqueTitle('Connected source')
		await page.goto(`${INSIGHTS_PATH}/data-source`)
		await page.getByRole('button', { name: 'New Data Source', exact: true }).first().click()
		await page.getByText('Connected app', { exact: true }).click()
		await page.getByLabel('Find a spreadsheet or app').fill('spreadsheets')
		await page.getByRole('button', { name: 'Search catalogue', exact: true }).click()
		await page.getByRole('button', { name: 'Spreadsheet reads', exact: true }).click()
		const popup = page.waitForEvent('popup')
		await page.getByRole('link', { name: 'Open secure connection', exact: true }).click()
		const account = await popup
		await expect(account.getByRole('heading', { name: 'Connected test account' })).toBeVisible()
		await account.close()
		await expect(
			page.getByText('Connected. Review the read below.', { exact: true }),
		).toBeVisible()
		await page.getByLabel('Source title', { exact: true }).fill(title)
		await page.getByLabel('Reviewed read path', { exact: false }).fill('/records')
		await page.getByRole('button', { name: 'Save and read data', exact: true }).click()
		await page.getByText(title, { exact: true }).click()
		await expect(page).toHaveURL(/\/data-source\/[^/]+$/)
		const source = page.url().split('/').pop()!
		await page.getByText('records', { exact: true }).click()
		await expect(page.getByRole('cell', { name: 'one', exact: true })).toBeVisible()
		await page.goto(`${INSIGHTS_PATH}/data-source/${source}`)
		await page.getByRole('button', { name: 'Table actions', exact: true }).click()
		await page.getByRole('menuitem', { name: 'Update Tables', exact: true }).click()
		await expect(page.getByText(`Tables updated for ${source}`, { exact: true })).toBeVisible()
		await adminApi.deleteDoc('Insights Data Source v3', source)
		await page.goto(`${INSIGHTS_PATH}/data-source`)
		await expect(page.getByText(title, { exact: true })).toHaveCount(0)
	})

	test('a scheduled report can be sent, saved, disabled and deleted', async ({
		page,
		adminApi,
		workbookWithQuery,
	}) => {
		const { workbook, query } = workbookWithQuery
		const title = uniqueTitle('Scheduled report')
		await page.goto(`${INSIGHTS_PATH}/workbook/${workbook.name}/query/${query.name}`)
		await page
			.getByRole('button', { name: 'Execute', exact: true })
			.locator('..')
			.getByRole('button')
			.last()
			.click()
		await page.getByText('Alerts', { exact: true }).click()
		await page.getByRole('button', { name: 'New Alert', exact: true }).first().click()
		await page
			.getByRole('switch', { name: 'Email query report on every scheduled run' })
			.click()
		await page.getByLabel('Alert Name', { exact: true }).fill(title)
		await page.getByLabel('Recipients', { exact: true }).fill('qa-reader@example.com')
		await page.getByRole('button', { name: 'Send Test Alert', exact: true }).click()
		await expect(page.getByText('Alert Sent', { exact: true })).toBeVisible()
		await page.getByRole('button', { name: 'Create Alert', exact: true }).click()
		await expect(page.getByRole('dialog', { name: 'Setup Alert', exact: true })).toBeHidden()
		const [alert] = await adminApi.getList<{ name: string }>('Insights Alert', {
			filters: { title },
			fields: ['name'],
		})
		expect(alert).toBeTruthy()
		await adminApi.updateDoc('Insights Alert', alert.name, { disabled: 1 })
		await adminApi.deleteDoc('Insights Alert', alert.name)
	})

	test('profile, general and data-store settings save and reload', async ({ page, adminApi }) => {
		await settings(page, 'Profile')
		const firstName = uniqueTitle('QA')
		await page.getByLabel('First Name', { exact: true }).fill(firstName)
		await page.getByRole('button', { name: 'Update', exact: true }).click()
		await expect(page.getByText('User Updated', { exact: true })).toBeVisible()
		await settings(page, 'Profile')
		await expect(page.getByLabel('First Name', { exact: true })).toHaveValue(firstName)
		await settings(page, 'General')
		const day = await changeFirstDayOfWeek(page)
		await page.getByRole('button', { name: 'Update', exact: true }).click()
		await expect(page.getByText('Settings Updated', { exact: true })).toBeVisible()
		await settings(page, 'General')
		await expect(page.getByRole('dialog').getByRole('combobox')).toContainText(day)
		await settings(page, 'Data Store')
		await expect(page.getByText('Memory Limit', { exact: true })).toBeVisible()
		await settings(page, 'Permissions')
		await expect(page.getByText('Allow Data Download', { exact: true })).toBeVisible()
		await adminApi.callMethod('insights.api.alert_channels.save_delivery_targets', {
			notify_subscriber_id: 'qa-member',
			workflow_webhook_url: 'https://example.com/qa-workflow',
		})
	})

	test('a member invitation uses mail and users search narrows the list', async ({
		page,
		adminApi,
	}) => {
		const email = `qa-invite-${Date.now()}@example.com`
		await settings(page, 'Users')
		await page.getByRole('button', { name: 'Invite User', exact: true }).click()
		await page.getByPlaceholder('Enter email address').fill('removed@example.com')
		await page.getByPlaceholder('Enter email address').press('Enter')
		await page
			.getByRole('button', { name: 'removed@example.com', exact: true })
			.locator('svg')
			.click()
		await expect(
			page.getByRole('button', { name: 'removed@example.com', exact: true }),
		).toHaveCount(0)
		await page.getByPlaceholder('Enter email address').fill(email)
		await page.getByPlaceholder('Enter email address').press('Enter')
		await page.getByRole('button', { name: 'Send Invitation', exact: true }).click()
		await expect(page.getByText(email, { exact: true }).first()).toBeVisible()
		await page.getByPlaceholder('Search', { exact: true }).fill(email)
		await expect(
			page.getByRole('dialog').getByText('Administrator', { exact: true }),
		).toHaveCount(0)
		const invites = await adminApi.getList<{ name: string }>('Insights User Invitation', {
			fields: ['name'],
			filters: { email },
		})
		for (const invitation of invites)
			await adminApi.deleteDoc('Insights User Invitation', invitation.name)
	})
})
